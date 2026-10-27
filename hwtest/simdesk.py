#!/usr/bin/env python3
"""Simulated desk for the dry runs of the EV-HW runbooks (EV-15): the targets of one runbook on this machine.

    simdesk.py serve --dir /tmp/rb-x --profile lp      # start, write <dir>/sim.json (runbook variables), serve ctl
    simdesk.py ctl --dir /tmp/rb-x svc restart flexengine
    simdesk.py ctl --dir /tmp/rb-x stop

What a profile starts (every part a real process or the reference stand-in of the test suite, read-only):

  cloud      the reference flexCloud fake (flexengine2.testing.fakecloud) with the profile's model, admin token file
  tls        a TLS front with a sim lab CA (the engine pins it: caFile), as the LattePanda's 9700 port
  uplink     NetShim between the engine and the TLS front (`uplink down|up`: the lab's link shim)
  engine     `flexengine run --config <dir>/lp/engine.yaml` (local API socket + read-only TCP bind with a token
             file), supervised like flexengine.service (Restart=on-failure, RestartSec=1); its stdout is the
             journal stand-in <dir>/lp/engine.log
  profiles   lp (container + native + podman devices), install (EV-HW-01: packaging/install.sh --root),
             mcu (QEMU mps2/an385 MCUboot + base image as the Portenta, relay with a Zenoh endpoint),
             applab (P4 board simulator + fake arduino-app-cli, remote mode over a fake ssh),
             plc (pyads `Hello` + pymodbus stand-ins of the CX and the Opta), rauc (fake rauc + boot simulator,
             the machine reboots by itself when the driver asks),
             fe4 (EV-HW-12: one device with the runtimes process + systemd (native) and docker + docker-compose
             (container); systemd, docker and compose are the contract suite's fake CLIs (tests/contract/
             spi_harness/fakes) on the engine's PATH, the model is the vendored UC1, no framework checkout needed)

ctl commands (the runbook's `svc`, `sim` and `uplink_*` variables point here in a dry run):
  svc start|stop|restart|kill [-s SIG]|is-active flexengine     systemctl stand-in for the engine
  uplink down|up                                                 cut / restore the engine's link to the cloud
  power-cycle [--down S]                                         power cut of the LattePanda (engine, workloads)
  mcu-power-cycle [--when-testing]                               power cut of the MCU (QEMU)
  plc-set cx|opta HASH|@RELEASE                                  the program the PLC runs (Hello / registers)
  engine-config KEY=VALUE                                        change engine.yaml (e.g. cloud.caFile) + restart
  copy SRC DST, stop
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FLEXENGINE = HERE.parent
# The framework checkout (examples/models, probes, demo/): $FLEX_REPO, default a sibling modular-interoperable-framework
REPO = Path(os.environ.get("FLEX_REPO") or FLEXENGINE.parent / "modular-interoperable-framework").resolve()
SRC = FLEXENGINE / "src"
FLEXMODEL_SRC = REPO / "flexmodel" / "src"
PROBES = Path(os.environ.get("FLEX_PROBES_DIR") or REPO / "probes").resolve()
P3 = PROBES / "p3-mcu-hotload"
P4 = PROBES / "p4-applab-target"
UC1 = REPO / "examples" / "models" / "uc1"
DP = REPO / "examples" / "models" / "demo-pick"
DEV_PREFIX = "rb-"
GP, GP_PODMAN, MCU, APP = "rb-panda", "rb-panda-podman", "rb-panda.portenta", "rb-unoq"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30, host: str = "127.0.0.1") -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection((host, port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} did not open within {timeout} s")


def log(msg: str) -> None:
    print(f"{datetime.datetime.now().strftime('%H:%M:%S')} simdesk: {msg}", flush=True)


# ---- PKI + TLS front ------------------------------------------------------------------------------------------------
def make_pki(d: Path, names=("lattepanda.lab", "localhost"), ips=("127.0.0.1",)) -> dict:
    """Lab CA + server certificate (and a second, rogue CA for the wrong-CA case)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())

    def ca(cn):
        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=30))
             .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
             .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                          crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
             .sign(k, hashes.SHA256()))
        return c, k

    out = {}
    for label in ("ca", "rogue-ca"):
        c, k = ca(f"flex sim desk {label}")
        (d / f"{label}.crt").write_bytes(c.public_bytes(serialization.Encoding.PEM))
        out[label] = (c, k)
    c_ca, k_ca = out["ca"]
    k = ec.generate_private_key(ec.SECP256R1())
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(c_ca.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(k_ca.public_key()), critical=False)
            .sign(k_ca, hashes.SHA256()))
    (d / "server.crt").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (d / "server.key").write_bytes(pem_key(k))
    os.chmod(d / "server.key", 0o600)
    return {"ca": d / "ca.crt", "rogue_ca": d / "rogue-ca.crt", "cert": d / "server.crt", "key": d / "server.key"}


class TlsFront:
    """TLS termination in front of the plain-HTTP cloud fake (the lab's TLS port)."""

    def __init__(self, upstream: int, cert: Path, key: Path, port: int = 0):
        self.upstream = upstream
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.stopped = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(15)
            t = self.ctx.wrap_socket(c, server_side=True)
        except (OSError, ssl.SSLError):
            c.close()
            return
        try:
            u = socket.create_connection(("127.0.0.1", self.upstream), timeout=15)
        except OSError:
            t.close()
            return

        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        th = threading.Thread(target=pump, args=(u, t), daemon=True)
        th.start()
        pump(t, u)
        th.join(10)
        t.close()
        u.close()

    def close(self):
        self.stopped = True
        self.sock.close()


# ---- supervised service (the systemd unit stand-in) -----------------------------------------------------------------
class Service:
    def __init__(self, name: str, argv: list[str], log_path: Path, env: dict, cwd: Path, restart_sec: float = 1.0):
        self.name, self.argv, self.log_path, self.env, self.cwd = name, argv, log_path, env, cwd
        self.restart_sec = restart_sec
        self.proc: subprocess.Popen | None = None
        self.wanted = False          # "enabled + started": restart when the process dies
        self.lock = threading.RLock()
        self.restarts = 0
        self.starts = 0
        threading.Thread(target=self._watch, daemon=True, name=f"svc-{name}").start()

    def _spawn(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdout=f, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        f.close()
        self.starts += 1

    def start(self):
        with self.lock:
            self.wanted = True
            if not self.active():
                self._spawn()

    def stop(self, timeout: float = 20):
        with self.lock:
            self.wanted = False
            p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)

    def kill(self, sig=signal.SIGKILL, keep: bool = True):
        """`systemctl kill -s SIG`: the unit fails and comes back (Restart=on-failure) unless keep=False."""
        with self.lock:
            if not keep:
                self.wanted = False
            p = self.proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                pass
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def restart(self):
        self.stop()
        self.start()

    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _watch(self):
        while True:
            time.sleep(0.2)
            with self.lock:
                if self.wanted and self.proc is not None and self.proc.poll() is not None:
                    rc = self.proc.returncode
                    with open(self.log_path, "a") as f:
                        f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                            "level": "warning", "event": "systemd.restart",
                                            "msg": f"{self.name}.service: main process exited (rc {rc}); "
                                                   f"scheduled restart"}) + "\n")
                    time.sleep(self.restart_sec)
                    if self.wanted:
                        self._spawn()
                        self.restarts += 1


# ---- the desk --------------------------------------------------------------------------------------------------------
class Sim:
    def __init__(self, d: Path, profile: str):
        self.dir = Path(d).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.py = sys.executable
        self.vars: dict[str, str] = {}
        self.closers: list = []
        self.services: dict[str, Service] = {}
        self.ctl_extra: dict = {}
        self.lp = self.dir / "lp"
        self.lp.mkdir(exist_ok=True)
        self.secrets = self.dir / "secrets"
        self.secrets.mkdir(exist_ok=True, mode=0o700)
        self.devices_cfg: list[dict] = []
        self.engine_cfg_path = self.lp / "engine.yaml"
        self.engine_bin: list[str] = [self.py, "-m", "flexengine2.local.cli"]
        self.engine_env_extra: dict = {}
        self.cloud = None
        self.shim = None
        self.catalog: dict = {}
        self.stopped = threading.Event()

    # -- environment ---------------------------------------------------------------------------------------------
    def pypath(self) -> str:
        return os.pathsep.join([str(SRC), str(FLEXMODEL_SRC)])

    def base_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "FLEXENGINE_CONFIG")}
        env.update({"PYTHONPATH": self.pypath(), "PYTHONUNBUFFERED": "1", "FLEX_PROBES_DIR": str(PROBES)})
        return env

    def secret(self, name: str, value: str) -> Path:
        p = self.secrets / name
        p.write_text(value + "\n")
        p.chmod(0o600)
        return p

    # -- cloud ---------------------------------------------------------------------------------------------------
    def start_cloud(self, models: list[Path]):
        from flexengine2.identity import crypto
        from flexengine2.testing.fakecloud import CloudServer
        from flexengine2.testing.netshim import NetShim

        self.keys = {"cloud": crypto.load_or_create(self.secrets / "cloud.key"),
                     "release": crypto.load_or_create(self.secrets / "release.key"),
                     "rogue": crypto.load_or_create(self.secrets / "rogue.key")}
        self.cloud = CloudServer(self.dir / "cloud", 0, "sim-admin-token", self.keys["cloud"],
                                 model_dirs=[Path(m) for m in models] or None).start()
        self.closers.append(self.cloud.stop)
        self.pki = make_pki(self.dir / "pki")
        self.tls = TlsFront(self.cloud.port, self.pki["cert"], self.pki["key"])
        self.closers.append(self.tls.close)
        self.shim = NetShim(self.tls.port)
        self.closers.append(self.shim.close)
        admin = self.secret("admin.token", "sim-admin-token")
        self.vars.update(cloud_url=self.cloud.url, cloud_ca="", admin_token_file=str(admin),
                         cloud_host="127.0.0.1", cloud_port=str(self.tls.port), cloud_layout=str(self.dir / "cloud" / "oci"),
                         lab_ca=str(self.pki["ca"]), rogue_ca=str(self.pki["rogue_ca"]))
        log(f"cloud fake {self.cloud.url}, TLS front :{self.tls.port}, uplink shim :{self.shim.port}")

    def token(self, dev: str) -> str:
        from flexengine2.testing.edge_lab import admin

        s, b = admin(self.cloud.port, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev},
                     token="sim-admin-token")
        assert s == 201, b
        return b["token"]

    # -- engine --------------------------------------------------------------------------------------------------
    def engine_config(self, extra: dict | None = None) -> dict:
        from flexengine2.identity import crypto

        tok = self.secret("fe-api.token", "sim-local-api-token")
        self.api_port = getattr(self, "api_port", None) or free_port()
        cfg = {"cloud": {"apiUrl": f"https://127.0.0.1:{self.shim.port}", "caFile": str(self.pki["ca"]),
                         "cloudPublicKey": crypto.public_b64(self.keys["cloud"]),
                         "releasePublicKeys": [crypto.public_b64(self.keys["release"])],
                         "pollInterval": 0.5, "backoff": [0.2, 2.0], "httpTimeout": 5},
               "dataDir": str(self.lp / "data"),
               "local": {"socket": str(self.lp / "api.sock"),
                         "tcp": {"bind": f"127.0.0.1:{self.api_port}", "tokenFile": str(tok)}},
               "logFormat": "json", "devices": self.devices_cfg}
        cfg.update(extra or {})
        self.vars.update(fe_api=f"http://127.0.0.1:{self.api_port}", fe_api_token_file=str(tok),
                         fe_api_port=str(self.api_port))
        return cfg

    def write_engine_config(self, cfg: dict) -> Path:
        self.engine_cfg = cfg
        self.engine_cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return self.engine_cfg_path

    def make_engine_service(self):
        env = {**self.base_env(), "FLEX_OTEL": "1", "FLEX_OTEL_FILE": str(self.lp / "spans.jsonl"),
               **self.engine_env_extra}
        svc = Service("flexengine", [*self.engine_bin, "run", "--config", str(self.engine_cfg_path)],
                      self.lp / "engine.log", env, self.lp)
        self.services["flexengine"] = svc
        self.closers.append(lambda: svc.stop())
        fe = " ".join(shlex.quote(x) for x in self.engine_bin)
        self.vars.update(
            fe=f"env FLEXENGINE_CONFIG={shlex.quote(str(self.engine_cfg_path))} {fe}",
            fe_enroll=f"env FLEXENGINE_CONFIG={shlex.quote(str(self.engine_cfg_path))} {fe} enroll",
            fe_config=str(self.engine_cfg_path), journal_source=f"file:{self.lp / 'engine.log'}",
            svc=f"{shlex.quote(self.py)} {shlex.quote(str(HERE / 'simdesk.py'))} ctl --dir {shlex.quote(str(self.dir))} svc")
        return svc

    def start_engine(self, wait_devices: list[str] | None = None):
        svc = self.services.get("flexengine") or self.make_engine_service()
        svc.start()
        wait_port(self.api_port, 60)
        for dev in wait_devices or []:
            self.wait_onboarded(dev)

    def wait_onboarded(self, dev: str, timeout: float = 60):
        from flexengine2.testing.edge_lab import admin

        end = time.time() + timeout
        while time.time() < end:
            s, b = admin(self.cloud.port, "GET", f"/api/v1/admin/devices/{dev}", token="sim-admin-token")
            if s == 200 and b.get("device") and (b["device"] or {}).get("capabilities"):
                return
            time.sleep(0.3)
        raise TimeoutError(f"{dev} not onboarded; engine log tail:\n{self.log_tail()}")

    def log_tail(self, n: int = 3000) -> str:
        p = self.lp / "engine.log"
        return p.read_text(errors="replace")[-n:] if p.exists() else ""

    # -- catalog -------------------------------------------------------------------------------------------------
    def write_catalog(self, doc: dict) -> Path:
        p = self.dir / "catalog.yaml"
        doc = {"releaseKey": str(self.secrets / "release.key"), "keys": {"rogue": str(self.secrets / "rogue.key")},
               "work": str(self.dir / "desk-work"), **doc}
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
        self.vars["catalog"] = str(p)
        return p

    def seed_container_payloads(self):
        """The probe's cached docker-save archives of the stand-in app (read-only: symlinked, never written)."""
        src = PROBES / "p2-reconciling-engine" / "work" / "payloads"
        dst = self.dir / "desk-work" / "payloads"
        dst.mkdir(parents=True, exist_ok=True)
        for tar in src.glob("*.image.tar"):
            if not (dst / tar.name).exists():
                os.symlink(tar, dst / tar.name)

    # -- control ---------------------------------------------------------------------------------------------------
    def ctl(self, argv: list[str]) -> tuple[int, str]:
        if not argv:
            return 2, "no command"
        cmd, args = argv[0], argv[1:]
        if cmd == "svc":
            return self.ctl_svc(args)
        if cmd == "uplink":
            if args[:1] == ["down"]:
                self.shim.cut()
                return 0, "uplink down (link shim cut)"
            if args[:1] == ["up"]:
                self.shim.restore()
                return 0, "uplink up"
            return 2, "uplink down|up"
        if cmd == "power-cycle":
            return self.power_cycle(float(args[1]) if args[:1] == ["--down"] else 3.0)
        if cmd == "engine-config":
            return self.ctl_engine_config(args)
        if cmd == "copy":
            shutil.copyfile(args[0], args[1])
            return 0, f"copied {args[0]} -> {args[1]}"
        if cmd == "stop":
            self.stopped.set()
            return 0, "stopping"
        if cmd == "vars":
            return 0, json.dumps(self.vars, indent=1)
        if cmd in self.ctl_extra:
            return self.ctl_extra[cmd](args)
        return 2, f"unknown command {cmd!r}"

    def ctl_svc(self, args: list[str]) -> tuple[int, str]:
        if not args:
            return 2, "svc start|stop|restart|kill|is-active flexengine"
        op, rest = args[0], args[1:]
        sig = signal.SIGTERM
        if op == "kill" and rest[:1] == ["-s"]:
            sig = getattr(signal, "SIG" + rest[1].upper().removeprefix("SIG"))
            rest = rest[2:]
        name = (rest[-1] if rest else "flexengine").removesuffix(".service")
        svc = self.services.get(name)
        if svc is None:
            if name == "flexengine" and op in ("start", "restart", "enable") and self.engine_cfg_path.exists():
                svc = self.make_engine_service()
            else:
                return 5, f"Unit {name}.service not found."
        if op in ("start", "enable"):
            svc.start()
            return 0, ""
        if op == "stop":
            svc.stop()
            return 0, ""
        if op == "restart":
            svc.restart()
            return 0, ""
        if op == "kill":
            svc.kill(sig)
            return 0, f"sent {sig.name} to {name}"
        if op == "is-active":
            return (0, "active") if svc.active() else (3, "inactive")
        if op == "status":
            return (0 if svc.active() else 3), json.dumps({"active": svc.active(), "starts": svc.starts,
                                                          "restarts": svc.restarts, "pid": svc.proc.pid if svc.proc else None})
        return 2, f"unknown svc op {op!r}"

    def ctl_engine_config(self, args: list[str]) -> tuple[int, str]:
        cfg = yaml.safe_load(self.engine_cfg_path.read_text())
        for kv in args:
            k, v = kv.split("=", 1)
            cur = cfg
            parts = k.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            if v == "":
                cur.pop(parts[-1], None)
            else:
                try:
                    cur[parts[-1]] = json.loads(v)
                except ValueError:
                    cur[parts[-1]] = v
        self.write_engine_config(cfg)
        self.services["flexengine"].restart()
        return 0, f"engine.yaml: {' '.join(args)}; restarted"

    def container_devs(self) -> set[str]:
        return {d["deviceId"] for d in self.devices_cfg if (d.get("drivers") or {}).get("container")}

    def workload_kill(self):
        """What a power cut does to the workloads: every container and native unit of the sim devices dies."""
        for rt in ("docker", "podman"):
            if shutil.which(rt):
                ids = subprocess.run([rt, "ps", "-q", "--filter", "label=dev.flex.device"], capture_output=True,
                                     text=True).stdout.split()
                mine = []
                for i in ids:
                    dev = subprocess.run([rt, "inspect", "-f", '{{index .Config.Labels "dev.flex.device"}}', i],
                                         capture_output=True, text=True).stdout.strip()
                    if dev in self.container_devs():
                        mine.append(i)
                if mine:
                    subprocess.run([rt, "kill", *mine], capture_output=True)
        for units in (self.lp / "data").glob("*/native/units.json"):
            try:
                for u in json.loads(units.read_text()).values():
                    if u.get("pid"):
                        try:
                            os.killpg(int(u["pid"]), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, ValueError):
                            pass
            except (OSError, ValueError):
                pass

    def power_cycle(self, down: float) -> tuple[int, str]:
        svc = self.services["flexengine"]
        svc.kill(signal.SIGKILL, keep=False)
        self.workload_kill()
        time.sleep(down)
        svc.start()
        wait_port(self.api_port, 60)
        return 0, f"power cut: engine and workloads killed, {down:.0f} s off, booted"

    def serve_ctl(self):
        path = self.dir / "ctl.sock"
        if path.exists():
            path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(8)
        srv.settimeout(0.5)

        def handle(c):
            with c:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                try:
                    argv = json.loads(buf.decode())
                    rc, out = self.ctl(argv)
                except Exception as e:  # noqa: BLE001  report to the caller, keep serving
                    rc, out = 1, f"{type(e).__name__}: {e}"
                c.sendall(json.dumps({"rc": rc, "out": out}).encode() + b"\n")

        while not self.stopped.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(c,), daemon=True).start()
        srv.close()
        path.unlink(missing_ok=True)

    def finish(self):
        """Write the variables the runbook gets with --set (the dry run's view of the desk)."""
        sim = f"{shlex.quote(self.py)} {shlex.quote(str(HERE / 'simdesk.py'))} ctl --dir {shlex.quote(str(self.dir))}"
        self.vars.setdefault("sim", sim)
        self.vars.update(target="sim", dry_run="1", lp="local", fe_host="local", lp_sh="sh -c",
                         uplink_down=f"{sim} uplink down", uplink_up=f"{sim} uplink up",
                         power_cut=f"{sim} power-cycle --down 3", sim_dir=str(self.dir))
        out = self.dir / "sim.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"profile": self.profile, "vars": self.vars, "pid": os.getpid()}, indent=1))
        os.replace(tmp, out)
        log(f"ready: {out}")

    def close(self):
        for c in reversed(self.closers):
            try:
                c()
            except Exception as e:  # noqa: BLE001
                log(f"close: {e!r}")
        self.cleanup_workloads()

    def cleanup_workloads(self):
        if not self.container_devs():
            self.workload_kill()
            return
        for rt in ("docker", "podman"):
            if not shutil.which(rt):
                continue
            ids = subprocess.run([rt, "ps", "-aq", "--filter", "label=dev.flex.device"], capture_output=True,
                                 text=True).stdout.split()
            for i in ids:
                dev = subprocess.run([rt, "inspect", "-f", '{{index .Config.Labels "dev.flex.device"}}', i],
                                     capture_output=True, text=True).stdout.strip()
                if dev in self.container_devs():
                    subprocess.run([rt, "rm", "-f", i], capture_output=True)
        self.workload_kill()


# ---- profiles ------------------------------------------------------------------------------------------------------
def container_device(dev: str, runtime: str, native: bool, safe: Path, tok: Path) -> dict:
    return {"deviceId": dev, "role": "standalone-device", "onboardingTokenFile": str(tok), "safeStateFile": str(safe),
            "drivers": {"container": {"runtime": runtime, "allowedRunOptions": {"devices": ["/dev/zero"]}},
                        "native": native}}


def lp_catalog(sim: Sim, runtime_devices=("/dev/zero",)) -> dict:
    two = ["alice", "bob"]
    ro = {"devices": list(runtime_devices)}

    def ctr(v, **kw):
        return {"module": "perception_guard", "version": v, "driver": "container",
                "build": {"kind": "container", "approvers": two, "runOptions": ro, **kw}}

    def nat(v, **kw):
        return {"module": "detection_logger", "version": v, "driver": "native", "build": {"kind": "native", **kw}}

    rel = {"app_v1": ctr("1.0.0"), "app_v2": ctr("1.1.0"), "app_bad": ctr("1.2.0", failHealth=True),
           "app_safety": {**ctr("1.3.0"), "activation": {"requires": ["machine_parked", "operator_confirmed"]}},
           "dl_v1": nat("1.0.0"), "dl_v2": nat("1.1.0"), "dl_v3": nat("2.0.0"), "dl_v4": nat("2.1.0"),
           "dl_bad": nat("1.2.0", failHealth=True), "dl_unsigned": nat("3.0.0", sign=False),
           "dl_rogue": nat("3.1.0", key="rogue"), "dl_tamper": nat("3.2.0")}
    return {"model": str(UC1), "runtime": "docker", "releases": rel}


def profile_lp(sim: Sim, podman: bool = True):
    sim.start_cloud([UC1])
    safe = sim.lp / "safe-state.json"
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    toks = sim.lp / "tokens"
    toks.mkdir(exist_ok=True)
    devs = [GP] + ([GP_PODMAN] if podman and shutil.which("podman") else [])
    for d in devs:
        t = toks / d
        t.write_text(sim.token(d) + "\n")
        t.chmod(0o600)
    sim.devices_cfg = [container_device(GP, "docker", True, safe, toks / GP)]
    if GP_PODMAN in devs:
        sim.devices_cfg.append(container_device(GP_PODMAN, "podman", False, safe, toks / GP_PODMAN))
    sim.write_engine_config(sim.engine_config())
    sim.seed_container_payloads()
    sim.write_catalog(lp_catalog(sim))
    sim.vars.update(tamper=f"{shlex.quote(sim.py)} {shlex.quote(str(HERE / 'deskctl.py'))} tamper --layout "
                           f"{shlex.quote(str(sim.dir / 'cloud' / 'oci'))}", cloud_sh="local",
                    lab_ca_lp=str(sim.pki["ca"]), rogue_ca=str(sim.dir / "pki" / "rogue-openssl-ca.crt"),
                    lp_up_host="127.0.0.1", lp_up_port=str(sim.api_port))
    sim.vars.update(gp=GP, gp_podman=GP_PODMAN if GP_PODMAN in devs else "", safe_state_file=str(safe),
                    igpu_device="/dev/zero", usb_dir=str(sim.dir / "usb"), lp_usb=str(sim.dir / "usb"), rt="docker", rt_podman="podman")
    (sim.dir / "usb").mkdir(exist_ok=True)
    sim.start_engine(devs)
    jaeger = JaegerStandIn(sim.lp / "spans.jsonl")
    sim.closers.append(jaeger.close)
    sim.vars["jaeger_url"] = f"http://127.0.0.1:{jaeger.port}"


class JaegerStandIn:
    """`GET /api/traces?service=...&operation=...` over the engine's OTel span file (FLEX_OTEL_FILE), in the
    shape of the Jaeger query API ({data: [{traceID, spans: [{operationName, tags}]}]})."""

    def __init__(self, spans: Path):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[-1] for k, v in parse_qs(u.query).items()}
                if u.path != "/api/traces":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"data": outer.traces(q.get("service"), q.get("operation"))}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.spans = spans
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def traces(self, service, operation):
        out: dict[str, list] = {}
        if not self.spans.exists():
            return []
        for line in self.spans.read_text(errors="replace").splitlines():
            try:
                s = json.loads(line)
            except ValueError:
                continue
            name = s.get("name") or s.get("operationName")
            svc = s.get("service") or (s.get("resource") or {}).get("service.name")
            if service and svc and svc != service:
                continue
            if operation and name != operation:
                continue
            tid = s.get("traceId") or s.get("trace_id") or s.get("traceID") or "?"
            attrs = s.get("attributes") or {}
            out.setdefault(tid, []).append({"operationName": name, "spanID": s.get("spanId") or s.get("span_id"),
                                            "tags": [{"key": k, "value": v} for k, v in attrs.items()]})
        return [{"traceID": t, "spans": sp} for t, sp in out.items()]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _p3_import():
    if str(P3) not in sys.path:
        sys.path.insert(0, str(P3))   # stand-ins only: QEMU control (flexp3.qemu), module key loading (flexp3.flxm)
    from flexp3 import flxm as p3flxm
    from flexp3.qemu import Mcu, provision
    return p3flxm, Mcu, provision


def mcu_catalog(sim: Sim) -> dict:
    B = P3 / "build"
    srcs = {"src": str(P3 / "agent_src" / "servo_drive")}
    rust = str(P3.parent / "p1-model-to-targets" / "agent_src" / "rust-nostd" / "src")

    def servo(v, defines, **kw):
        return {"module": "servo_drive", "version": v, "driver": "mcu-module",
                "parameters": {"_interfaceHash": "@interfaceHash", "open_mm": 80, "close_mm": 20},
                "build": {"kind": "flex-release", "targets": [
                    {"name": "servo_drive", "kind": "mcu-module", "instance": "servo", "version": v, **srcs,
                     "defines": defines}], **kw}}

    rel = {
        "servo_2": servo("2.0.0", ["SERVO_GAIN=0.05f"]),
        "servo_stall": servo("3.0.0", ["SERVO_GAIN=0.05f", "FLEX_FAULT_STALL=40"]),
        "grip_1": {"module": "gripper_station", "version": "1.0.0", "driver": "mcu-module",
                   "parameters": {"_interfaceHash": "@interfaceHash", "max_force_n": 30},
                   "build": {"kind": "flex-release", "targets": [
                       {"name": "gripper_station", "kind": "mcu-module", "instance": "gripper", "version": "1.0.0",
                        "rust_src": rust}]}},
        "servo_abi2": {"module": "servo_drive", "version": "4.0.0-abi2", "driver": "mcu-module",
                       "parameters": {"_interfaceHash": "@interfaceHash", "open_mm": 80, "close_mm": 20},
                       "build": {"kind": "mcu-flxm", "flxm": str(B / "modules" / "servo_drive-abi2.flxm"),
                                 "genMeta": str(B / "gen" / "servo_drive")}},
    }
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        rel[f"fw_{v.replace('.', '_')}"] = {"module": "flexmcu_base", "version": v, "driver": "mcu-firmware",
                                            "build": {"kind": "mcu-firmware", "image": str(B / f"base-{v}.signed.bin")}}
    # 1.0.0 is the image of the power-cut step: a 20 s soak keeps it in TESTING long enough for a cut by hand
    rel["fw_1_0_0"]["build"]["health"] = {"minSeconds": 20, "timeoutSeconds": 40}
    return {"model": str(DP), "releases": rel,
            "flexRelease": {"deployment": "d_pick_lab", "mcu": {
                "module_key": str(P3 / "work" / "keys" / "module-ec-p256.pem"),
                "edk": {"cortex-m7-hard": str(B / "edk-arduino_portenta_h7_m7" / "flexmcu-edk"),
                        "cortex-m3": str(B / "edk" / "flexmcu-edk")}}}}


def profile_mcu(sim: Sim):
    p3flxm, Mcu, provision = _p3_import()
    B = P3 / "build"
    sim.start_cloud([DP])
    flash = sim.dir / "mcu" / "flash.img"
    flash.parent.mkdir(parents=True)
    provision(flash, B / "mcuboot" / "zephyr" / "zephyr.bin", B / "base-1.0.0.signed.bin")
    mcu = Mcu(sim.dir / "mcu", flash)
    mcu.start()
    sim.closers.append(mcu.stop)
    sim.mcu = mcu
    zport = free_port()
    pub = p3flxm.raw_public(p3flxm.load_private(P3 / "work" / "keys" / "module-ec-p256.pem")).hex()
    toks = sim.lp / "tokens"
    toks.mkdir(exist_ok=True)
    for d in (GP, MCU):
        (toks / d).write_text(sim.token(d) + "\n")
        (toks / d).chmod(0o600)
    sim.devices_cfg = [
        {"deviceId": GP, "role": "gateway", "onboardingTokenFile": str(toks / GP), "drivers": {}},
        {"deviceId": MCU, "parent": GP, "modelDevice": "portenta", "onboardingTokenFile": str(toks / MCU),
         "link": "smp/uart (QEMU mps2/an385 tcp)", "mcu": {"smp": {"host": "127.0.0.1", "port": mcu.port},
                                                           "modulePublicKey": pub},
         "relay": {"enabled": True, "maxRateHz": 10, "listen": f"tcp/127.0.0.1:{zport}", "connect": None}}]
    sim.write_engine_config(sim.engine_config({"model": str(DP), "deployment": "d_pick_lab"}))
    sim.write_catalog(mcu_catalog(sim))
    sim.vars.update(gp=GP, mcu=MCU, smp_host="127.0.0.1", smp_port=str(mcu.port), smp_tty="",
                    zenoh_connect=f"tcp/127.0.0.1:{zport}", zenoh_mode="peer", model_dir=str(DP),
                    base_version="1.0.0")

    def mcu_power_cycle(args):
        when_testing = "--when-testing" in args
        if when_testing:   # the test image is up (FLEXMCU_READY after `Swap type: test`) and not yet confirmed
            mark = mcu.mark()
            end = time.time() + 900
            while True:
                con = mcu.console_text(mark)
                i = con.find("Swap type: test")
                if i >= 0 and "FLEXMCU_READY" in con[i:]:
                    break
                if time.time() > end:
                    return 1, "MCUboot never booted a test image (no 'Swap type: test' + FLEXMCU_READY)"
                time.sleep(0.05)
            time.sleep(3.0)
        mcu.stop()
        time.sleep(1.0)
        mark = mcu.mark()
        mcu.start(wait_ready=False)
        try:
            mcu.wait_console("FLEXMCU_READY", 60, since=mark)
        except TimeoutError as e:
            return 1, str(e)
        con = mcu.console_text(mark)
        swap = next((l.strip() for l in con.splitlines() if "Swap type" in l), "no swap line")
        return 0, f"MCU power cycled{' during its test boot' if when_testing else ''}; boot: {swap}"

    sim.ctl_extra["mcu-power-cycle"] = mcu_power_cycle
    sim.start_engine([GP, MCU])


APP_LOCAL = "rb-unoq-board"


def profile_applab(sim: Sim):
    """UNO Q stand-in: P4's board simulator (real arduino-router + host-built sketches) and fake arduino-app-cli;
    the engine drives it in remote mode over a fake ssh/scp (as from the LattePanda) and, as a second device of the
    same host, in board-local mode (router health), the two placements of DA-11."""
    if str(P4) not in sys.path:
        sys.path.insert(0, str(P4))   # stand-ins only (pipeline, simboard, fake CLI)
    from flexp4 import hostbuild, pipeline
    from flexp4.simboard import SimBoard, have_toolchain

    from flexengine2.artefacts.oci import Layout

    if not have_toolchain():
        raise RuntimeError("App Lab board simulator needs probes/p4-applab-target/.ref/bin (arduino-router) and g++")
    cache = FLEXENGINE / "tests" / "work" / "applab" / "host-cache"     # git-ignored; seeded from the probe
    if not cache.exists() and hostbuild.CACHE.exists():
        shutil.copytree(hostbuild.CACHE, cache)
    hostbuild.CACHE = cache
    sim.start_cloud([pipeline.UC3_MODEL])
    built = pipeline.build_uc3(sim.dir / "pipeline")
    pipeline.wrap_all(Layout(sim.dir / "cloud" / "oci"), sim.keys["release"], built, health_timeout=15)
    board = SimBoard(sim.dir / "board", None)
    board.set_analog("A0", 200)
    board.set_analog("A1", 500)
    board.start()
    sim.closers.append(board.stop)
    fakebin = P4 / "tools" / "fakebin"
    env = {"ARDUINO_APP_CLI__DATA_DIR": str(sim.dir / "appcli"), "ARDUINO_APP_CLI__APPS_DIR": str(sim.dir / "ArduinoApps"),
           "FAKE_APPCLI_BOARD": "unoq", "FAKE_APPCLI_ROUTER": str(board.sock), "FAKE_APPCLI_UART": str(board.tty),
           "FAKE_APPCLI_SIM_DIR": str(board.sim), "FAKE_APPCLI_PYTHON": str(P4 / ".venv" / "bin" / "python"),
           "PATH": f"{fakebin}:{os.environ.get('PATH', '')}"}
    sim.engine_env_extra = env

    def stop_apps():
        try:
            st = json.loads((sim.dir / "appcli" / "fake-state.json").read_text())
        except (OSError, ValueError):
            return
        for pid in [a.get("pid") for a in st.get("apps", {}).values()] + [(st.get("mcu") or {}).get("pid")]:
            if pid:
                try:
                    os.killpg(int(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    sim.closers.insert(0, stop_apps)
    toks = sim.lp / "tokens"
    toks.mkdir(exist_ok=True)
    for d in (APP, APP_LOCAL):
        (toks / d).write_text(sim.token(d) + "\n")
        (toks / d).chmod(0o600)
    sim.devices_cfg = [
        {"deviceId": APP, "onboardingTokenFile": str(toks / APP), "drivers": {"native": False, "applab": {
            "board": "unoq", "cli": "arduino-app-cli", "healthVia": "logs", "ssh": "arduino@unoq-sim",
            "sshCmd": [str(fakebin / "fake-ssh")], "scpCmd": [str(fakebin / "fake-scp"), "-q"],
            "remoteStaging": str(sim.dir / "staging")}}},
        {"deviceId": APP_LOCAL, "onboardingTokenFile": str(toks / APP_LOCAL), "drivers": {"native": False, "applab": {
            "board": "unoq", "cli": str(fakebin / "arduino-app-cli"), "routerSocket": str(board.sock),
            "healthVia": "router"}}}]
    sim.write_engine_config(sim.engine_config())
    rel = {f"gh_{v.replace('.', '_')}": {"module": "uc3_greenhouse", "version": v, "driver": "applab",
                                          "parameters": {"ctl.open_above_c": 25.0}}
           for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0")}
    sim.write_catalog({"model": str(pipeline.UC3_MODEL), "releases": rel})
    appcli = "env " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items() if k != "PATH") + \
             f" {shlex.quote(str(fakebin / 'arduino-app-cli'))}"
    sim.vars.update(app=APP, app_local=APP_LOCAL, unoq="local", appcli=appcli, appcli_board=appcli,
                    unoq_board="local")
    sim.start_engine([APP, APP_LOCAL])
    sim.vars.update(fe_api_board=sim.vars["fe_api"], fe_api_board_token_file=sim.vars["fe_api_token_file"])


def plc_model_v2(d: Path) -> Path:
    """Copy of demo-pick whose CX bridge layout differs (one more interlock parameter): the CX bridge 1.1.0."""
    m = d / "model-v2"
    if not m.exists():
        shutil.copytree(DP, m)
        p = m / "modules" / "cell_interlock.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["spec"]["parameters"].append({"name": "door_debounce_ms", "type": "uint32", "default": 20, "min": 0,
                                          "max": 500})
        p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return m


def profile_plc(sim: Sim):
    """CX (TwinCAT ADS `Hello`) and Opta (Modbus hash registers) stand-ins of demo/edge/sim (read-only), provisioned
    with the 1.0 programs before the run, as D2-BRINGUP s5/s6 leave the bench."""
    edge = REPO / "demo" / "edge"
    if str(edge) not in sys.path:
        sys.path.insert(0, str(edge))   # the `sim` stand-in package only; flexedge is never imported
    from sim.ads_plc import AdsPlc
    from sim.modbus_plc import ModbusPlc

    import deskctl

    sim.start_cloud([DP])
    v2 = plc_model_v2(sim.dir)
    plc = lambda name, v, **kw: {"module": {"cx": "pick_cell_bridge", "opta": "stack_light"}[name], "version": v,
                                 "driver": "plc-verify", "build": {"kind": "flex-release", "targets": [
                                     {**({"name": "cx_bridge", "kind": "plc", "domain": "panda/cx"} if name == "cx"
                                         else {"name": "opta_stack_light", "kind": "plc", "device": "opta"}),
                                      "version": v}], **kw}}
    rel = {"cx_1_0": plc("cx", "1.0.0"), "opta_1_0": plc("opta", "1.0.0"),
           "cx_1_1": plc("cx", "1.1.0", model=str(v2)), "cx_1_1_1": plc("cx", "1.1.1", model=str(v2), force=True),
           "cell_1_0": {"module": "cell_controller", "version": "1.0.0", "driver": "native",
                        "activation": {"requires": ["plc:cx-bridge@@iface:cx_1_0"]}, "build": {"kind": "native"}},
           "cell_1_1": {"module": "cell_controller", "version": "1.1.0", "driver": "native",
                        "activation": {"requires": ["plc:cx-bridge@@iface:cx_1_1"]}, "build": {"kind": "native"}}}
    cat_path = sim.write_catalog({"model": str(DP), "releases": rel, "flexRelease": {"deployment": "d_pick_lab"}})
    cloud = deskctl.Cloud(sim.cloud.url, sim.vars["admin_token_file"])
    cat = deskctl.Catalog(str(cat_path))
    hashes = {n: deskctl.build_release(cat, cloud, n)["interfaceHash"] for n in ("cx_1_0", "opta_1_0")}
    end = time.time() + 30
    while True:   # AMS TCP 48898 is fixed (pyads' router protocol); a desk that just stopped may still hold it
        try:
            ads = AdsPlc(hashes["cx_1_0"]).start()
            break
        except OSError as e:
            if time.time() > end:
                raise RuntimeError(f"ADS stand-in: AMS port 48898 busy ({e}); another ADS stand-in runs here") from e
            time.sleep(1.0)
    sim.closers.append(ads.stop)
    mb_port = free_port()
    mb = ModbusPlc(hashes["opta_1_0"], port=mb_port).start()
    sim.closers.append(mb.stop)
    safe = sim.lp / "safe-state.json"     # the CX release carries the safety gateway: implicit safe-state interlock
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    tok = sim.lp / "tokens" / GP
    tok.parent.mkdir(exist_ok=True)
    tok.write_text(sim.token(GP) + "\n")
    tok.chmod(0o600)
    ads_conn = {"via": "ads", "amsNetId": "127.0.0.1.1.1", "ip": "127.0.0.1", "amsPort": 851,
                "localNetId": "127.0.0.1.1.2", "timeout": 1.0}
    sim.devices_cfg = [{"deviceId": GP, "role": "gateway", "onboardingTokenFile": str(tok), "safeStateFile": str(safe),
                        "drivers": {"native": True, "plc-verify": {"plcs": {
                            "panda/cx": ads_conn, "opta": {"via": "modbus", "host": "127.0.0.1", "port": mb_port,
                                                           "timeout": 1.0}}}}}]
    sim.write_engine_config(sim.engine_config({"model": str(DP), "deployment": "d_pick_lab"}))
    sim.vars.update(gp=GP, cx_net_id="127.0.0.1.1.1", cx_ip="127.0.0.1", cx_port="851",
                    ads_local_net_id="127.0.0.1.1.3", opta_host="127.0.0.1", opta_port=str(mb_port), opta_unit="1",
                    safe_state_file=str(safe))

    def plc_set(args):
        which, value = args[0], args[1]
        if value.startswith("@"):
            value = deskctl.iface_of(deskctl.Catalog(str(cat_path)), value[1:])
        elif value == "zero":
            value = "FLEX01_" + "00" * 32
        if which == "cx":
            ads.hash = value
        else:
            mb.set_hash(value)
        return 0, f"{which} runs the program with interface hash {value}"

    sim.ctl_extra["plc-set"] = plc_set
    sim.start_engine([GP])


def profile_rauc(sim: Sim):
    """An A/B device: the os-rauc driver over the fake `rauc` + boot simulator of the contract suite (read-only),
    with a machine that reboots when the driver asks (the engine dies, the bootloader picks a slot, the engine starts
    on the new slot with the same shared data partition)."""
    contract = FLEXENGINE / "tests" / "contract"
    if str(contract) not in sys.path:
        sys.path.insert(0, str(contract))
    from spi_harness.os_rauc import SimRauc

    sim.start_cloud([])
    lab = SimRauc(sim.dir / "ab", auto_boot=False).start()
    shared = sim.lp
    drv = lab.driver_cfg(shared, healthChecks=[lab.health_check_cmd()], bootTimeoutSeconds=120)
    tok = sim.lp / "tokens" / GP
    tok.parent.mkdir(exist_ok=True)
    tok.write_text(sim.token(GP) + "\n")
    tok.chmod(0o600)
    sim.devices_cfg = [{"deviceId": GP, "onboardingTokenFile": str(tok),
                        "drivers": {"os-rauc": drv, "native": False}}]
    sim.write_engine_config(sim.engine_config())
    gate = {"type": "os-rauc", "periodSeconds": 0.25, "successThreshold": 2, "timeoutSeconds": 6}
    soak = {"type": "os-rauc", "periodSeconds": 0.25, "successThreshold": 1, "timeoutSeconds": 30, "minSeconds": 3}

    def osrel(v, health=gate, **fake):
        return {"module": "os_image", "version": v, "driver": "os-rauc",
                "build": {"kind": "os-rauc", "fake": fake, "health": health}}

    sim.write_catalog({"releases": {"os_1_1": osrel("1.1.0"), "os_no_boot": osrel("2.0.0", bootable=False),
                                    "os_bad_health": osrel("2.1.0", healthy=False), "os_1_2": osrel("1.2.0"),
                                    "os_1_3": osrel("1.3.0", soak)}})
    svc = sim.make_engine_service()
    state = {"reboots": 0, "busy": threading.Lock()}

    def boot_machine(cut: bool):
        with state["busy"]:
            svc.kill(signal.SIGKILL, keep=False)
            if cut:
                lab.power_off()
            time.sleep(0.5)
            st = lab.boot()
            svc.start()
            return st

    def machine():   # the device reboots by itself when the driver asks (rebootCommand -> bootsim reboot)
        while not sim.stopped.is_set():
            if int(lab.show().get("rebootRequested", 0)) > 0:      # boot() resets the request counter
                state["reboots"] += 1
                st = boot_machine(cut=False)
                log(f"reboot #{state['reboots']}: booted slot {st.get('booted')}")
            time.sleep(0.2)
    threading.Thread(target=machine, daemon=True).start()

    def rauc_set(args):
        kv = {k: json.loads(v) for k, v in (a.split("=", 1) for a in args)}
        lab.set(**kv)
        return 0, f"boot simulator: {kv}"

    def actions() -> list[dict]:
        p = sim.lp / "data" / GP / "actions.jsonl"
        out = []
        for line in (p.read_text().splitlines() if p.exists() else []):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def power_cut(args):
        end = time.time() + 600
        if "--during-install" in args:
            while not lab.install_started():
                if time.time() > end:
                    return 1, "rauc install never started"
                time.sleep(0.1)
            time.sleep(1.0)
        elif "--before-mark-good" in args:
            n0 = len(actions())
            while not any(a.get("action") == "adopt" for a in actions()[n0:]):
                if time.time() > end:
                    return 1, "the engine never adopted the new slot"
                time.sleep(0.1)
        st = boot_machine(cut=True)
        lab.set(installDelay=0)
        return 0, f"power cut; booted slot {st.get('booted')} (tries {st['slots'][st['booted']].get('tries')})"

    sim.ctl_extra.update({"rauc-set": rauc_set, "rauc-power-cut": power_cut})
    sim.vars.update(gp=GP, rauc=str(lab.rauc), rauc_host="local", shared_dir=str(shared))
    sim.start_engine([GP])


def _wheelhouse() -> Path:
    """Offline wheelhouse of build-wheels.sh: $FLEX_HWTEST_WHEELHOUSE, this checkout's dist/wheelhouse, or
    the main checkout's (a git worktree shares the main checkout's build outputs)."""
    cands = [os.environ.get("FLEX_HWTEST_WHEELHOUSE"), FLEXENGINE / "dist" / "wheelhouse"]
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        cands.append(Path(r.stdout.strip()).parent / "flexengine" / "dist" / "wheelhouse")
    for c in cands:
        if c and Path(c).is_dir():
            return Path(c)
    raise RuntimeError("no offline wheelhouse: run packaging/build-wheels.sh or set FLEX_HWTEST_WHEELHOUSE")


def profile_install(sim: Sim):
    """EV-HW-01: a fresh 'LattePanda' (an empty --root), the real packaging/install.sh with the engine wheel built
    from this checkout and the offline wheelhouse; the installed venv's `flexengine` is the service."""
    sim.start_cloud([UC1])
    root = sim.dir / "root"
    dist = sim.dir / "dist"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(dist), str(FLEXENGINE)],
                       capture_output=True, text=True, timeout=600)
    wheels = sorted(dist.glob("flexengine-*-py3-none-any.whl"))
    if r.returncode != 0 or not wheels:
        raise RuntimeError(f"engine wheel build failed: {r.stderr[-1500:]}")
    wh = _wheelhouse()
    install = (f"bash {shlex.quote(str(FLEXENGINE / 'packaging' / 'install.sh'))} --root {shlex.quote(str(root))} "
               f"--wheel {shlex.quote(str(wheels[-1]))} --wheelhouse {shlex.quote(str(wh))} --device-id {GP} "
               f"--extras mcu,plc --python /usr/bin/python3")
    cfg_path = root / "etc" / "flexengine" / "engine.yaml"
    fe_bin = root / "opt" / "flexengine" / "bin" / "flexengine"
    sim.engine_cfg_path = cfg_path
    sim.engine_bin = [str(fe_bin)]
    sim.engine_env_extra = {"PYTHONPATH": ""}     # the installed venv only, not this checkout
    sim.write_catalog(lp_catalog(sim))
    fe = f"env FLEXENGINE_CONFIG={shlex.quote(str(cfg_path))} PYTHONPATH= {shlex.quote(str(fe_bin))}"
    sim.vars.update(token_path=str(root / "etc" / "flexengine" / "tokens" / GP), lp_sudo="")
    sim.vars.update(gp=GP, install=install, fe=fe, fe_enroll=f"{fe} enroll", fe_config=str(cfg_path),
                    fe_host="local", journal_source=f"file:{sim.lp / 'engine.log'}", usb_dir=str(sim.dir / "usb"),
                    suite=f"cd {shlex.quote(str(FLEXENGINE))} && {shlex.quote(sys.executable)} -m pytest -q "
                          f"-p no:cacheprovider --basetemp {shlex.quote(str(sim.dir / 'pt'))} "
                          "tests/unit/test_config.py tests/unit/test_cli.py tests/unit/test_local_api.py",
                    svc=f"{shlex.quote(sim.py)} {shlex.quote(str(HERE / 'simdesk.py'))} ctl --dir "
                        f"{shlex.quote(str(sim.dir))} svc")
    sim.api_port = free_port()

    def configure(args):
        """What the operator writes into /etc/flexengine/engine.yaml (EV-HW-01 step configure), for the sim root."""
        if not cfg_path.exists():
            return 1, f"{cfg_path} missing: run install.sh first"
        from flexengine2.identity import crypto

        tpl = cfg_path.read_text()
        tok = sim.secret("fe-api.token", "sim-local-api-token")
        cfg = {"cloud": {"apiUrl": f"https://127.0.0.1:{sim.shim.port}", "caFile": str(sim.pki["ca"]),
                         "cloudPublicKey": crypto.public_b64(sim.keys["cloud"]),
                         "releasePublicKeys": [crypto.public_b64(sim.keys["release"])],
                         "pollInterval": 0.5, "backoff": [0.2, 2.0], "httpTimeout": 5},
               "dataDir": str(root / "var" / "lib" / "flexengine"),
               "local": {"socket": str(sim.lp / "api.sock"),
                         "tcp": {"bind": f"127.0.0.1:{sim.api_port}", "tokenFile": str(tok)}},
               "logFormat": "json",
               "devices": [{"deviceId": GP, "role": "standalone-device",
                            "onboardingTokenFile": str(root / "etc" / "flexengine" / "tokens" / GP),
                            "safeStateFile": str(root / "var" / "lib" / "flexengine" / "safe-state.json"),
                            "drivers": {"native": True}}]}
        (root / "var" / "lib" / "flexengine" / "safe-state.json").write_text(
            json.dumps({"machine_parked": True, "operator_confirmed": True}))
        cfg_path.write_text("# configured for the simulated desk (template was installed by install.sh)\n"
                            + "".join(f"# {l}\n" for l in tpl.splitlines()[:3]) + yaml.safe_dump(cfg, sort_keys=False))
        sim.vars.update(fe_api=f"http://127.0.0.1:{sim.api_port}", fe_api_token_file=str(tok))
        return 0, f"wrote {cfg_path} (cloud via TLS {sim.shim.port}, local.tcp 127.0.0.1:{sim.api_port})"

    sim.ctl_extra["install-configure"] = configure
    tok = sim.secrets / "fe-api.token"
    sim.vars.update(fe_api=f"http://127.0.0.1:{sim.api_port}", fe_api_token_file=str(tok), lp_up_port=str(sim.api_port))


FAKES = FLEXENGINE / "tests" / "contract" / "spi_harness" / "fakes"
UC1_VENDORED = FLEXENGINE / "tests" / "vectors" / "framework" / "models" / "uc1"


def fake_runtime_bin(sim: Sim) -> Path:
    """docker (fakeoci, `docker compose ...` -> fakecompose) and systemctl (fakesystemctl) for the engine and the
    runbook's host checks; the shims carry their state directories, so every caller sees the same fake runtime."""
    bindir, state = sim.dir / "bin", sim.dir / "fakert"
    bindir.mkdir(exist_ok=True)
    env = {"FAKE_OCI_STATE": state / "oci", "FAKE_OCI_NAME": "docker", "FAKE_SYSTEMD_STATE": state / "systemd",
           "FAKE_SYSTEMD_UNITDIR": state / "units"}
    exports = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items())
    py = shlex.quote(sim.py)
    shims = {
        "docker": (f'if [ "$1" = compose ]; then shift; exec {py} {shlex.quote(str(FAKES / "fakecompose.py"))} "$@"; fi\n'
                   f'exec {py} {shlex.quote(str(FAKES / "fakeoci.py"))} "$@"\n'),
        "systemctl": f'exec {py} {shlex.quote(str(FAKES / "fakesystemctl.py"))} "$@"\n',
    }
    for name, body in shims.items():
        f = bindir / name
        f.write_text("#!/bin/sh\n" + exports + body)
        f.chmod(0o755)
    for d in ("oci", "systemd", "units"):
        (state / d).mkdir(parents=True, exist_ok=True)
    return bindir


def fe4_catalog(ports: dict) -> dict:
    """The EV-HW-12 release names of catalogs/lab-d2.yaml, built for the fake runtimes."""
    two = ["alice", "bob"]
    rel = {
        "fb_ctr": {"module": "perception_guard", "version": "4.0.0", "driver": "container",
                   "build": {"kind": "fake-container", "port": ports["ctr"], "approvers": two}},
        "fb_compose": {"module": "gige_camera", "version": "4.0.0", "driver": "container",
                       "build": {"kind": "fake-compose", "port": ports["cam"], "approvers": two}},
        "fb_nat": {"module": "detection_logger", "version": "4.0.0", "driver": "native", "build": {"kind": "native"}},
        "fb_gate": {"module": "detection_logger", "version": "4.1.0", "driver": "native",
                    "build": {"kind": "native", "failHealth": True}},
        "fb_src": {"module": "detection_logger", "version": "4.2.0", "driver": "native",
                   "build": {"kind": "native-source"}},
        "fb_src_bad": {"module": "detection_logger", "version": "4.3.0", "driver": "native",
                       "build": {"kind": "native-source", "fail": True}},
    }
    return {"model": str(UC1_VENDORED), "runtime": "docker", "releases": rel}


def profile_fe4(sim: Sim):
    sim.start_cloud([UC1_VENDORED])
    bindir = fake_runtime_bin(sim)
    tok = sim.lp / "tokens"
    tok.mkdir(exist_ok=True)
    (tok / GP).write_text(sim.token(GP) + "\n")
    (tok / GP).chmod(0o600)
    units = sim.dir / "fakert" / "units"
    safe = sim.lp / "safe-state.json"   # the containers' modules are safety-relevant: machine parked
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    sim.devices_cfg = [{"deviceId": GP, "role": "standalone-device", "onboardingTokenFile": str(tok / GP),
                        "safeStateFile": str(safe),
                        "drivers": {"container": {"runtime": "docker", "runtimes": ["compose"]},
                                    "native": {"runtime": "systemd", "runtimes": ["process"],
                                               "unitDir": str(units)}}}]
    sim.write_engine_config(sim.engine_config())
    sim.engine_env_extra = {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
    sim.write_catalog(fe4_catalog({"ctr": free_port(), "cam": free_port()}))
    sim.vars.update(gp=GP, rt=str(bindir / "docker"), sysctl=str(bindir / "systemctl"), safe_state_file=str(safe),
                    unit_dir=str(units), fe_data=str(sim.lp / "data"), fe_sudo="")

    def stop_fakes():   # runs after the engine stopped (closers run in reverse): the fake runtimes' workloads
        dk, sc = str(bindir / "docker"), str(bindir / "systemctl")
        ids = subprocess.run([dk, "ps", "-a", "-q"], capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run([dk, "rm", "-f", *ids], capture_output=True)
        for unit in sorted(units.glob("*.service")):
            subprocess.run([sc, "stop", unit.name], capture_output=True)
        for f in (sim.lp / "data").rglob("units.json"):   # process runtime children (own sessions)
            try:
                for u in json.loads(f.read_text()).values():
                    if isinstance(u, dict) and u.get("pid"):
                        try:
                            os.killpg(int(u["pid"]), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, ValueError):
                            pass
            except (OSError, ValueError):
                pass
    sim.closers.append(stop_fakes)
    sim.start_engine([GP])


PROFILES = {"lp": profile_lp, "fe4": profile_fe4, "mcu": profile_mcu, "applab": profile_applab, "plc": profile_plc,
            "rauc": profile_rauc, "install": profile_install}


def serve(d: Path, profile: str) -> int:
    sim = Sim(d, profile)
    signal.signal(signal.SIGTERM, lambda *a: sim.stopped.set())
    try:
        PROFILES[profile](sim)
        sim.finish()
        sim.serve_ctl()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        (sim.dir / "sim.json.error").write_text(f"{type(e).__name__}: {e}\n{sim.log_tail()}")
        return 1
    finally:
        sim.close()
        log("stopped")
    return 0


def ctl(d: Path, argv: list[str], timeout: float = 600) -> int:
    path = Path(d) / "ctl.sock"
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(str(path))
    except OSError as e:
        print(f"simdesk ctl: no desk at {path}: {e}", file=sys.stderr)
        return 1
    with c:
        c.sendall(json.dumps(argv).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    r = json.loads(buf.decode())
    if r["out"]:
        print(r["out"])
    return int(r["rc"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--profile", required=True, choices=sorted(PROFILES))
    p = sub.add_parser("ctl")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        return serve(a.dir, a.profile)
    return ctl(a.dir, a.args)


if __name__ == "__main__":
    sys.exit(main())

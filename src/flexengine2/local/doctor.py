"""`flexengine doctor [--repair [--yes]] [--json]`: host checks for an operator (EV-07).

Checks, in order: config valid; data dir and per-device dirs (exist, writable, not world-writable); device keys
(0600, owned by us); state.json (readable JSON object, known schemaVersion); local API socket (reachable, mode 0660)
and the TCP token file; cloud reachable (GET over the configured TLS settings; any HTTP answer counts); per configured
driver: docker/podman CLI, ankaios `ank`, arduino-app-cli (or ssh in remote mode), ADS/Modbus TCP ports, shm region,
SMP link. `--repair` moves a corrupt state.json aside, only with `--yes` (it makes the engine start from scratch: it
re-onboards and re-pulls the desired state).

    checks = run("engine.yaml")                      # [{"name", "status": ok|warn|fail|repaired|skip, "detail"}]
    print(format_checks(checks))
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

try:  # the engine's own rules (core/state.py, EV-05)
    from flexengine2.core.state import CORE_KEYS, SCHEMA_VERSION
except ImportError:  # pragma: no cover
    SCHEMA_VERSION, CORE_KEYS = 1, ("deviceId", "manifestVersion", "applied", "outbox", "seq")
ADS_PORT = 48898


def _tcp(host: str, port: int, timeout: float) -> str | None:
    """None when a TCP connection succeeds, else the error text."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return None
    except OSError as e:
        return e.strerror or str(e)


def _cmd(argv: list[str], timeout: float) -> tuple[bool, str]:
    if not shutil.which(argv[0]):
        return False, f"{argv[0]}: not found on PATH"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{' '.join(argv)}: {e}"
    out = (r.stdout or r.stderr).strip().splitlines()
    return r.returncode == 0, f"{' '.join(argv)}: " + (out[0][:120] if out else f"exit {r.returncode}")


class Doctor:
    def __init__(self, config_path: str, *, socket_path: str | None = None, repair: bool = False, yes: bool = False,
                 timeout: float = 3.0, tcp=_tcp, cmd=_cmd, cloud=None):
        self.config_path, self.socket_path = config_path, socket_path
        self.repair, self.yes, self.timeout = repair, yes, timeout
        self.tcp, self.cmd, self.cloud_probe = tcp, cmd, cloud or self._cloud_probe
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str):
        self.checks.append({"name": name, "status": status, "detail": detail})

    def run(self) -> list[dict]:
        from flexengine2.config.load import check, format_error, parse

        r = check(self.config_path)
        if not r["valid"]:
            self.add("config", "fail", f"{self.config_path}: " + "; ".join(format_error(e) for e in r["errors"][:5]))
            self._socket(None)
            return self.checks
        self.add("config", "ok", f"{self.config_path}: valid" + (f" ({len(r['warnings'])} note(s))" if r["warnings"] else ""))
        cfg = parse(self.config_path)
        from flexengine2.local.api import config_devices

        self._dir("dataDir", Path(cfg["dataDir"]))
        for d in config_devices(cfg):
            dd = Path(d["dataDir"])
            if Path(cfg["dataDir"]) / d["deviceId"] != dd:
                self._dir(f"{d['deviceId']}: dataDir", dd)
            self._key(d["deviceId"], dd / "device.key")
            self._state(d["deviceId"], dd / "state.json")
            for old in sorted(dd.glob("state.json.corrupt-*")):
                self.add(f"{d['deviceId']}: state copy", "warn", f"{old}: copy of an earlier corrupt state (keep or delete)")
        self._socket(cfg)
        self._cloud(cfg)
        for dev in cfg["devices"]:
            self._drivers(dev)
        return self.checks

    # ---- filesystem -----------------------------------------------------------------------------------------
    def _dir(self, name: str, d: Path):
        if not d.exists():
            self.add(name, "warn", f"{d}: missing (created at the first start)")
            return
        if not d.is_dir():
            self.add(name, "fail", f"{d}: not a directory")
            return
        mode = stat.S_IMODE(d.stat().st_mode)
        if mode & 0o002:
            self.add(name, "fail", f"{d}: mode {mode:04o} is world-writable")
        elif not os.access(d, os.W_OK | os.X_OK):
            self.add(name, "fail", f"{d}: not writable by uid {os.geteuid()}")
        else:
            self.add(name, "ok", f"{d}: mode {mode:04o}")

    def _key(self, dev: str, path: Path):
        from flexengine2.identity.keys import KeyPermissionError, check_private_file

        name = f"{dev}: device key"
        if not path.exists():
            self.add(name, "warn", f"{path}: missing (created at the first start)")
            return
        try:
            check_private_file(path)
            self.add(name, "ok", f"{path}: 0600")
        except (KeyPermissionError, OSError) as e:
            self.add(name, "fail", str(e))

    def _state(self, dev: str, path: Path):
        name = f"{dev}: state.json"
        if not path.exists():
            self.add(name, "ok", f"{path}: none yet (fresh device)")
            return
        problem = None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            v = st.get("schemaVersion", 0) if isinstance(st, dict) else None
            if not isinstance(st, dict):
                problem = f"not a JSON object ({type(st).__name__})"
            elif [k for k in CORE_KEYS if k not in st]:
                problem = "missing " + ", ".join(k for k in CORE_KEYS if k not in st)
            elif not isinstance(st["applied"], dict) or not isinstance(st["outbox"], list):
                problem = "applied/outbox have the wrong type"
            elif st.get("deviceId") != dev:
                problem = f"belongs to device {st.get('deviceId')!r}"
            elif not isinstance(v, int) or v < 0:
                problem = f"bad schemaVersion {v!r}"
            elif v > SCHEMA_VERSION:
                self.add(name, "fail", f"{path}: schemaVersion {v} is newer than this engine's {SCHEMA_VERSION}; run "
                                       "the newer engine or restore a backup (not repaired)")
                return
        except UnicodeDecodeError:
            problem = "not UTF-8"
        except ValueError as e:
            problem = f"not valid JSON ({e})"
        except OSError as e:
            self.add(name, "fail", f"{path}: unreadable ({e.strerror or e})")
            return
        if problem is None:
            sv = st.get("schemaVersion")
            self.add(name, "ok", f"{path}: manifestVersion {st.get('manifestVersion', 0)}"
                                 + (f", schemaVersion {sv}" if sv is not None else ""))
            return
        if self.repair and self.yes:
            dest = path.with_name(f"state.json.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
            os.replace(path, dest)
            self.add(name, "repaired", f"{path}: {problem}; moved to {dest.name} (the engine starts fresh: it "
                                       "re-onboards and re-pulls the desired state)")
        else:
            hint = "re-run with --repair --yes to move it aside" if not self.repair else "add --yes to confirm the move"
            self.add(name, "fail", f"{path}: {problem}; {hint}")

    # ---- local API ------------------------------------------------------------------------------------------
    def _socket(self, cfg: dict | None):
        from flexengine2.local.api import ApiClient, local_settings

        loc = local_settings(cfg)
        sp = self.socket_path or loc["socket_path"]
        if not os.path.exists(sp):
            self.add("local API", "warn", f"unix:{sp}: no socket (engine not running; the CLI reads the data dirs)")
        else:
            mode = stat.S_IMODE(os.lstat(sp).st_mode)
            try:
                r = ApiClient(sp, timeout=self.timeout).request("GET", "/v1/health")
                h = r.json()
                st = "ok" if r.status == 200 else "warn"
                self.add("local API", st, f"unix:{sp}: health ok={h.get('ok')} ticks={h.get('ticks')}"
                                          + (f"; mode {mode:04o} (expected 0660)" if mode != 0o660 else ""))
            except (OSError, ValueError) as e:
                self.add("local API", "warn", f"unix:{sp}: not reachable ({e}); stale socket of a stopped engine?")
        if loc["tcp_bind"]:
            from flexengine2.identity.secrets import read_secret_file

            try:
                read_secret_file(loc["token_file"])
                self.add("local API tcp", "ok", f"{loc['tcp_bind']}: token file {loc['token_file']} 0600")
            except (OSError, ValueError) as e:
                self.add("local API tcp", "fail", f"token file: {e}")

    # ---- cloud ----------------------------------------------------------------------------------------------
    def _cloud_probe(self, cloud: dict) -> tuple[str, str]:
        from flexengine2.cloud.client import CloudClient, Offline, Rejected, TlsConfigError, from_cfg_kwargs

        url = cloud["apiUrl"]
        try:
            c = CloudClient(url, "doctor", None, timeout=self.timeout, **from_cfg_kwargs(cloud))
        except (TlsConfigError, ValueError) as e:
            return "fail", f"{url}: {e}"
        try:
            st, _, _ = c.request("GET", "/", signed=False)
            return "ok", f"{url}: HTTP {st}"
        except Rejected as e:
            return "ok", f"{url}: reachable (HTTP {e.status})"
        except Offline as e:
            return "fail", f"{url}: {e}"

    def _cloud(self, cfg: dict):
        cloud = dict(cfg.get("cloud") or {})
        if not cloud.get("apiUrl") and cloud.get("stackInfo"):
            try:
                info = json.loads(Path(cloud["stackInfo"]).read_text())
                cloud["apiUrl"] = (info.get("internal") or {}).get("p2") or info["urls"]["p2"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.add("cloud", "fail", f"stackInfo {cloud['stackInfo']}: {e}")
                return
        if not cloud.get("apiUrl"):
            self.add("cloud", "fail", "no cloud.apiUrl")
            return
        self.add("cloud", *self.cloud_probe(cloud))

    # ---- drivers --------------------------------------------------------------------------------------------
    def _drivers(self, dev: dict):
        dev_id = dev["deviceId"]
        m = dev.get("mcu")
        if m:
            smp = m["smp"]
            if "tty" in smp:
                ok = os.path.exists(smp["tty"])
                self.add(f"{dev_id}: smp", "ok" if ok else "fail", f"{smp['tty']}: {'present' if ok else 'missing'}")
            else:
                err = self.tcp(smp["host"], smp["port"], self.timeout)
                self.add(f"{dev_id}: smp", "fail" if err else "ok",
                         f"tcp {smp['host']}:{smp['port']}: {err or 'connected'}")
        drv = dev.get("drivers") or {}
        c = drv.get("container")
        if c:
            if c["runtime"] == "ankaios":
                ok = os.access(c["ankBin"], os.X_OK)
                self.add(f"{dev_id}: container", "ok" if ok else "fail",
                         f"{c['ankBin']}: {'executable' if ok else 'missing or not executable'}")
            else:
                fmt = "{{.Server.Version}}" if c["runtime"] == "docker" else "{{.Version}}"
                ok, detail = self.cmd([c["runtime"], "version", "--format", fmt], self.timeout * 3)
                self.add(f"{dev_id}: container", "ok" if ok else "fail", detail)
        a = drv.get("applab")
        if a is not None:
            cli = a.get("cli", "arduino-app-cli")
            if a.get("ssh"):
                argv = list(a.get("sshCmd") or ["ssh", "-o", "BatchMode=yes"]) + [a["ssh"], cli, "version"]
            else:
                argv = [cli, "version"]
            ok, detail = self.cmd(argv, self.timeout * 3)
            self.add(f"{dev_id}: applab", "ok" if ok else "fail", detail)
        pv = drv.get("plc-verify")
        for name, conn in sorted(((pv or {}).get("plcs") or {}).items()):
            label = f"{dev_id}: plc {name}"
            if conn["via"] == "ads":
                err = self.tcp(conn["ip"], ADS_PORT, self.timeout)
                self.add(label, "fail" if err else "ok", f"ads {conn['ip']}:{ADS_PORT}: {err or 'connected'}")
            elif conn["via"] == "modbus":
                port = conn.get("port", 502)
                err = self.tcp(conn["host"], port, self.timeout)
                self.add(label, "fail" if err else "ok", f"modbus {conn['host']}:{port}: {err or 'connected'}")
            elif conn.get("path"):
                ok = os.path.exists(conn["path"])
                self.add(label, "ok" if ok else "warn", f"shm {conn['path']}: {'present' if ok else 'missing'}")
            else:
                self.add(label, "skip", "shm region named by the release (checked at install)")


def run(config_path: str, **kw) -> list[dict]:
    return Doctor(config_path, **kw).run()


TAGS = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]", "repaired": "[fix ]", "skip": "[skip]"}


def format_checks(checks: list[dict]) -> str:
    w = max([len(c["name"]) for c in checks] + [4])
    lines = [f"{TAGS[c['status']]} {c['name']:<{w}}  {c['detail']}" for c in checks]
    fails = sum(c["status"] == "fail" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    lines.append(f"{len(checks)} checks: {fails} failed, {warns} warning(s)")
    return "\n".join(lines)


def ok(checks: list[dict]) -> bool:
    return not any(c["status"] == "fail" for c in checks)

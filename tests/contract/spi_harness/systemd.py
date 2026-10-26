"""Harnesses: SystemdDriver (profile native) on a fake `systemctl` (push) and on real systemd (nightly).

push     fakes/fakesystemctl.py plays the service manager (Restart=, RestartSec, start limit, NRestarts) with real
         local processes; the driver runs in the test process.
nightly  an Ubuntu 24.04 container with systemd as PID 1 (the packaging tests' image, tests/packaging/fe_pkg.py);
         the worktree's src is mounted read-only and EVERY driver call runs inside the container through
         `docker exec … python3 systemd_rpc.py` on a NEW driver instance (so adopt-after-restart is exercised on
         every call). Marked docker + slow.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload
from flexengine2.drivers.native import systemd as systemd_mod
from flexengine2.drivers.native.systemd import SystemdDriver, unit_name

from .base import DEVICE, FAKES, Harness, Release, digest_of, failing_shim, shim, targz
from .process import APP, _markers, native_annotations

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parents[2]
HEALTH = {"type": "http", "path": "/healthz", "periodSeconds": 0.1, "successThreshold": 2, "timeoutSeconds": 10}


class _SystemdBase(Harness):
    driver_cls = SystemdDriver
    profile = "native"
    fail_delay = 1.5
    na = {}

    def _payload(self, version: str, **rel) -> tuple[str, str]:
        data = targz({"app.py": APP.encode(),
                      "release.json": json.dumps({"version": version, "ready": True, **rel}).encode()})
        p = self.payload_dir / f"native-{version}-{digest_of(data)[7:19]}.tar.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o644)
        return str(p), digest_of(data + version.encode())

    def annotations(self) -> dict:
        return native_annotations()

    def workload(self, version="1.0.0", dep_id=None):
        path, dg = self._payload(version)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_native", version=version, digest=dg,
                        payload_path=path, payload_annotations=self.annotations(), health=dict(HEALTH),
                        parameters={"greeting": "hello world"},
                        restart={"policy": "on-failure", "maxRetries": 2, "restartSec": 0.2})

    def bad_workload(self, dep_id=None):
        path, dg = self._payload("9.9.9-bad", ready=False, exit_after=self.fail_delay, exit_code=3)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_native", version="9.9.9-bad", digest=dg,
                        payload_path=path, payload_annotations=self.annotations(), health=dict(HEALTH),
                        restart={"policy": "never"})

    def refused_release(self):
        data = targz({"release.json": b"{}", "README": b"no entry point"})
        p = self.payload_dir / "native-refused.tar.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o644)
        rel = Release(str(p), {"dev.flex.profile": "native"}, {"health": HEALTH, "restartPolicy": {}})
        return rel, {"metadata": {"id": self.dep_id}, "x-flex-driver": "native",
                     "spec": {"applicationId": "ct_native", "parameters": {},
                              "deploymentProfile": {"components": [{"properties": {"revision": "1.0.0"}}]}}}


# ---- push tier: fake systemctl -----------------------------------------------------------------------------------
class FakeSystemdHarness(_SystemdBase):
    id = "native-systemd[fake-systemctl]"

    def setup(self):
        self.device = DEVICE + "-" + os.urandom(3).hex()
        self.payload_dir = self.tmp / "payloads"
        self.units = self.tmp / "units"
        self.state = self.tmp / "fakesystemd"
        self.bindir = self.tmp / "bin"
        shim(self.bindir, "systemctl", FAKES / "fakesystemctl.py",
             {"FAKE_SYSTEMD_STATE": self.state, "FAKE_SYSTEMD_UNITDIR": self.units})
        self.path_prepend(self.bindir)
        self.n_extract = 0
        real = systemd_mod._extract

        def counting(src, dst):  # instrumentation only: count payload unpacks
            self.n_extract += 1
            return real(src, dst)
        self.mp.setattr(systemd_mod, "_extract", counting)

    def make_driver(self):
        return SystemdDriver(self.device, self.data, unit_dir=self.units)

    def transfers(self):
        return self.n_extract

    def instances(self, dep_id):
        return len(_markers(self.device).get(dep_id, []))

    def _ctl(self, *args):
        return subprocess.run([str(self.bindir / "systemctl"), *args], capture_output=True, text=True, timeout=30)

    def destroy_out_of_band(self, dep_id):
        u = unit_name(self.device, dep_id)
        self._ctl("stop", u)
        (self.units / u).unlink(missing_ok=True)
        self._ctl("daemon-reload")
        return True

    def cleanup(self):
        for p in self.units.glob("*.service") if self.units.exists() else []:
            self._ctl("stop", p.name)
        for pids in _markers(self.device).values():
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        time.sleep(0.05)

    @contextmanager
    def unreachable(self):
        down = self.tmp / "down-bin"
        failing_shim(down, "systemctl", "Failed to connect to bus: No such file or directory")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PATH", f"{down}{os.pathsep}{os.environ['PATH']}")
            yield self.make_driver()


# ---- nightly tier: real systemd in a container --------------------------------------------------------------------
def _fe_pkg():
    p = str(FLEXENGINE / "tests" / "packaging")
    if p not in sys.path:
        sys.path.insert(0, p)
    import fe_pkg
    return fe_pkg


class SystemdBox:
    """Ubuntu 24.04 with systemd as PID 1 (privileged); /fe/src = this checkout's src (ro), /fe/harness = this
    directory (ro), `shared` = a host directory mounted rw at the same path (payloads, shims)."""

    def __init__(self, name: str | None = None, image: str | None = None):
        fe = _fe_pkg()
        self.name = name or f"fe-systemd-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.shared = Path(tempfile.mkdtemp(prefix="flex-systemd-ct-"))
        self.shared.chmod(0o755)
        img = image or fe.image()
        r = subprocess.run(["docker", "run", "-d", "--name", self.name, "--privileged", "--cgroupns=host",
                            "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw", "--tmpfs", "/run", "--tmpfs", "/run/lock",
                            "-v", f"{FLEXENGINE / 'src'}:/fe/src:ro", "-v", f"{HERE}:/fe/harness:ro",
                            "-v", f"{self.shared}:{self.shared}:rw", img, "/sbin/init"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"docker run: {r.stderr.strip()[-300:]}")
        if not self.wait_systemd():
            self.close()
            raise RuntimeError("systemd did not come up as PID 1 in the container")

    def wait_systemd(self, timeout: float = 30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            st = self.sh("systemctl is-system-running 2>/dev/null").stdout.strip()
            if st in ("running", "degraded"):
                return True
            time.sleep(0.25)
        return False

    def sh(self, script: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", self.name, "bash", "-c", script], capture_output=True, text=True,
                              timeout=timeout)

    def rpc(self, cfg: dict, method: str, args: list, env: dict | None = None, user: str | None = None):
        envs = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
        envs += ["-u", user] if user else []
        req = json.dumps({"cfg": cfg, "method": method, "args": args})
        p = subprocess.run(["docker", "exec", "-i", "-e", "PYTHONPATH=/fe/src", *envs, self.name, "python3", "-P",
                            "/fe/harness/systemd_rpc.py"], input=req, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError(f"rpc {method}: rc={p.returncode} {p.stderr.strip()[-800:]}")
        r = json.loads(p.stdout.strip().splitlines()[-1])
        if "error" in r:
            if r["error"] == "DriverError":
                raise DriverError(r["msg"])
            raise RuntimeError(f"{method}: {r['error']}: {r['msg']}")
        return r["ok"]

    def close(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=120)
        shutil.rmtree(self.shared, ignore_errors=True)


def polkit_image() -> str:
    """The packaging tests' image + polkitd (cached as a local tag): for the polkit rule of packaging/polkit/."""
    fe = _fe_pkg()
    tag = fe.IMAGE + "-polkit"
    if fe.sh(["docker", "image", "inspect", tag]).returncode == 0:
        return tag
    name = f"fe-polkit-prep-{os.getpid()}"
    vol = fe.ca_mount()
    script = (fe._apt_proxy_script() + "apt-get update -q >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get "
              "install -y -q --no-install-recommends polkitd >/dev/null && rm -f /etc/apt/apt.conf.d/99proxy")
    r = fe.sh(["docker", "run", "--name", name, "--network", "host", *vol, fe.image(), "bash", "-c", script],
              timeout=900)
    try:
        if r.returncode != 0:
            raise RuntimeError(f"apt install polkitd failed: {r.stderr.strip()[-300:]}")
        c = fe.sh(["docker", "commit", "--change", 'CMD ["/sbin/init"]', name, tag])
        if c.returncode != 0:
            raise RuntimeError(c.stderr)
    finally:
        fe.sh(["docker", "rm", "-f", name])
    return tag


_BOX: dict = {}


def shared_box() -> SystemdBox:
    if "box" not in _BOX:
        if "error" in _BOX:
            raise RuntimeError(_BOX["error"])
        try:
            _BOX["box"] = SystemdBox()
        except Exception as e:  # noqa: BLE001
            _BOX["error"] = str(e)
            raise
        atexit.register(_BOX["box"].close)
    return _BOX["box"]


def _dec(v):
    if isinstance(v, dict) and "__obs" in v:
        return Observed(**v["__obs"])
    if isinstance(v, dict):
        return {k: _dec(x) for k, x in v.items()}
    return v


def _enc(a):
    if isinstance(a, Workload):
        return {"__wl": a.to_dict()}
    if isinstance(a, Release) or hasattr(a, "payload_path"):
        return {"__rel": {"payload_path": a.payload_path, "payload_annotations": a.payload_annotations,
                          "descriptor": getattr(a, "descriptor", {})}}
    return a


class RemoteSystemdDriver(Driver):
    """SPI proxy: each call is one SystemdDriver call inside the box, on a fresh driver instance."""

    profile = SystemdDriver.profile
    name = SystemdDriver.name

    def __init__(self, box: SystemdBox, device: str, data: str, kw: dict | None = None, env: dict | None = None,
                 user: str | None = None):
        self.box, self.cfg, self.env, self.user = box, {"device": device, "data": data, "kw": kw or {}}, env, user

    def _call(self, method, *args):
        return _dec(self.box.rpc(self.cfg, method, [_enc(a) for a in args], self.env, self.user))

    def plan(self, current, desired):
        return self._call("plan", current, desired)

    def apply(self, current, desired):
        return self._call("apply", current, desired)

    def remove(self, dep_id):
        return self._call("remove", dep_id)

    def status(self, dep_id):
        return self._call("status", dep_id)

    def list(self):
        return self._call("list")

    def health(self, dep_id, probe):
        return self._call("health", dep_id, probe)

    def failed(self, dep_id):
        return self._call("failed", dep_id)

    def supervise(self):
        return self._call("supervise")

    def preflight(self, release, dep):
        return self._call("preflight", release, dep)

    def platform(self):
        return self._call("platform")

    def capabilities(self):
        return self._call("capabilities")


class RealSystemdHarness(_SystemdBase):
    id = "native-systemd[real-systemd]"

    def setup(self):
        if not shutil.which("docker"):
            pytest.skip("docker not installed")
        try:
            self.box = shared_box()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no systemd container: {e}")
        self.device = DEVICE + "-" + os.urandom(3).hex()
        self.payload_dir = self.box.shared / self.device
        self.cdata = f"/var/lib/flex-ct/{self.device}"

    def make_driver(self):
        return RemoteSystemdDriver(self.box, self.device, self.cdata)

    def annotations(self) -> dict:
        if "platform" not in _BOX:                       # the box's interpreter (EV-37 platform())
            _BOX["platform"] = self.make_driver().platform()
        return {"dev.flex.profile": "native", **_BOX["platform"]}

    def transfers(self):
        out = self.box.sh(f"cat {self.cdata}/.ct-extracts 2>/dev/null").stdout
        return len(out.split())

    def instances(self, dep_id):
        return len(self.box.rpc({"device": self.device}, "_markers", []).get(dep_id, []))

    def destroy_out_of_band(self, dep_id):
        u = unit_name(self.device, dep_id)
        self.box.sh(f"systemctl stop {u}; rm -f /etc/systemd/system/{u}; systemctl daemon-reload")
        return True

    def cleanup(self):
        if not hasattr(self, "device"):
            return                       # setup() skipped before it made a device: nothing to clean up
        pre = f"flex-{systemd_mod.sanitize(self.device)}-"
        self.box.sh(f"cd /etc/systemd/system && for u in {pre}*.service; do [ -e \"$u\" ] || continue; "
                    f"systemctl stop \"$u\"; systemctl disable \"$u\"; rm -f \"$u\"; done; systemctl daemon-reload; "
                    f"systemctl reset-failed; rm -rf {self.cdata}")
        shutil.rmtree(self.payload_dir, ignore_errors=True)

    @contextmanager
    def unreachable(self):
        down = self.box.shared / f"{self.device}-down-bin"
        failing_shim(down, "systemctl", "Failed to connect to bus: No such file or directory")
        down.chmod(0o755)
        yield RemoteSystemdDriver(self.box, self.device, self.cdata,
                                  env={"PATH": f"{down}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})


"""Native (container-free) process driver, systemd-like.

On a target this driver would write a systemd/Quadlet unit (or a BlueChi unit) and let systemd own
the process. The probe container has no systemd, so the driver plays systemd itself:
  * the release payload (tar.gz) is unpacked into <data>/native/<dep>/<digest>/ (read-only after unpack)
  * the process runs in its own session (it survives an engine crash, like a systemd unit)
  * a unit table (units.json, atomically written) holds pid, digest, port, restart counters
  * Restart=on-failure with RestartSec and StartLimitBurst, evaluated by supervise() every tick
  * after an engine restart the unit is re-adopted by pid + FLEX_UNIT marker in /proc/<pid>/environ
"""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from flexengine2.util import atomic_write_json, read_json
from flexengine2.drivers.base import Driver, DriverError, Observed, Workload, http_probe, wait_until


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProcessDriver(Driver):
    profile = "native"
    name = "process"

    def __init__(self, device_id: str, data_dir: Path):
        self.device = device_id
        self.root = Path(data_dir) / "native"
        self.root.mkdir(parents=True, exist_ok=True)
        self.units_path = self.root / "units.json"
        self.units: dict = read_json(self.units_path, {})

    def _save(self):
        atomic_write_json(self.units_path, self.units)

    def marker(self, dep_id: str) -> str:
        return f"{self.device}/{dep_id}"

    def _alive(self, u: dict | None) -> bool:
        if not u or not u.get("pid"):
            return False
        pid = u["pid"]
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(") ")[-1].startswith("Z"):
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                    return False
            env = Path(f"/proc/{pid}/environ").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return False
        return f"FLEX_UNIT={u['marker']}".encode() in env.split(b"\0")

    def _unpack(self, wl: Workload) -> Path:
        d = self.root / wl.dep_id / wl.digest.split(":")[1][:16]
        if not (d / ".complete").exists():
            d.mkdir(parents=True, exist_ok=True)
            with tarfile.open(wl.payload_path, "r:gz") as tf:
                tf.extractall(d, filter="data")
            (d / ".complete").write_text(wl.digest)
        return d

    def _spawn(self, dep_id: str):
        u = self.units[dep_id]
        log = open(Path(u["dir"]) / "app.log", "ab")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FLEX_UNIT": u["marker"],
               "FLEX_HEALTH_PORT": str(u["port"]), "PYTHONDONTWRITEBYTECODE": "1"}
        for k, v in sorted(u.get("parameters", {}).items()):
            env[f"FLEX_PARAM_{k.upper()}"] = str(v)
        p = subprocess.Popen([sys.executable, "app.py"], cwd=u["dir"], env=env, stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        u.update(pid=p.pid, startedAt=time.time())
        self._save()

    def _stop(self, u: dict | None, timeout: float = 5):
        if not self._alive(u):
            return
        pid = u["pid"]
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if not wait_until(lambda: not self._alive(u), timeout, 0.05):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            wait_until(lambda: not self._alive(u), 2, 0.05)

    # ---- SPI --------------------------------------------------------------------------------
    def plan(self, current, desired):
        steps = []
        if desired:
            steps.append(f"unpack {desired.module} {desired.version} -> native/{desired.dep_id}/")
        if current:
            steps.append(f"stop unit {self.marker(current.dep_id)} ({current.version})")
        if desired:
            steps.append(f"start unit {self.marker(desired.dep_id)} (Restart={desired.restart.get('policy')})")
        return steps

    def apply(self, current, desired):
        d = self._unpack(desired)
        old = self.units.get(desired.dep_id)
        self._stop(old)
        self.units[desired.dep_id] = {
            "marker": self.marker(desired.dep_id), "dir": str(d), "digest": desired.digest,
            "version": desired.version, "port": (old or {}).get("port") or _free_port(),
            "parameters": desired.parameters, "restart": desired.restart, "restarts": 0,
            "failures": [], "wantRunning": True, "pid": None}
        self._save()
        self._spawn(desired.dep_id)
        if not wait_until(lambda: self._alive(self.units[desired.dep_id]), 5, 0.05):
            raise DriverError("process did not start")

    def remove(self, dep_id):
        u = self.units.get(dep_id)
        if u:
            u["wantRunning"] = False
            self._stop(u)
            del self.units[dep_id]
            self._save()

    def status(self, dep_id):
        u = self.units.get(dep_id)
        if not u:
            return Observed(present=False)
        alive = self._alive(u)
        return Observed(present=True, running=alive, digest=u["digest"], version=u["version"],
                        restarts=u.get("restarts", 0), detail="running" if alive else "dead")

    def list(self):
        return {k: self.status(k) for k in self.units}

    def health(self, dep_id, probe):
        u = self.units.get(dep_id)
        if not self._alive(u):
            return False
        return http_probe("127.0.0.1", u["port"], probe.get("path", "/healthz"))

    def failed(self, dep_id):
        """Terminal failure: the unit is dead and systemd semantics will not restart it (Restart=no, or the
        start limit was hit)."""
        u = self.units.get(dep_id)
        if not u or self._alive(u):
            return None
        if u.get("restart", {}).get("policy", "on-failure") == "never":
            return "process exited (Restart=never)"
        if u.get("startLimitHit"):
            return f"process exited, start limit hit after {u.get('restarts', 0)} restarts"
        return None

    def supervise(self):
        """Restart=on-failure, RestartSec, StartLimitBurst within 60 s (systemd semantics, simplified)."""
        events = []
        for dep_id, u in self.units.items():
            if not u.get("wantRunning") or self._alive(u):
                continue
            pol = u.get("restart", {})
            if pol.get("policy", "on-failure") == "never":
                continue
            now = time.time()
            if u.get("nextStart"):
                if now < u["nextStart"]:
                    continue
                u["nextStart"] = None
                u["restarts"] = u.get("restarts", 0) + 1
                self._spawn(dep_id)
                events.append({"dep": dep_id, "event": "restarted", "restarts": u["restarts"]})
                continue
            if u.get("startLimitHit"):
                continue
            u["failures"] = [t for t in u.get("failures", []) if now - t < 60] + [now]
            if len(u["failures"]) > int(pol.get("maxRetries", 5)):
                u["startLimitHit"] = True
                events.append({"dep": dep_id, "event": "start-limit-hit"})
            else:
                u["nextStart"] = now + float(pol.get("restartSec", 0.5))
                events.append({"dep": dep_id, "event": "exited", "willRestartAt": u["nextStart"]})
            self._save()
        return events

    def capabilities(self):
        return {"profile": self.profile, "driver": self.name, "executor": "process (systemd-like, simulated)",
                "python": sys.version.split()[0]}

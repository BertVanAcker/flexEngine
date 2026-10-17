"""Native (container-free) process driver, systemd-like.

On a target this driver would write a systemd/Quadlet unit (or a BlueChi unit) and let systemd own
the process. The probe container has no systemd, so the driver plays systemd itself:
  * the release payload (tar.gz) is unpacked into <data>/native/<dep>/<digest>/ (read-only after unpack)
  * the process runs in its own session (it survives an engine crash, like a systemd unit)
  * a unit table (units.json, atomically written) holds pid, digest, port, restart counters
  * Restart=on-failure with RestartSec and StartLimitBurst, evaluated by supervise() every tick
  * after an engine restart the unit is re-adopted by pid + FLEX_UNIT marker in /proc/<pid>/environ (Linux); without
    procfs (macOS dev installs) by pid + the process start time `ps` reports, recorded at spawn. Workload processes
    the unit table does not know (EV-13 orphans) are found and reaped only on Linux (they need /proc)
  * EV-36/37 (drivers/native/pyenv.py): a payload with requirements.lock + wheels/ gets its own venv per digest
    (offline, hash-checked); its python runs app.py. unit.json's ExecStart is honoured ({python} = the payload
    interpreter; a relative program path is resolved in the payload tree, e.g. a Rust binary). platform() =
    {dev.flex.arch, dev.flex.pyabi}; health probe type "process" = the main process is alive
"""

from __future__ import annotations

import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from flexengine2.util import atomic_write_bytes, atomic_write_json, fsync_dir, fsync_tree, read_json
from flexengine2.drivers.base import Driver, DriverError, Observed, Workload, http_probe, wait_until
from flexengine2.drivers.native import pyenv


PROCFS = sys.platform.startswith("linux")   # /proc/<pid>/{stat,environ,cwd}; else `ps` (macOS, BSD)


def proc_start(pid: int) -> str | None:
    """Start time of a live, non-zombie process as `ps -o lstart=` prints it (C locale), None when there is none.
    With the pid it identifies a process across engine restarts where /proc is missing: a reused pid has another
    start time."""
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                           timeout=5, env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    stat, _, start = r.stdout.strip().partition(" ")
    if r.returncode != 0 or not stat or stat.startswith("Z"):
        return None
    return " ".join(start.split()) or None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ProcessDriver(Driver):
    profile = "native"
    name = "process"

    def __init__(self, device_id: str, data_dir: Path, python: str | None = None, venv_timeout: float = 600.0):
        self.device = device_id
        self.python = python or sys.executable          # payload interpreter (venvs are created with it)
        self.venv_timeout = float(venv_timeout)
        self.root = Path(data_dir) / "native"
        self.root.mkdir(parents=True, exist_ok=True)
        self.units_path = self.root / "units.json"
        self.units: dict = read_json(self.units_path, {})
        self._children: dict[int, subprocess.Popen] = {}   # processes this driver instance spawned (exact liveness)
        if PROCFS:
            self._reap_unknown()

    def _save(self):
        atomic_write_json(self.units_path, self.units)

    def marker(self, dep_id: str) -> str:
        return f"{self.device}/{dep_id}"

    def _alive(self, u: dict | None) -> bool:
        if not u or not u.get("pid"):
            return False
        pid = u["pid"]
        if not PROCFS:
            return self._alive_ps(u)
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

    def _alive_ps(self, u: dict) -> bool:
        """No procfs: a child of this driver instance by its Popen (which also reaps it), an adopted process (engine
        restarted) by pid + the start time recorded at spawn."""
        pid = u["pid"]
        p = self._children.get(pid)
        if p is not None:
            if p.poll() is None:
                return True
            del self._children[pid]
            return False
        return bool(u.get("startTime")) and proc_start(pid) == u["startTime"]

    def _unpack(self, wl: Workload) -> Path:
        """Unpack the payload once per digest. Durable before it is marked complete (EV-13): every extracted file and
        directory is fsynced, then `.complete` is written atomically (write + fsync + rename + dir fsync), so after
        a power cut the marker never stands next to files whose data did not reach the disk. A directory without
        the marker (interrupted unpack) is unpacked again from scratch."""
        d = self.root / wl.dep_id / wl.digest.split(":")[1][:16]
        if not (d / ".complete").exists():
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
            with tarfile.open(wl.payload_path, "r:gz") as tf:
                tf.extractall(d, filter="data")
            fsync_tree(d)
            fsync_dir(d.parent)
            atomic_write_bytes(d / ".complete", wl.digest.encode())
        return d

    def _prepare(self, wl: Workload) -> tuple[Path, list[str]]:
        """Unpack, then (EV-36) the payload's own venv; returns (tree, argv)."""
        d = self._unpack(wl)
        py = pyenv.ensure_venv(d, self.python, self.venv_timeout) or self.python
        argv = [py, "app.py"]
        uj = d / "unit.json"
        if uj.exists():
            from flexengine2.drivers.native.systemd import exec_argv

            try:
                argv = [a.replace("{python}", py) for a in exec_argv({p.name for p in d.iterdir()}, uj.read_bytes())]
            except DriverError:
                if not (d / "app.py").exists():
                    raise
                argv = [py, "app.py"]                  # an unusable unit.json next to app.py: as before
            if "/" in argv[0] and not os.path.isabs(argv[0]):
                argv[0] = str(d / argv[0])
        return d, argv

    def _spawn(self, dep_id: str):
        u = self.units[dep_id]
        # "spawning" first (EV-13): a kill between Popen and the save below leaves a process the unit table does not
        # know; the next driver start finds it by its FLEX_UNIT marker and adopts or kills it (_reap_unknown)
        u.update(pid=None, spawning=time.time())
        self._save()
        log = open(Path(u["dir"]) / "app.log", "ab")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FLEX_UNIT": u["marker"],
               "FLEX_HEALTH_PORT": str(u["port"]), "PYTHONDONTWRITEBYTECODE": "1"}
        for k, v in sorted(u.get("parameters", {}).items()):
            env[f"FLEX_PARAM_{k.upper()}"] = str(v)
        p = subprocess.Popen(u.get("argv") or [self.python, "app.py"], cwd=u["dir"], env=env, stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        u.pop("spawning", None)
        u.update(pid=p.pid, startedAt=time.time())
        if not PROCFS:
            self._children[p.pid] = p
            u["startTime"] = proc_start(p.pid)   # fork time: the same before and after exec
        self._save()
        # until the child has exec'ed, /proc/<pid>/environ still shows the engine's environment (no FLEX_UNIT marker),
        # so _alive() would call a just-started workload dead; wait for the exec (bounded)
        if PROCFS:
            wait_until(lambda: self._alive(u) or p.poll() is not None, 2.0, 0.01)

    def _marked_processes(self) -> dict[str, list[tuple[int, str]]]:
        """{dep_id: [(pid, cwd)]} of live process-group leaders owned by this user, running from a directory of this
        driver (<data>/native/...) and carrying this device's FLEX_UNIT marker in their environment (Linux /proc;
        empty elsewhere). The directory check keeps another data directory with the same device id out."""
        out: dict[str, list[tuple[int, str]]] = {}
        pre = f"FLEX_UNIT={self.device}/".encode()
        root = os.path.realpath(self.root) + os.sep
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return out
        uid = os.getuid()
        for pid in pids:
            try:
                if pid == os.getpid() or os.stat(f"/proc/{pid}").st_uid != uid or os.getpgid(pid) != pid:
                    continue
                with open(f"/proc/{pid}/stat") as f:
                    if f.read().split(") ")[-1].startswith("Z"):
                        continue
                cwd = os.readlink(f"/proc/{pid}/cwd")
                if not (cwd + os.sep).startswith(root):
                    continue
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except (OSError, ProcessLookupError):
                continue
            for e in env:
                if e.startswith(pre):
                    out.setdefault(e[len(pre):].decode("utf-8", "replace"), []).append((pid, cwd))
                    break
        return out

    def _reap_unknown(self):
        """At driver start: workload processes of this device the unit table does not know (an engine killed between
        Popen and the unit-table save, EV-13). A unit caught `spawning` whose process runs from the unit's directory
        adopts it; every other unknown process with this device's marker is killed (it would hold the port and run
        unsupervised)."""
        changed = False
        for dep_id, procs in self._marked_processes().items():
            u = self.units.get(dep_id)
            known = u.get("pid") if u and self._alive(u) else None
            for pid, cwd in procs:
                if pid == known:
                    continue
                if (u is not None and known is None and u.get("spawning") and u.get("wantRunning")
                        and cwd == os.path.realpath(u.get("dir", ""))):
                    u.pop("spawning", None)
                    u.update(pid=pid, startedAt=time.time())
                    known, changed = pid, True
                    continue
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for u in self.units.values():
            if u.pop("spawning", None) is not None:
                changed = True
        if changed:
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
        d, argv = self._prepare(desired)
        old = self.units.get(desired.dep_id)
        self._stop(old)
        self.units[desired.dep_id] = {
            "marker": self.marker(desired.dep_id), "dir": str(d), "digest": desired.digest,
            "version": desired.version, "port": (old or {}).get("port") or _free_port(),
            "parameters": desired.parameters, "restart": desired.restart, "restarts": 0, "argv": argv,
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
        if (probe or {}).get("type") == "process":       # no endpoint (e.g. a flexCommunicator node): alive
            return True
        return http_probe("127.0.0.1", u["port"], (probe or {}).get("path", "/healthz"))

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

    def platform(self):
        """EV-37: {dev.flex.arch, dev.flex.pyabi} of the payload interpreter (layer selection)."""
        return pyenv.platform_of(pyenv.interpreter_info(self.python))

    def preflight(self, release, dep):
        """EV-36/37: a layer built for another CPU/Python, wheels that do not fit, an incomplete requirements.lock."""
        info = pyenv.interpreter_info(self.python)      # DriverError: interpreter unusable -> deferred
        try:
            pyenv.check_tar(getattr(release, "payload_path", None), getattr(release, "payload_annotations", None),
                            info)
        except DriverError as e:
            return str(e)
        return None

    def capabilities(self):
        out = {"profile": self.profile, "driver": self.name, "executor": "process (systemd-like, simulated)",
               "python": sys.version.split()[0], "payloadPython": self.python}
        try:
            out["platform"] = self.platform()
        except DriverError:
            pass                                          # interpreter not runnable: no platform facts
        return out

"""Native (container-free) process driver, systemd-like.

On a target this driver would write a systemd/Quadlet unit (or a BlueChi unit) and let systemd own
the process. The probe container has no systemd, so the driver plays systemd itself:
  * the release payload (tar.gz) is unpacked into <data>/native/<dep>/<digest>/ (read-only after unpack)
  * the workload's writable directory is <data>/native/<dep>/data (FLEX_DATA_DIR, absolute, as the systemd driver
    gives it): created at install, kept across updates and rollbacks (it is not a payload tree). remove() leaves it,
    like the unpacked trees: the systemd driver serves the same <data>/native/<dep>/ directory, so a remove on this
    driver (e.g. an abandoned install on the other runtime, FE-4) must not delete what a systemd unit writes
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
  * UM-4 observation: resources() = CPU percent (utime + stime between two samples) and resident memory of each
    unit's main process (/proc, Linux; nothing elsewhere); logs() = the new complete lines of app.log (stdout) and
    app.stderr.log (stderr) in the unit's tree, cursor = {stream: [inode, offset]}, at most 64 KiB per stream a call
  * FE-4: stage (unpack + unit entry, not started), start, stop (the unit stays, wantRunning false: supervise leaves it
    alone) and build (drivers/buildstep.py native_build: build.command in the source tree -> payload)
"""

from __future__ import annotations

import io
import json
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
from flexengine2.drivers.base import (BuildRequest, BuildResult, Driver, DriverError, Observed, Workload, http_probe,
                                      tree_key, wait_until)
from flexengine2.drivers.native import pyenv
from flexengine2.obs.resources import ProcCpu, proc_rss


PROCFS = sys.platform.startswith("linux")
OUT_LOG, ERR_LOG = "app.log", "app.stderr.log"    # the workload's stdout / stderr, in its unpacked tree
LOG_READ_MAX = 64 * 1024                           # bytes per stream read by one logs() call   # /proc/<pid>/{stat,environ,cwd}; else `ps` (macOS, BSD)


def proc_start(pid: int) -> str | None:
    """Start time of a live, non-zombie process, None when there is none: field 22 of /proc/<pid>/stat (clock ticks
    since boot) on Linux, `ps -o lstart=` (C locale) elsewhere. It is the fork time, so exec does not change it; with
    the pid it identifies one process across engine restarts (a reused pid has another start time)."""
    if PROCFS:
        try:
            with open(f"/proc/{int(pid)}/stat") as f:
                fields = f.read().rsplit(") ", 1)[-1].split()
        except (OSError, ValueError):
            return None
        return None if not fields or fields[0] == "Z" else fields[19]
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
    runtime = "process"

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

    def data_dir(self, dep_id: str) -> Path:
        """The workload's writable directory (FLEX_DATA_DIR), next to its payload trees; absolute, because the
        workload runs with its unpacked tree as working directory."""
        return Path(os.path.abspath(self.root / dep_id / "data"))

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
        if f"FLEX_UNIT={u['marker']}".encode() in env.split(b"\0"):
            return True
        # no marker: another process under a reused pid -- or ours in the middle of an exec (e.g. a shell wrapper
        # exec'ing the real program), when /proc/<pid>/environ reads empty or stale for a moment. The start time
        # recorded at spawn tells them apart (exec keeps it); without one (older unit tables) the marker decides.
        return bool(u.get("startTime")) and proc_start(pid) == u["startTime"]

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
        d = self.root / wl.dep_id / tree_key(wl)
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
        err = open(Path(u["dir"]) / ERR_LOG, "ab")      # UM-4: stderr apart, so logs() can tell the streams
        data = self.data_dir(dep_id)
        data.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FLEX_UNIT": u["marker"],
               "FLEX_HEALTH_PORT": str(u["port"]), "FLEX_DATA_DIR": str(data), "PYTHONDONTWRITEBYTECODE": "1"}
        for k, v in sorted(u.get("parameters", {}).items()):
            env[f"FLEX_PARAM_{k.upper()}"] = str(v)
        p = subprocess.Popen(u.get("argv") or [self.python, "app.py"], cwd=u["dir"], env=env, stdout=log, stderr=err,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        err.close()
        u.pop("spawning", None)
        u.update(pid=p.pid, startedAt=time.time())
        u["startTime"] = proc_start(p.pid)       # fork time: the same before and after exec
        if not PROCFS:
            self._children[p.pid] = p
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

    def _install(self, desired: Workload, run: bool):
        d, argv = self._prepare(desired)
        self.data_dir(desired.dep_id).mkdir(parents=True, exist_ok=True)
        old = self.units.get(desired.dep_id)
        self._stop(old)
        self.units[desired.dep_id] = {
            "marker": self.marker(desired.dep_id), "dir": str(d), "digest": desired.digest,
            "version": desired.version, "port": (old or {}).get("port") or _free_port(),
            "parameters": desired.parameters, "restart": desired.restart, "restarts": 0, "argv": argv,
            "failures": [], "wantRunning": run, "pid": None}
        self._save()
        if run:
            self._spawn(desired.dep_id)
            if not wait_until(lambda: self._alive(self.units[desired.dep_id]), 5, 0.05):
                raise DriverError("process did not start")

    def apply(self, current, desired):
        self._install(desired, run=True)

    # FE-4 run state: a staged or stopped unit keeps its entry (tree, port, argv) with wantRunning false
    def stage(self, current, desired):
        u = self.units.get(desired.dep_id)
        if (u and u.get("digest") == desired.digest and not u.get("wantRunning") and not self._alive(u)
                and u.get("dir") == str(self.root / desired.dep_id / tree_key(desired))):
            return                                       # idempotent: already staged
        self._install(desired, run=False)

    def start(self, dep_id):
        u = self.units.get(dep_id)
        if not u:
            raise DriverError(f"process unit {dep_id} is not installed")
        if u.get("wantRunning") and self._alive(u):
            return
        u.update(wantRunning=True, restarts=0, failures=[], nextStart=None, startLimitHit=False)
        self._save()
        self._spawn(dep_id)
        if not wait_until(lambda: self._alive(self.units[dep_id]), 5, 0.05):
            raise DriverError("process did not start")

    def stop(self, dep_id):
        u = self.units.get(dep_id)
        if not u:
            return
        u.update(wantRunning=False, nextStart=None)
        self._save()
        self._stop(u)

    def build(self, desired, request: BuildRequest) -> BuildResult:
        from flexengine2.drivers.buildstep import native_build

        return native_build(request, self.profile)

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
                        restarts=u.get("restarts", 0),
                        detail="running" if alive else ("dead" if u.get("wantRunning", True) else "stopped"))

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
        if not u or self._alive(u) or not u.get("wantRunning", True):
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

    # ---- observation (UM-4) ----------------------------------------------------------------------------------
    def resources(self, dep_ids):
        """SPI: the main process of each running unit (Linux /proc); never raises."""
        if not PROCFS:
            return {}
        if not hasattr(self, "_cpu"):
            self._cpu = ProcCpu()
        units = dict(self.units)           # the engine thread may change the table meanwhile
        out = {}
        for dep in dep_ids:
            u = units.get(dep) or {}
            if not u.get("pid") or not self._alive(u):
                continue
            r = {}
            pct = self._cpu.percent(f"{dep}:{u['pid']}", self._cpu.cpu_seconds(u["pid"]))
            if pct is not None:
                r["cpuPercent"] = pct
            rss = proc_rss(u["pid"])
            if rss is not None:
                r["memoryBytes"] = rss
            if r:
                out[dep] = r
        self._cpu.forget({f"{d}:{(units.get(d) or {}).get('pid')}" for d in dep_ids})
        return out

    def logs(self, dep_id, cursor, limit):
        """SPI: new complete lines of the unit's stdout / stderr files; never raises."""
        u = dict(self.units).get(dep_id)
        try:
            pos = json.loads(cursor) if cursor else {}
        except ValueError:
            pos = {}
        pos = pos if isinstance(pos, dict) else {}
        if not u or not u.get("dir"):
            return {"lines": [], "cursor": cursor}
        lines, new = [], {}
        for stream, name in (("stdout", OUT_LOG), ("stderr", ERR_LOG)):
            try:
                with open(Path(u["dir"]) / name, "rb") as f:
                    st = os.fstat(f.fileno())
                    v = pos.get(stream)
                    if (isinstance(v, list) and len(v) == 2 and v[0] == st.st_ino and isinstance(v[1], int)
                            and 0 <= v[1] <= st.st_size):
                        start, cut = v[1], False
                    else:                                    # another file (new tree) or the first call: its tail
                        start = max(0, st.st_size - LOG_READ_MAX)
                        cut = start > 0
                    f.seek(start)
                    data = f.read(LOG_READ_MAX)
            except OSError:
                continue
            skip = 0
            if cut:                                          # the tail starts inside a line: drop that part
                i = data.find(b"\n")
                skip = i + 1 if i >= 0 else len(data)
            body = data[skip:]
            j = body.rfind(b"\n")
            complete = body[:j + 1] if j >= 0 else b""
            if not complete and len(data) == LOG_READ_MAX:   # one line longer than a read: take it as it is
                complete = body
            new[stream] = [st.st_ino, start + skip + len(complete)]
            for ln in complete.decode("utf-8", "replace").splitlines():
                lines.append({"stream": stream, "msg": ln})
        n = max(1, int(limit))
        out = {"lines": lines[-n:], "cursor": json.dumps(new, sort_keys=True) if new else cursor}
        if len(lines) > n:
            out["dropped"] = len(lines) - n
        return out

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

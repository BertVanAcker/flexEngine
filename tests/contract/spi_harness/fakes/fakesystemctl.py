"""Fake `systemctl` for the push tier of the driver contract suite (no systemd, no D-Bus needed).

It implements the CLI surface `SystemdDriver` uses, with systemd's semantics for simple/exec services:

    [--user] [--no-pager] show [-p A,B | --property=A] [--value] [unit]   no unit: manager (Version=); UM-4:
                                        CPUUsageNSec, MemoryCurrent, MemoryMax, TasksCurrent of the main process
    daemon-reload                       (re)load unit files from $FAKE_SYSTEMD_UNITDIR (+ linked paths)
    enable <unit|/abs/path> / disable <unit> / is-enabled <unit>
    start | restart | stop | reset-failed | is-active <unit>
    kill [--signal=SIG | -s SIG] <unit> (main process)

Unit files are parsed for ExecStart (quoted words, $$ and %% unescaped), Environment="K=V", WorkingDirectory,
Restart (no | on-failure | always), RestartSec (ms or s), StartLimitBurst / StartLimitIntervalSec. A detached
monitor process per started unit plays systemd's service manager: it runs ExecStart with exactly the unit's
Environment (plus systemd's default PATH, nothing from the caller), restarts it per Restart= / RestartSec=, counts
NRestarts and stops with Result=start-limit-hit when more than StartLimitBurst starts fall within the interval.
`reset-failed` clears the failed state and the start rate limit. Runtime state: $FAKE_SYSTEMD_STATE/run/<unit>.json
(written by the monitor), loaded units: loaded.json. Every call is appended to $FAKE_SYSTEMD_STATE/calls.jsonl.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

STATE = Path(os.environ.get("FAKE_SYSTEMD_STATE", "/tmp/fakesystemd"))
UNITDIR = Path(os.environ.get("FAKE_SYSTEMD_UNITDIR", "/tmp/fakesystemd/units"))
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


# ---- files -------------------------------------------------------------------------------------------------------
def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def run_path(unit: str) -> Path:
    return STATE / "run" / f"{unit}.json"


def loaded() -> dict:
    return _read(STATE / "loaded.json", {})


# ---- unit file parsing --------------------------------------------------------------------------------------------
def words(s: str) -> list[str]:
    out, cur, i, q, have = [], "", 0, False, False
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            cur += {"n": "\n", "r": "\r", "t": "\t"}.get(n, n)
            i += 2
            have = True
            continue
        if c == '"':
            q = not q
            have = True
        elif c.isspace() and not q:
            if have:
                out.append(cur)
            cur, have = "", False
        else:
            cur += c
            have = True
        i += 1
    if have:
        out.append(cur)
    return out


def _sec(v: str) -> float:
    v = v.strip()
    if v.endswith("ms"):
        return float(v[:-2]) / 1000
    if v.endswith("s"):
        return float(v[:-1])
    return float(v)


def parse_unit(path: Path) -> dict:
    cfg = {"env": {}, "restart": "no", "restartSec": 0.1, "burst": 5, "interval": 10.0, "cwd": None, "exec": None,
           "path": str(path)}
    for ln in path.read_text().splitlines():
        if "=" not in ln or ln.startswith(("#", ";", "[")):
            continue
        k, v = ln.split("=", 1)
        if k == "ExecStart":
            cfg["exec"] = [w.replace("$$", "$").replace("%%", "%") for w in words(v)]
        elif k == "Environment":
            for w in words(v):
                a, _, b = w.partition("=")
                cfg["env"][a] = b.replace("%%", "%")
        elif k == "WorkingDirectory":
            cfg["cwd"] = v.replace("%%", "%")
        elif k == "Restart":
            cfg["restart"] = v
        elif k == "RestartSec":
            cfg["restartSec"] = _sec(v)
        elif k == "StartLimitBurst":
            cfg["burst"] = int(v)
        elif k == "StartLimitIntervalSec":
            cfg["interval"] = _sec(v)
    return cfg


# ---- process helpers ---------------------------------------------------------------------------------------------
# macOS has no /proc; FAKE_SYSTEMD_NO_PROCFS=1 takes the same `ps` path on Linux (to check it there)
PROCFS = sys.platform.startswith("linux") and not os.environ.get("FAKE_SYSTEMD_NO_PROCFS")


def alive(pid) -> bool:
    """A live, non-zombie process: /proc on Linux, `ps` elsewhere (macOS has no procfs)."""
    if not pid:
        return False
    if PROCFS:
        try:
            st = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        return not st.split(") ")[-1].startswith("Z")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True,
                       env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
    return r.returncode == 0 and bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def runtime(unit: str) -> dict:
    return _read(run_path(unit), {"ActiveState": "inactive", "SubState": "dead", "Result": "success", "MainPID": 0,
                                  "NRestarts": 0, "ExecMainStatus": 0, "ExecMainExitTimestampMonotonic": 0,
                                  "starts": []})


# ---- the monitor (one per started unit): systemd's service state machine -----------------------------------------
def monitor(unit: str, token: str) -> None:
    cfg = loaded()[unit]
    st = runtime(unit)
    st.update(monitorPid=os.getpid(), token=token, NRestarts=0, Result="success", ExecMainStatus=0,
              ExecMainExitTimestampMonotonic=0)
    stopping = {"v": False}
    child = {"p": None}

    def on_term(*_):
        stopping["v"] = True
        p = child["p"]
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    signal.signal(signal.SIGTERM, on_term)

    def save(**kw):
        st.update(kw)
        _write(run_path(unit), st)

    first = True
    while True:
        now = time.time()
        starts = [t for t in st.get("starts", []) if now - t < cfg["interval"]]
        if len(starts) >= cfg["burst"]:
            save(ActiveState="failed", SubState="failed", Result="start-limit-hit", MainPID=0, starts=starts,
                 monitorPid=0)
            return
        st["starts"] = starts + [now]
        if not first:
            st["NRestarts"] = st.get("NRestarts", 0) + 1
        first = False
        env = {"PATH": DEFAULT_PATH, **cfg["env"]}
        log = open(STATE / "run" / f"{unit}.log", "ab")
        try:
            p = subprocess.Popen(cfg["exec"], cwd=cfg["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True)
        except OSError:
            log.close()
            save(ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus=203, MainPID=0,
                 ExecMainExitTimestampMonotonic=int(time.monotonic() * 1e6), monitorPid=0)
            return
        log.close()
        child["p"] = p
        save(ActiveState="active", SubState="running", MainPID=p.pid)
        if stopping["v"]:
            on_term()
        deadline = None
        while p.poll() is None:
            if stopping["v"]:
                deadline = deadline or time.time() + 10
                if time.time() > deadline:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.02)
        rc = p.returncode
        try:
            os.killpg(p.pid, signal.SIGKILL)   # KillMode=control-group: nothing of the unit outlives its main process
        except (ProcessLookupError, PermissionError):
            pass
        code = rc if rc >= 0 else -rc
        st.update(ExecMainStatus=code, ExecMainExitTimestampMonotonic=int(time.monotonic() * 1e6), MainPID=0)
        if stopping["v"]:
            save(ActiveState="inactive", SubState="dead", Result="success", monitorPid=0)
            return
        clean = rc == 0
        again = cfg["restart"] == "always" or (cfg["restart"] == "on-failure" and not clean)
        if not again:
            if clean:
                save(ActiveState="inactive", SubState="dead", Result="success", monitorPid=0)
            else:
                save(ActiveState="failed", SubState="failed", Result="exit-code" if rc > 0 else "signal",
                     monitorPid=0)
            return
        save(ActiveState="activating", SubState="auto-restart")
        end = time.time() + cfg["restartSec"]
        while time.time() < end:
            if stopping["v"]:
                save(ActiveState="inactive", SubState="dead", Result="success", monitorPid=0)
                return
            time.sleep(0.02)


# ---- CLI commands ------------------------------------------------------------------------------------------------
def fail(msg: str, rc: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(rc)


def daemon_reload():
    links = _read(STATE / "links.json", {})
    out = {}
    for p in sorted(UNITDIR.glob("*.service")) if UNITDIR.exists() else []:
        out[p.name] = parse_unit(p)
    for name, path in links.items():
        if name not in out and Path(path).exists():
            out[name] = parse_unit(Path(path))
    _write(STATE / "loaded.json", out)


def stop(unit: str) -> None:
    st = runtime(unit)
    mp = st.get("monitorPid")
    if alive(mp):
        os.kill(mp, signal.SIGTERM)
        end = time.time() + 15
        while alive(mp) and time.time() < end:
            time.sleep(0.02)


def start(unit: str) -> None:
    if unit not in loaded():
        fail(f"Failed to start {unit}: Unit {unit} not found.", 5)
    st = runtime(unit)
    if alive(st.get("monitorPid")):
        return
    now = time.time()
    cfg = loaded()[unit]
    if len([t for t in st.get("starts", []) if now - t < cfg["interval"]]) >= cfg["burst"]:
        _write(run_path(unit), {**st, "ActiveState": "failed", "SubState": "failed", "Result": "start-limit-hit"})
        fail(f"Job for {unit} failed because start of the service was attempted too often.")
    token = uuid.uuid4().hex
    (STATE / "run").mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "__monitor", unit, token], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    end = time.time() + 10
    while time.time() < end:
        st = runtime(unit)
        if st.get("token") == token and st.get("ActiveState") in ("active", "failed", "inactive"):
            if st["ActiveState"] == "failed":
                fail(f"Job for {unit} failed because the control process exited with error code.")
            return
        time.sleep(0.02)
    fail(f"Job for {unit} timed out.")


def _accounting(pid, active: str) -> dict:
    """UM-4: CPUUsageNSec / MemoryCurrent / TasksCurrent of the main process (systemd: of the control group)."""
    if active != "active" or not str(pid or "").isdigit() or not int(pid):
        return {"CPUUsageNSec": "[not set]", "MemoryCurrent": "[not set]", "TasksCurrent": "[not set]",
                "MemoryMax": "infinity"}
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        ns = (int(f[11]) + int(f[12])) * 10**9 // os.sysconf("SC_CLK_TCK")
        rss = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return {}
    return {"CPUUsageNSec": str(ns), "MemoryCurrent": str(rss), "TasksCurrent": "1", "MemoryMax": "infinity"}


def show(unit: str | None, props: list[str], value: bool) -> None:
    if unit is None:
        vals = {"Version": "255-fake", "SystemState": "running"}
    else:
        st = runtime(unit)
        vals = {k: st.get(k, "") for k in ("ActiveState", "SubState", "Result", "MainPID", "NRestarts",
                                            "ExecMainStatus", "ExecMainExitTimestampMonotonic")}
        if vals["ActiveState"] in ("active", "activating") and not alive(st.get("monitorPid")):
            vals.update(ActiveState="failed", SubState="failed", Result="signal", MainPID=0)  # monitor died
        vals.update(_accounting(vals.get("MainPID"), vals["ActiveState"]))
        vals["LoadState"] = "loaded" if unit in loaded() else "not-found"
        vals["Id"] = unit
        vals["UnitFileState"] = "enabled" if unit in _read(STATE / "enabled.json", []) else "disabled"
    for k in props or sorted(vals):
        v = vals.get(k, "")
        print(v if value else f"{k}={v}")


def main(argv: list[str]) -> int:
    if argv[:1] == ["__monitor"]:
        monitor(argv[1], argv[2])
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "calls.jsonl", "a") as f:
        f.write(json.dumps(argv) + "\n")
    if os.environ.get("FAKE_SYSTEMD_DOWN"):
        fail("Failed to connect to bus: No such file or directory")
    props, value, sig, pos = [], False, "SIGTERM", []
    it = iter(argv)
    for a in it:
        if a in ("--user", "--no-pager", "--quiet", "-q", "--no-block"):
            continue
        if a == "--value":
            value = True
        elif a in ("-p", "--property"):
            props += next(it).split(",")
        elif a.startswith("--property="):
            props += a.split("=", 1)[1].split(",")
        elif a.startswith("--signal="):
            sig = a.split("=", 1)[1]
        elif a == "-s":
            sig = next(it)
        else:
            pos.append(a)
    if not pos:
        fail("missing command")
    cmd, args = pos[0], pos[1:]
    if cmd == "show":
        show(args[0] if args else None, props, value)
    elif cmd == "daemon-reload":
        daemon_reload()
    elif cmd == "enable":
        en = set(_read(STATE / "enabled.json", []))
        links = _read(STATE / "links.json", {})
        for a in args:
            name = Path(a).name
            if a.startswith("/"):
                links[name] = a
            elif name not in loaded():
                fail(f"Failed to enable unit: Unit file {name} does not exist.")
            en.add(name)
        _write(STATE / "links.json", links)
        _write(STATE / "enabled.json", sorted(en))
    elif cmd == "link":
        links = _read(STATE / "links.json", {})
        for a in args:
            links[Path(a).name] = a
        _write(STATE / "links.json", links)
    elif cmd == "disable":
        en = set(_read(STATE / "enabled.json", []))
        links = _read(STATE / "links.json", {})
        for a in args:
            en.discard(Path(a).name)
            links.pop(Path(a).name, None)
        _write(STATE / "links.json", links)
        _write(STATE / "enabled.json", sorted(en))
    elif cmd == "is-enabled":
        en = args[0] in _read(STATE / "enabled.json", [])
        print("enabled" if en else "disabled")
        return 0 if en else 1
    elif cmd == "is-active":
        a = runtime(args[0]).get("ActiveState", "inactive")
        print(a)
        return 0 if a == "active" else 3
    elif cmd == "start":
        for u in args:
            start(u)
    elif cmd == "restart":
        for u in args:
            stop(u)
            start(u)
    elif cmd == "stop":
        for u in args:
            if u not in loaded() and not alive(runtime(u).get("monitorPid")):
                fail(f"Failed to stop {u}: Unit {u} not loaded.", 5)
            stop(u)
    elif cmd == "reset-failed":
        for u in args:
            st = runtime(u)
            if not alive(st.get("monitorPid")):
                if st.get("ActiveState") == "failed":
                    st.update(ActiveState="inactive", SubState="dead", Result="success")
                st["starts"] = []
                _write(run_path(u), st)
    elif cmd == "kill":
        pid = runtime(args[0]).get("MainPID")
        if alive(pid):
            os.kill(pid, getattr(signal, sig if sig.startswith("SIG") else f"SIG{sig}"))
    else:
        fail(f"fake systemctl: unsupported command {cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

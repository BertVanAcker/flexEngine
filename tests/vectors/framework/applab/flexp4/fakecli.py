"""Fake `arduino-app-cli` for the probe (no board here). It emulates the commands and outputs the applab
driver uses, as read from arduino-app-cli source (cmd/arduino-app-cli/app/*.go, internal/orchestrator,
cmd/feedback; HEAD 2026-10-15):

  --format text|json                     global flag; json results on stdout; errors as {"error": ...} on
                                         stderr with exit code 1 (ErrGeneric) or 7 (ErrBadArgument)
  version
  app list [--releases|--all]            {"apps": [AppInfo], "brokenApps": []}  (catalog, no status)
  app ps [--all]                         {"apps": [AppInfo + status]}  running|starting|stopping|stopped|failed
  app install <file.ard> --yes           {"name","release","target","app_id","path"}; refuses: not .ard, not a
                                         single-rooted gzip tar, no release.yaml, newer schema, other board,
                                         already installed (ErrAppAlreadyExists)
  app start <id>                         one app at a time: refuses if another app is running/starting, stops
                                         another *failed* app; flashes the MCU (release: prebuilt firmware;
                                         user app: compiles the sketch), then starts the Python part
  app stop <id>                          stops the Python part and resets the MCU (the sketch restarts)
  app logs <id> [--tail N]               "[<container>] <line>"
  app destroy <id>                       stop + remove
  ids: "user:<name>", "release:<name>" or a path; JSON ids are base64url("release:<name>") without padding.

What is simulated, and how: the Python part runs as a local process (not a Compose project) with
APP_SOCKET pointing at the (real) arduino-router; "flashing" restarts a host-built sketch process that opens
the pty the router uses as the MCU UART. Docker, bricks, models and the venv are not emulated.

Environment: ARDUINO_APP_CLI__DATA_DIR, ARDUINO_APP_CLI__APPS_DIR, ARDUINO_APP_CLI__RELEASES_DIR (real names),
FAKE_APPCLI_BOARD (unoq), FAKE_APPCLI_ROUTER (router socket), FAKE_APPCLI_UART (pty link the router opens),
FAKE_APPCLI_SIM_DIR (simulated analog inputs), FAKE_APPCLI_PYTHON (interpreter for main.py).
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import yaml

ERR_GENERIC, ERR_BAD_ARGUMENT = 1, 7
VERSION = "0.14.0-fake-p4"


class Fatal(Exception):
    def __init__(self, msg: str, code: int = ERR_GENERIC):
        super().__init__(msg)
        self.code = code


class Cfg:
    def __init__(self):
        self.data = Path(os.environ.get("ARDUINO_APP_CLI__DATA_DIR", "/var/lib/arduino-app-cli"))
        self.apps = Path(os.environ.get("ARDUINO_APP_CLI__APPS_DIR", str(Path.home() / "ArduinoApps")))
        self.releases = Path(os.environ.get("ARDUINO_APP_CLI__RELEASES_DIR", str(self.data / "releases")))
        self.board = os.environ.get("FAKE_APPCLI_BOARD", "unoq")
        self.router = os.environ.get("FAKE_APPCLI_ROUTER", "/var/run/arduino-router.sock")
        self.uart = os.environ.get("FAKE_APPCLI_UART", "")
        self.sim = os.environ.get("FAKE_APPCLI_SIM_DIR", "")
        self.python = os.environ.get("FAKE_APPCLI_PYTHON", sys.executable)
        for d in (self.data, self.apps, self.releases):
            d.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "fake-state.json"


# ---- state (one lock for the whole CLI invocation, like the orchestrator's mutex) -----------------

class State:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.lockf = open(cfg.data / "fake-state.lock", "w")
        fcntl.flock(self.lockf, fcntl.LOCK_EX)
        try:
            self.s = json.loads(cfg.state_path.read_text())
        except (OSError, ValueError):
            self.s = {"mcu": None, "apps": {}}

    def save(self):
        tmp = self.cfg.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1))
        os.replace(tmp, self.cfg.state_path)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return not f.read().split(") ")[-1].startswith("Z")
    except OSError:
        return False


def _kill_group(pid: int | None, timeout: float = 5.0):
    if not _alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.time() + timeout
    while _alive(pid) and time.time() < end:
        time.sleep(0.05)
    if _alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---- apps ---------------------------------------------------------------------------------------

def app_id(cfg: Cfg, path: Path) -> str:
    path = path.resolve()
    if str(path).startswith(str(cfg.releases.resolve())):
        raw = "release:" + str(path.relative_to(cfg.releases.resolve()))
    elif str(path).startswith(str(cfg.apps.resolve())):
        raw = "user:" + str(path.relative_to(cfg.apps.resolve()))
    else:
        raw = str(path)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def alias(encoded: str) -> str:
    pad = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + pad).decode()


def resolve(cfg: Cfg, ident: str) -> Path:
    prefix, _, rest = ident.partition(":")
    if rest and prefix == "user":
        p = cfg.apps / rest
    elif rest and prefix == "release":
        p = cfg.releases / rest
    else:
        p = Path(ident)
    if not (p / "app.yaml").exists():
        raise Fatal(f"invalid app path: {ident}", ERR_BAD_ARGUMENT)
    return p.resolve()


def load_app(p: Path) -> dict:
    try:
        d = yaml.safe_load((p / "app.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise Fatal(f"invalid app: {e}")
    if not (p / "python" / "main.py").exists():
        raise Fatal("invalid app: missing python/main.py")
    rel = None
    if (p / "release.yaml").exists():
        rel = yaml.safe_load((p / "release.yaml").read_text())
    return {"name": d.get("name") or p.name, "icon": d.get("icon", ""), "description": d.get("description", ""),
            "release": rel}


def status_of(st: State, p: Path) -> str:
    a = st.s["apps"].get(str(p))
    if not a:
        return "stopped"
    if a.get("stopped"):
        return "stopped"
    if _alive(a.get("pid")):
        return "running"
    ec = p / ".cache" / "exitcode"
    if ec.exists():
        code = ec.read_text().strip()
        return "stopped" if code == "0" else "failed"
    return "stopped"


def app_info(cfg: Cfg, st: State | None, p: Path, with_status: bool) -> dict:
    a = load_app(p)
    rel = a["release"]
    info = {"id": app_id(cfg, p), "name": a["name"], "description": a["description"], "icon": a["icon"],
            "example": False, "default": False, "release": rel is not None}
    if rel:
        info.update(release_id=p.name, release_label=rel.get("release_label", ""), created_at=rel.get("created_at"))
    if with_status and st is not None:
        info["status"] = status_of(st, p)
    return info


def all_apps(cfg: Cfg, releases: bool = True, user: bool = True) -> list[Path]:
    out = []
    for base, want in ((cfg.apps, user), (cfg.releases, releases)):
        if want:
            out += [d.resolve() for d in sorted(base.iterdir()) if (d / "app.yaml").exists()]
    return out


# ---- MCU ("flash" = restart the host-built sketch on a new pty) ------------------------------------

def flash(cfg: Cfg, st: State, firmware: Path, app: Path):
    mcu = st.s.get("mcu") or {}
    _kill_group(mcu.get("pid"))
    if not cfg.uart:
        raise Fatal("no MCU UART configured (FAKE_APPCLI_UART)")
    env = {**os.environ, "FLEX_PTY_LINK": cfg.uart}
    if cfg.sim:
        env["FLEX_SIM_DIR"] = cfg.sim
    log = open(cfg.data / "mcu.log", "ab")
    p = subprocess.Popen([str(firmware)], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    st.s["mcu"] = {"pid": p.pid, "firmware": str(firmware), "app": str(app), "flashedAt": time.time()}
    st.save()
    end = time.time() + 5
    while time.time() < end and not os.path.lexists(cfg.uart):
        time.sleep(0.02)


def firmware_for(cfg: Cfg, p: Path, a: dict) -> Path | None:
    if not (p / "sketch").exists():
        return None
    if a["release"] is not None:
        fw = p / ".cache" / "flex-host-mcu"
        if not fw.exists():
            raise Fatal(f"failed to upload the sketch: the release has no firmware for this board")
        return fw
    # an editable app: compile the sketch (arduino-cli compile on the board; the host build here)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from flexp4 import hostbuild
    return hostbuild.build(p / "sketch", p / ".cache" / "flex-host-mcu")


# ---- commands ------------------------------------------------------------------------------------

def cmd_install(cfg: Cfg, args: list[str]) -> dict:
    files = [a for a in args if not a.startswith("--")]
    if not files:
        raise Fatal("missing release_path", ERR_BAD_ARGUMENT)
    if "--yes" not in args:
        raise Fatal("Install canceled: confirmation required (pass --yes)")
    ard = Path(files[0])
    if not ard.exists():
        raise Fatal(f"bad request: {ard} not found", ERR_BAD_ARGUMENT)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from flexp4.ardrelease import ReleaseError, read_ard
    try:
        info = read_ard(ard)
    except ReleaseError as e:
        raise Fatal(f"bad request: {e}", ERR_BAD_ARGUMENT)
    m, rel = info["manifest"], info["releaseName"]
    if m["target"] != cfg.board:
        raise Fatal(f"bad request: the release is built for {m['target']}, this board is a {cfg.board}",
                    ERR_BAD_ARGUMENT)
    dest = cfg.releases / rel
    if dest.exists():
        raise Fatal(f"app already exists: {rel} is already installed")
    stage = cfg.releases / f".staging-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    with tarfile.open(ard, "r:gz") as tf:
        for mem in tf.getmembers():
            inner = "/".join(Path(mem.name).parts[1:])
            top, _, rest = inner.partition("/")
            target = {"src": rest, "prebuild": f".cache/{rest}", "data": f"data/{rest}",
                      "release.yaml": "release.yaml"}.get(top)
            if target is None or not mem.isfile() or not target.strip("/"):
                continue
            out = stage / target
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tf.extractfile(mem).read())
            out.chmod(mem.mode & 0o777)
    (stage / "data").mkdir(exist_ok=True)
    try:
        load_app(stage)
    except Fatal as e:
        shutil.rmtree(stage)
        raise Fatal(f"bad request: the release does not hold a valid app: {e}", ERR_BAD_ARGUMENT)
    stage.rename(dest)
    return {"name": m["name"], "release": rel, "target": m["target"], "app_id": app_id(cfg, dest),
            "path": str(dest.resolve())}


def cmd_start(cfg: Cfg, st: State, p: Path) -> dict:
    a = load_app(p)
    for other in all_apps(cfg):
        if other == p:
            continue
        s = status_of(st, other)
        if s in ("running", "starting"):
            raise Fatal(f'[ERROR] app "{load_app(other)["name"]}" is running')
        if s == "failed":
            _stop(cfg, st, other, reset_mcu=False)
    if status_of(st, p) in ("running", "starting"):
        raise Fatal(f'[ERROR] app "{a["name"]}" is running')
    fw = firmware_for(cfg, p, a)
    if fw is not None:
        flash(cfg, st, fw, p)
    cache = p / ".cache"
    cache.mkdir(exist_ok=True)
    (cache / "exitcode").unlink(missing_ok=True)
    script = (f"cd {shlex.quote(str(p / 'python'))} && {shlex.quote(cfg.python)} main.py "
              f">> ../.cache/app.log 2>&1; echo $? > ../.cache/exitcode")
    env = {**os.environ, "APP_SOCKET": f"unix://{cfg.router}", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(["sh", "-c", script], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    st.s["apps"][str(p)] = {"pid": proc.pid, "startedAt": time.time(), "stopped": False}
    st.save()
    return {"appName": a["name"], "status": "started"}


def _stop(cfg: Cfg, st: State, p: Path, reset_mcu: bool = True):
    a = st.s["apps"].get(str(p))
    if a:
        _kill_group(a.get("pid"))
        a["stopped"] = True
        st.save()
    mcu = st.s.get("mcu") or {}
    if reset_mcu and mcu.get("app") == str(p) and mcu.get("firmware") and Path(mcu["firmware"]).exists():
        flash(cfg, st, Path(mcu["firmware"]), p)  # Reset(): the sketch restarts


def cmd_logs(p: Path, tail: int) -> str:
    log = p / ".cache" / "app.log"
    lines = log.read_text(errors="replace").splitlines() if log.exists() else []
    name = p.name + "-main-1"
    return "\n".join(f"[{name}] {l}" for l in lines[-tail:])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt = "text"
    if "--format" in argv:
        i = argv.index("--format")
        fmt = argv[i + 1]
        del argv[i:i + 2]
    try:
        cfg = Cfg()
        if not argv:
            raise Fatal("usage: arduino-app-cli <command>", ERR_BAD_ARGUMENT)
        if argv[0] == "version":
            res = {"version": VERSION, "board": cfg.board}
        elif argv[0] == "app" and len(argv) > 1:
            sub, rest = argv[1], argv[2:]
            pos = [a for a in rest if not a.startswith("--")]
            if sub == "install":
                res = cmd_install(cfg, rest)
            else:
                st = State(cfg)
                if sub == "list":
                    rel_only = "--releases" in rest
                    res = {"apps": [app_info(cfg, None, p, False)
                                    for p in all_apps(cfg, releases=True, user=not rel_only)], "brokenApps": []}
                elif sub == "ps":
                    apps = [app_info(cfg, st, p, True) for p in all_apps(cfg)]
                    res = {"apps": [x for x in apps if x["status"] != "stopped" or "--all" in rest]}
                elif sub in ("start", "stop", "destroy", "logs", "details"):
                    if not pos:
                        raise Fatal("missing app_path", ERR_BAD_ARGUMENT)
                    p = resolve(cfg, pos[0])
                    if sub == "start":
                        res = cmd_start(cfg, st, p)
                    elif sub == "stop":
                        _stop(cfg, st, p)
                        res = {"appName": load_app(p)["name"], "status": "stopped"}
                    elif sub == "destroy":
                        _stop(cfg, st, p)
                        st.s["apps"].pop(str(p), None)
                        st.save()
                        name = load_app(p)["name"]
                        shutil.rmtree(p)
                        res = {"appName": name, "status": "destroyed"}
                    elif sub == "details":
                        res = app_info(cfg, st, p, True)
                    else:
                        tail = int(rest[rest.index("--tail") + 1]) if "--tail" in rest else 100
                        print(cmd_logs(p, tail))
                        return 0
                else:
                    raise Fatal(f"unknown command app {sub}", ERR_BAD_ARGUMENT)
        else:
            raise Fatal(f"unknown command {' '.join(argv)}", ERR_BAD_ARGUMENT)
    except Fatal as e:
        if fmt == "text":
            print(str(e), file=sys.stderr)
        else:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
        return e.code
    if fmt == "text":
        print(json.dumps(res, indent=2))  # the real text output is a table / one-line message
    else:
        print(json.dumps(res, indent=2 if fmt == "json" else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fake `docker` / `podman` CLI for the push tier of the driver contract suite (no daemon needed).

It implements exactly the CLI surface `CliContainerDriver` / `AnkaiosDriver` use, with docker's semantics:

    image inspect <ref>                       rc 0 when the image was loaded
    load -q -i <payload>                      payload = fake image archive (JSON, see below)
    run -d --name N --restart P --label k=v -e K=V <ref>
    container inspect <name|id>  /  inspect   docker-shaped JSON (State, RestartCount, HostConfig.RestartPolicy,
                                              Config.Labels, NetworkSettings.IPAddress = 127.0.0.1)
    ps -a -q [--filter label=k=v ...]
    stop [-t N] <name>  /  rm -f <name>
    version --format ...
    info

Fake image archive (the "payload"): {"fakeoci": 1, "images": {"<ref>": {"port": 8080, "ready": true,
"exit_after": null, "exit_code": 1}}}. A container runs a small Python HTTP server on 127.0.0.1:<port> answering
GET /healthz (200 when ready) and, with `exit_after`, exits with `exit_code` after that many seconds. A detached
supervisor process per container plays the runtime's restart policy (no / on-failure[:N] / always /
unless-stopped) and keeps RestartCount, like dockerd does.

State lives in $FAKE_OCI_STATE (one directory per container; the CLI writes container.json, the supervisor
writes run.json). Every CLI call is appended to $FAKE_OCI_STATE/calls.jsonl. With $FAKE_OCI_DOWN set, every
command fails like a CLI whose daemon is unreachable.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

STATE = Path(os.environ.get("FAKE_OCI_STATE", "/tmp/fakeoci"))
NAME = os.environ.get("FAKE_OCI_NAME") or Path(sys.argv[0]).name

APP = r'''
import os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
cfg = {"port": int(os.environ["FAKE_PORT"]), "ready": os.environ.get("FAKE_READY") == "1",
       "exit_after": os.environ.get("FAKE_EXIT_AFTER") or None, "code": int(os.environ.get("FAKE_EXIT_CODE", "1"))}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = cfg["ready"] and self.path == "/healthz"
        self.send_response(200 if ok else 503); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok" if ok else b"no")
    def log_message(self, *a): pass
HTTPServer.allow_reuse_address = True
srv = HTTPServer(("127.0.0.1", cfg["port"]), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
if cfg["exit_after"]:
    time.sleep(float(cfg["exit_after"])); os._exit(cfg["code"])
threading.Event().wait()
'''


# ---- helpers -------------------------------------------------------------------------------------------------
def _lock():
    STATE.mkdir(parents=True, exist_ok=True)
    f = open(STATE / ".lock", "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)


def _images() -> dict:
    return _read(STATE / "images.json", {})


def _containers() -> dict[str, dict]:
    out = {}
    d = STATE / "containers"
    if d.exists():
        for c in d.iterdir():
            meta = _read(c / "container.json", None)
            if meta:
                out[meta["Id"]] = meta
    return out


def _find(ref: str) -> dict | None:
    for cid, meta in _containers().items():
        if ref in (cid, cid[:12], meta["Name"], "/" + meta["Name"]):
            return meta
    return None


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return not f.read().split(") ")[-1].startswith("Z")
    except OSError:
        return False


def _inspect(meta: dict) -> dict:
    run = _read(STATE / "containers" / meta["Id"] / "run.json", {})
    sup_alive = _alive(run.get("supervisor"))
    app_alive = _alive(run.get("pid"))
    status = run.get("status", "created")
    if status in ("running", "restarting") and not sup_alive:
        status = "exited"  # supervisor gone (killed): the container is down
    if status == "running" and not app_alive:
        status = "restarting" if sup_alive else "exited"
    pol = meta["RestartPolicy"]
    return {"Id": meta["Id"], "Name": "/" + meta["Name"], "Image": meta["Image"],
            "Config": {"Labels": meta["Labels"], "Env": meta["Env"], "Image": meta["Image"]},
            "State": {"Status": status, "Running": status == "running", "Restarting": status == "restarting",
                      "Dead": False, "ExitCode": int(run.get("exit_code") or 0), "Pid": run.get("pid") or 0},
            "RestartCount": int(run.get("restarts", 0)),
            "HostConfig": {"RestartPolicy": {"Name": pol["Name"], "MaximumRetryCount": pol["MaximumRetryCount"]}},
            "NetworkSettings": {"IPAddress": "127.0.0.1", "Networks": {}}}


def _kill(meta: dict):
    run = _read(STATE / "containers" / meta["Id"] / "run.json", {})
    for key in ("supervisor",):
        pid = run.get(key)
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            end = time.time() + 5
            while _alive(pid) and time.time() < end:
                time.sleep(0.02)
            if _alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    for key in ("pid",):
        pid = run.get(key)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _parse_policy(p: str) -> dict:
    name, _, n = p.partition(":")
    name = {"": "no"}.get(name, name)
    return {"Name": name, "MaximumRetryCount": int(n or 0)}


class Fail(Exception):
    def __init__(self, msg: str, rc: int = 1):
        super().__init__(msg)
        self.rc = rc


# ---- commands -------------------------------------------------------------------------------------------------
def cmd_load(args):
    path = args[args.index("-i") + 1]
    try:
        doc = json.loads(Path(path).read_text())
        assert doc.get("fakeoci") == 1
    except (OSError, ValueError, AssertionError):
        raise Fail("Error: payload is not a fake image archive")
    imgs = _images()
    imgs.update(doc["images"])
    _write(STATE / "images.json", imgs)
    print("Loaded image(s): " + ", ".join(doc["images"]))


def cmd_run(args):
    name, restart, labels, env, i = None, "no", {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "-d":
            i += 1
            continue
        if a in ("--name", "--restart", "--label", "-e", "--device", "--gpus", "--runtime", "--group-add"):
            v = args[i + 1]
            if a == "--name":
                name = v
            elif a == "--restart":
                restart = v
            elif a == "--label":
                k, _, val = v.partition("=")
                labels[k] = val
            elif a == "-e":
                env.append(v)
            i += 2
            continue
        break
    image = args[i]
    cfg = _images().get(image)
    if cfg is None:
        raise Fail(f"Unable to find image '{image}' locally", 125)
    if name and _find(name):
        raise Fail(f'Error: the container name "/{name}" is already in use', 125)
    cid = uuid.uuid4().hex + uuid.uuid4().hex
    d = STATE / "containers" / cid
    d.mkdir(parents=True)
    meta = {"Id": cid, "Name": name or cid[:12], "Image": image, "Labels": labels, "Env": env,
            "RestartPolicy": _parse_policy(restart), "Cfg": cfg, "Created": time.time()}
    _write(d / "container.json", meta)
    _write(d / "run.json", {"status": "created", "restarts": 0})
    sup = subprocess.Popen([sys.executable, os.path.abspath(__file__), "__supervise", cid],
                           env={**os.environ, "FAKE_OCI_STATE": str(STATE)}, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=open(d / "supervisor.log", "ab"), start_new_session=True)
    end = time.time() + 5
    while time.time() < end and _read(d / "run.json", {}).get("status") == "created":
        time.sleep(0.02)
    _write(d / "sup.json", {"pid": sup.pid})
    print(cid)


def supervise(cid: str):
    """The runtime's restart policy for one container (runs detached, one per container)."""
    d = STATE / "containers" / cid
    meta = _read(d / "container.json", {})
    cfg, pol = meta["Cfg"], meta["RestartPolicy"]
    restarts, child = 0, None

    def term(*_):
        if child and child.poll() is None:
            child.kill()
        run = _read(d / "run.json", {})
        _write(d / "run.json", {**run, "status": "exited", "exit_code": 137 if child else 0})
        os._exit(0)

    signal.signal(signal.SIGTERM, term)
    env = {"PATH": os.environ.get("PATH", ""), "FAKE_PORT": str(cfg.get("port", 8080)),
           "FAKE_READY": "1" if cfg.get("ready", True) else "0", "FAKE_EXIT_AFTER": str(cfg.get("exit_after") or ""),
           "FAKE_EXIT_CODE": str(cfg.get("exit_code", 1))}
    for kv in meta["Env"]:
        k, _, v = kv.partition("=")
        env[k] = v
    while True:
        child = subprocess.Popen([sys.executable, "-c", APP], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _write(d / "run.json", {"status": "running", "pid": child.pid, "supervisor": os.getpid(),
                                "restarts": restarts, "startedAt": time.time()})
        code = child.wait()
        name, maxr = pol["Name"], pol["MaximumRetryCount"]
        again = (name in ("always", "unless-stopped")) or (name == "on-failure" and code != 0 and
                                                           (maxr == 0 or restarts < maxr))
        if not again:
            _write(d / "run.json", {"status": "exited", "pid": None, "supervisor": None, "restarts": restarts,
                                    "exit_code": code})
            return
        _write(d / "run.json", {"status": "restarting", "pid": None, "supervisor": os.getpid(),
                                "restarts": restarts, "exit_code": code})
        time.sleep(0.2)
        restarts += 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["__supervise"]:
        supervise(argv[1])
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "calls.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time(), "cli": NAME, "args": argv}) + "\n")
    if os.environ.get("FAKE_OCI_DOWN"):
        print(f"Cannot connect to the {NAME} daemon at unix:///var/run/{NAME}.sock. Is the daemon running?",
              file=sys.stderr)
        return 1
    lk = _lock()
    try:
        a = argv
        if a[:2] == ["image", "inspect"]:
            if a[2] not in _images():
                raise Fail(f"Error: No such image: {a[2]}")
            print(json.dumps([{"Id": a[2], "RepoDigests": [a[2]]}]))
        elif a[:1] == ["load"]:
            cmd_load(a[1:])
        elif a[:1] == ["run"]:
            cmd_run(a[1:])
        elif a[:2] == ["container", "inspect"] or a[:1] == ["inspect"]:
            ref = a[2] if a[0] == "container" else a[1]
            meta = _find(ref)
            if meta is None:
                print("[]")
                raise Fail(f"Error: No such container: {ref}")
            print(json.dumps([_inspect(meta)]))
        elif a[:1] == ["ps"]:
            want = [x.split("=", 1)[1] for i, x in enumerate(a) if i and a[i - 1] == "--filter" and x.startswith("label=")]
            out = []
            for cid, meta in sorted(_containers().items(), key=lambda kv: kv[1]["Created"]):
                if all(meta["Labels"].get(w.partition("=")[0]) == w.partition("=")[2] for w in want):
                    if "-a" in a or _inspect(meta)["State"]["Running"]:
                        out.append(cid[:12])
            print("\n".join(out))
        elif a[:1] in (["stop"], ["rm"]):
            ref = a[-1]
            meta = _find(ref)
            if meta is None:
                raise Fail(f"Error: No such container: {ref}")
            _kill(meta)
            if a[0] == "rm":
                import shutil
                shutil.rmtree(STATE / "containers" / meta["Id"], ignore_errors=True)
            print(ref)
        elif a[:1] == ["version"]:
            print("99.0.0-fake")
        elif a[:1] == ["info"]:
            print("fake runtime")
        else:
            raise Fail(f"fakeoci: unsupported command {a}", 125)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return e.rc
    finally:
        lk.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

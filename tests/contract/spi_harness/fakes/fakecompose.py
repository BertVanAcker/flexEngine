"""Fake compose CLI for the push tier of the driver contract suite (FE-4 ComposeDriver), on top of fakeoci.

    fakecompose -p P --project-directory D -f F <command> ...      (the `docker compose` argument order)

It reads the services of F (image, restart) and drives the fake runtime (`docker` on PATH = fakeoci) the way
Compose v2 drives dockerd: one container `<P>-<service>-1` per service, labelled com.docker.compose.project /
.service / .config-hash (hash of the service and the project directory, so another tree recreates it).

    up -d [--remove-orphans] [--pull never] [--no-build]   create what changed, start every service
    create [--remove-orphans] ...                           create what changed, start nothing
    start / stop [-t N]                                     every container of the project
    down [--volumes] [--remove-orphans]                     remove every container of the project
    ps -a --format json                                     JSON lines: ID, Name, Service, State, ExitCode, Health
    config --images                                         the services' images
    build                                                   prints two lines (the fake builds nothing)
    version --short / ls

$FAKE_COMPOSE_DOWN set: every command fails like a CLI whose daemon is unreachable.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import yaml


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        print(p.stderr, file=sys.stderr, end="")
        raise SystemExit(p.returncode)
    return p


def containers(project: str) -> list[dict]:
    ids = docker("ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}").stdout.split()
    out = []
    for cid in ids:
        p = docker("container", "inspect", cid, check=False)
        if p.returncode == 0:
            out.append(json.loads(p.stdout)[0])
    return out


def health(c: dict) -> str:
    """A service healthcheck the fake way: the fake app's /healthz (200 when its image is `ready`)."""
    import http.client
    if not c["State"]["Running"]:
        return ""
    try:   # the fake image's port: fakeoci's image store ($FAKE_OCI_STATE/images.json)
        with open(os.path.join(os.environ.get("FAKE_OCI_STATE", ""), "images.json")) as f:
            port = json.load(f)[c["Config"]["Image"]]["port"]
    except (OSError, ValueError, KeyError):
        port = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port or 0), timeout=1)
        conn.request("GET", "/healthz")
        ok = conn.getresponse().status == 200
        conn.close()
    except (OSError, ValueError):
        ok = False
    return "healthy" if ok else "starting"


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_COMPOSE_DOWN"):
        print("Cannot connect to the Docker daemon. Is the docker daemon running?", file=sys.stderr)
        return 1
    project = directory = file = None
    rest: list[str] = []
    it = iter(argv)
    for a in it:
        if a == "-p":
            project = next(it)
        elif a == "--project-directory":
            directory = next(it)
        elif a == "-f":
            file = next(it)
        else:
            rest.append(a)
    cmd, args = (rest[0], rest[1:]) if rest else ("", [])
    if cmd == "version":
        print("2.99.0-fake")
        return 0
    if cmd == "ls":
        print("[]")
        return 0
    if project is None:
        print("fakecompose: -p is required", file=sys.stderr)
        return 1
    services = {}
    if file:
        with open(file) as f:
            services = (yaml.safe_load(f) or {}).get("services") or {}
    if cmd in ("up", "create"):
        have = {c["Config"]["Labels"].get("com.docker.compose.service"): c for c in containers(project)}
        for name, svc in sorted(services.items()):
            h = hashlib.sha256(json.dumps([svc, directory], sort_keys=True).encode()).hexdigest()
            cname = f"{project}-{name}-1"
            cur = have.get(name)
            if cur is None or cur["Config"]["Labels"].get("com.docker.compose.config-hash") != h:
                if cur is not None:
                    docker("rm", "-f", cname)
                restart = str(svc.get("restart", "no"))
                docker("run" if cmd == "up" else "create", *(["-d"] if cmd == "up" else []), "--name", cname,
                       "--restart", restart, "--label", f"com.docker.compose.project={project}",
                       "--label", f"com.docker.compose.service={name}",
                       "--label", f"com.docker.compose.config-hash={h}", svc["image"])
            elif cmd == "up" and not cur["State"]["Running"]:
                docker("start", cname)
        if "--remove-orphans" in args:
            for name, c in have.items():
                if name not in services:
                    docker("rm", "-f", c["Id"])
    elif cmd == "start":
        for c in containers(project):
            if not c["State"]["Running"]:
                docker("start", c["Id"])
    elif cmd == "stop":
        for c in containers(project):
            docker("stop", c["Id"], check=False)
    elif cmd == "down":
        for c in containers(project):
            docker("rm", "-f", c["Id"])
    elif cmd == "ps":
        for c in containers(project):
            st = c["State"]
            svc = c["Config"]["Labels"].get("com.docker.compose.service")
            print(json.dumps({"ID": c["Id"][:12], "Name": c["Name"].lstrip("/"), "Service": svc,
                              "State": st["Status"], "ExitCode": st["ExitCode"], "Project": project,
                              "Health": health(c) if (services.get(svc) or {}).get("healthcheck") else ""}))
    elif cmd == "config" and "--images" in args:
        for _, svc in sorted(services.items()):
            print(svc.get("image") or f"{project}-{_}")
    elif cmd == "build":
        print("Step 1/1 : fake build")
        print("Successfully built")
    else:
        print(f"fakecompose: unsupported command {rest}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

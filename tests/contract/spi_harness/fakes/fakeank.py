"""Fake Ankaios `ank` CLI for the driver contract suite: the server + agent collapsed into the CLI call.

Surface used by `AnkaiosDriver`:

    ank -k --server-url URL [--no-wait] apply -            manifest (JSON is YAML) on stdin
    ank -k --server-url URL [--no-wait] delete workload N
    ank -k --server-url URL get state -o json

The agent's runtime is `podman` from PATH (the fake one in the push tier, the real one in the nightly tier).
Ankaios semantics kept: applying an unchanged workload is a no-op; a changed one is replaced (delete + create);
restart policies NEVER / ON_FAILURE / ALWAYS map to podman's no / on-failure / always. The applied workloads are
kept in $FAKE_ANK_STATE/state.json; with $FAKE_ANK_DOWN set the server is unreachable.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_ANK_STATE", "/tmp/fakeank"))
POLICY = {"NEVER": "no", "ON_FAILURE": "on-failure", "ALWAYS": "always"}


def podman(*args, check=True):
    p = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise RuntimeError(f"podman {' '.join(args[:2])}: {p.stderr.strip()}")
    return p


def main(argv: list[str]) -> int:
    args = [a for a in argv if a not in ("-k", "--no-wait")]
    if "--server-url" in args:
        i = args.index("--server-url")
        del args[i:i + 2]
    if os.environ.get("FAKE_ANK_DOWN"):
        print("error: could not connect to the Ankaios server", file=sys.stderr)
        return 1
    STATE.mkdir(parents=True, exist_ok=True)
    lk = open(STATE / ".lock", "w")
    fcntl.flock(lk, fcntl.LOCK_EX)
    sp = STATE / "state.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"workloads": {}}
    with open(STATE / "calls.jsonl", "a") as f:
        f.write(json.dumps({"args": args}) + "\n")
    try:
        if args[:1] == ["apply"]:
            doc = json.loads(sys.stdin.read())
            for name, wl in doc.get("workloads", {}).items():
                exists = podman("container", "inspect", name, check=False).returncode == 0
                if st["workloads"].get(name) == wl and exists:
                    continue  # unchanged: Ankaios does nothing
                if exists:
                    podman("stop", "-t", "3", name, check=False)
                    podman("rm", "-f", name, check=False)
                rc = json.loads(wl["runtimeConfig"])
                podman("run", "-d", "--name", name, "--restart", POLICY.get(wl.get("restartPolicy"), "no"),
                       *rc.get("commandOptions", []), rc["image"])
                st["workloads"][name] = wl
        elif args[:2] == ["delete", "workload"]:
            for name in args[2:]:
                podman("stop", "-t", "3", name, check=False)
                podman("rm", "-f", name, check=False)
                st["workloads"].pop(name, None)
        elif args[:2] == ["get", "state"]:
            print(json.dumps({"agents": {"agent_A": {"cpuUsage": 0}},
                              "workloadStates": {n: {"state": "Running"} for n in st["workloads"]}}))
        else:
            print(f"fakeank: unsupported {args}", file=sys.stderr)
            return 2
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        sp.write_text(json.dumps(st))
        lk.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

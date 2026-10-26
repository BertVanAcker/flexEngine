"""RPC runner for the nightly systemd harness: runs ONE SystemdDriver call inside the systemd container.

    docker exec -i -e PYTHONPATH=/fe/src <box> python3 /fe/harness/systemd_rpc.py  < {"cfg":..., "method":..., "args":...}

Every call builds a NEW driver instance (the driver must rebuild everything from systemd + its files, which is the
point of the contract). Prints one JSON line: {"ok": result} or {"error": "DriverError" | <type>, "msg": ...}.
Instrumentation only: payload unpacks are counted in <dataDir>/.ct-extracts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def markers(device: str) -> dict:
    out: dict = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            env = (p / "environ").read_bytes().split(b"\0")
            st = (p / "stat").read_text()
        except OSError:
            continue
        if st.split(") ")[-1].startswith("Z"):
            continue
        for e in env:
            if e.startswith(f"FLEX_UNIT={device}/".encode()):
                out.setdefault(e.decode().split("/", 1)[1], []).append(int(p.name))
    return out


def main() -> int:
    req = json.loads(sys.stdin.read())
    cfg = req["cfg"]
    from flexengine2.drivers.base import DriverError, Observed, Workload
    from flexengine2.drivers.native import systemd as sd

    if req["method"] == "_markers":
        print(json.dumps({"ok": markers(cfg["device"])}))
        return 0
    data = Path(cfg["data"])
    real = sd._extract

    def counting(src, dst):
        data.mkdir(parents=True, exist_ok=True)
        with open(data / ".ct-extracts", "a") as f:
            f.write("x\n")
        return real(src, dst)
    sd._extract = counting
    drv = sd.SystemdDriver(cfg["device"], data, **cfg.get("kw", {}))
    args = []
    for a in req["args"]:
        if isinstance(a, dict) and a.get("__wl"):
            args.append(Workload.from_dict(a["__wl"]))
        elif isinstance(a, dict) and a.get("__rel"):
            args.append(SimpleNamespace(**a["__rel"]))
        else:
            args.append(a)
    try:
        r = getattr(drv, req["method"])(*args)
    except DriverError as e:
        print(json.dumps({"error": "DriverError", "msg": str(e)}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": type(e).__name__, "msg": str(e)}))
        return 0

    def enc(v):
        if isinstance(v, Observed):
            return {"__obs": v.__dict__}
        if isinstance(v, dict):
            return {k: enc(x) for k, x in v.items()}
        return v
    print(json.dumps({"ok": enc(r)}))
    return 0


if __name__ == "__main__":
    os.environ.pop("PYTHONSTARTUP", None)
    raise SystemExit(main())

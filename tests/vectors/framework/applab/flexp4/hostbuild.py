"""Build a generated sketch for the host (probe P4): sketch.ino + its headers compiled UNMODIFIED against the
real Arduino_RouterBridge (bridge.h), Arduino_RPClite and MsgPack, with small Zephyr/Arduino shims
(host/shim) and host/host_main.cpp, which connects Serial1 to a pty for the real arduino-router."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
REF = PROBE / ".ref"
LIB_DIRS = ["Arduino_RouterBridge/src", "Arduino_RPClite/src", "MsgPack", "ArxContainer", "ArxTypeTraits", "DebugLog"]
CACHE = PROBE / "build" / "host-cache"


def include_flags() -> list[str]:
    return ["-I" + str(PROBE / "host" / "shim")] + ["-I" + str(REF / d) for d in LIB_DIRS]


def build(sketch_dir: Path, out: Path, *, opt: str = "-O1") -> Path:
    """Compile sketch_dir/sketch.ino (with its headers) to `out`. Cached by source content."""
    sketch_dir = Path(sketch_dir)
    h = hashlib.sha256()
    for p in sorted(sketch_dir.glob("*")) + sorted((PROBE / "host").rglob("*.[ch]*")):
        if p.is_file():
            h.update(p.name.encode() + p.read_bytes())
    h.update(opt.encode())
    key = h.hexdigest()[:20]
    cached = CACHE / key
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cached.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".tmp")
        cmd = ["g++", "-std=gnu++17", opt, "-DARDUINO_UNO_Q", "-Wall", "-Wno-unused-function",
               "-Wno-unused-variable", *include_flags(), "-I" + str(sketch_dir),
               "-include", "Arduino.h", "-x", "c++", str(sketch_dir / "sketch.ino"),
               "-x", "c++", str(PROBE / "host" / "host_main.cpp"), "-o", str(tmp), "-lpthread"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            errs = [l for l in r.stderr.splitlines() if "error" in l]
            raise RuntimeError(f"host build of {sketch_dir} failed:\n" + "\n".join(errs[:20]))
        tmp.rename(cached)
    shutil.copy2(cached, out)
    return out

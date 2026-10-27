"""Fake `journalctl` for the push tier (UM-4 SystemdDriver.logs), on the fake service manager's output files.

    journalctl [--user] [--no-pager] -o json -u <unit> [-n N] [--after-cursor C]

fakesystemctl.py's monitor writes a unit's stdout and stderr to $FAKE_SYSTEMD_STATE/run/<unit>.log; each line is one
journal entry: MESSAGE, __CURSOR (`line=<n>`), __REALTIME_TIMESTAMP (µs: the file's mtime, the same for every line,
as close as the fake gets). `-n N`: the last N entries (after the cursor); without -u nothing. $FAKE_SYSTEMD_DOWN set:
fails like a journal that cannot be opened.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_SYSTEMD_STATE", "/tmp/fakesystemd"))


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_SYSTEMD_DOWN"):
        print("Failed to open journal: No such file or directory", file=sys.stderr)
        return 1
    unit, n, after, i = None, None, None, 0
    while i < len(argv):
        a = argv[i]
        if a in ("-u", "--unit"):
            unit, i = argv[i + 1], i + 2
        elif a in ("-n", "--lines"):
            n, i = int(argv[i + 1]), i + 2
        elif a == "--after-cursor":
            after, i = argv[i + 1], i + 2
        elif a in ("-o", "--output"):
            i += 2
        else:
            i += 1
    if not unit:
        return 0
    p = STATE / "run" / f"{unit}.log"
    try:
        lines = p.read_text(errors="replace").splitlines()
        at = int(p.stat().st_mtime * 1e6)
    except OSError:
        return 0
    first = int(after.split("=", 1)[1]) + 1 if after and after.startswith("line=") else 0
    entries = [(k, ln) for k, ln in enumerate(lines) if k >= first]
    if n is not None:
        entries = entries[-n:] if n > 0 else []
    for k, ln in entries:
        print(json.dumps({"MESSAGE": ln, "__CURSOR": f"line={k}", "__REALTIME_TIMESTAMP": str(at),
                          "_SYSTEMD_UNIT": unit}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

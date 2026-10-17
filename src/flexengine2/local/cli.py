"""flexengine CLI (alpha skeleton; the full command set and the local API follow in EV-07).

    flexengine run --config engine.yaml [--ticks N]
    flexengine plc-read --via ads|modbus|shm --conn '{...}' [--spec flex-plc.json]
    flexengine config check <file> [--json] [--resolve]
    flexengine version
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flexengine2 import __version__


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flexengine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run every device engine of this host")
    p.add_argument("--config", required=True)
    p.add_argument("--ticks", type=int, default=0, help="stop after N scheduler rounds (0 = forever)")
    p = sub.add_parser("plc-read", help="read the interface hash of a PLC once")
    p.add_argument("--via", required=True, choices=["ads", "modbus", "shm"])
    p.add_argument("--conn", required=True)
    p.add_argument("--spec")
    p = sub.add_parser("config", help="engine configuration tools")
    csub = p.add_subparsers(dest="config_cmd", required=True)
    p = csub.add_parser("check", help="validate an engine config (exit 0 valid, 1 invalid)")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help='print {"valid": bool, "errors": [{path, message}]}')
    p.add_argument("--resolve", action="store_true", help="also read stackInfo, releaseKeyFiles, onboardingTokenFile")
    sub.add_parser("version")
    a = ap.parse_args(argv)
    if a.cmd == "version":
        print(__version__)
        return 0
    if a.cmd == "run":
        from flexengine2.host import load_config, run

        run(load_config(a.config), ticks=a.ticks)
        return 0
    if a.cmd == "config":
        from flexengine2.config.load import check, format_error

        r = check(a.file, resolve_files=a.resolve)
        for w in r["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        if a.json:
            print(json.dumps({"valid": r["valid"], "errors": r["errors"]}))
        else:
            for e in r["errors"]:
                print(format_error(e))
            if r["valid"]:
                print(f"{a.file}: valid")
        return 0 if r["valid"] else 1
    if a.cmd == "plc-read":
        from flexengine2.drivers.plc.verify import READERS, Unreachable

        spec = json.loads(Path(a.spec).read_text()) if a.spec else {}
        try:
            h, detail = READERS[a.via](json.loads(a.conn), spec).read_hash()
        except Unreachable as e:
            print(f"unreachable: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"interfaceHash": h, "detail": detail}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

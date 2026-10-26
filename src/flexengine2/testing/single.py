"""One device engine as a process (test entry; equivalent of probe P2's `python -m flexp2.engine`).

    python -m flexengine2.testing.single --config engine.json [--import-bundle bundle.tar] [--ticks N]

`engine.json` is the flat P2 engine config (deviceId, apiUrl, dataDir, onboardingToken, cloudPublicKey,
releasePublicKeys, drivers, ...). The production entry is `flexengine run` (multi-device host, flexengine2.host).
"""

from __future__ import annotations

import argparse
import json

from flexengine2.core.reconciler import Engine


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="flexengine2.testing.single")
    ap.add_argument("--config", required=True)
    ap.add_argument("--import-bundle")
    ap.add_argument("--ticks", type=int, default=0, help="run N ticks then exit (0 = forever)")
    a = ap.parse_args(argv)
    with open(a.config) as f:
        eng = Engine(json.load(f))
    if a.import_bundle:
        eng.import_bundle(a.import_bundle)
    if a.ticks:
        for _ in range(a.ticks):
            eng.tick()
    else:
        eng.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

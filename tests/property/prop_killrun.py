"""Engine subprocess with deterministic SIGKILL points (EV-13). Same as `python -m flexengine2.testing.single`, plus
`--kill EVENT:K:WHEN`: the process SIGKILLs itself (no cleanup, no atexit, no final save -- a real kill -9) at the
K-th occurrence of EVENT in this incarnation, before or after it (WHEN = before | after | torn).

Events
  action:<kind>       Engine.action (apply, adopt, commit, rollback, remove, reject, repair); the record is fsynced
  write:state.json    util.atomic_write_bytes of state.json      (torn = a partial temp file is left, target intact)
  write:units.json    the process driver's unit table (writes inside _spawn are not counted, see `spawn`)
  write:cas           artefact store writes (downloads, bundle import, referrer index)
  drv:apply / drv:remove   ProcessDriver.apply / remove
  gate                Engine.health_gate
  spawn               ProcessDriver._spawn after Popen, before the unit table records the pid

Every fsync of a file under native/ is logged to fsynced.log next to the config (power-cut model of the payload).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


class Spec:
    def __init__(self, s: str):
        ev, k, when = s.rsplit(":", 2)
        self.event, self.k, self.when, self.seen = ev, int(k), when, 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--import-bundle")
    ap.add_argument("--kill", action="append", default=[])
    a = ap.parse_args(argv)
    specs = [Spec(s) for s in a.kill]

    from flexengine2 import util
    from flexengine2.artefacts import store
    from flexengine2.core import reconciler
    from flexengine2.drivers import process

    def die():
        os.kill(os.getpid(), signal.SIGKILL)

    def hit(ev: str, when: str):
        for s in specs:
            if s.event != ev:
                continue
            if when == "before":
                s.seen += 1
            if s.seen == s.k and s.when == when:
                die()

    # power-cut model of the native payload (test_prop_crash.lose_unsynced_payload): which files under native/ were
    # fsynced; the harness empties every other payload file after a simulated power cut
    synced_log = Path(a.config).resolve().parent / "fsynced.log"
    real_fsync = os.fsync

    def fsync(fd):
        real_fsync(fd)
        try:
            p = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return
        if f"{os.sep}native{os.sep}" in p:
            with open(synced_log, "a") as f:
                f.write(p + "\n")

    os.fsync = fsync

    in_spawn = [False]
    orig_write = util.atomic_write_bytes

    def write(path, data, mode=0o644):
        p = Path(path)
        ev = ("write:state.json" if p.name == "state.json" else "write:units.json" if p.name == "units.json"
              else "write:cas" if "cas" in p.parts else None)
        if ev and not (ev == "write:units.json" and in_spawn[0]):
            for s in specs:
                if s.event == ev and s.when == "torn" and s.seen + 1 == s.k:
                    t = p.parent / f".{p.name}.torn"
                    with open(t, "wb") as f:
                        f.write(data[: len(data) // 2])
                    die()
            hit(ev, "before")
            orig_write(path, data, mode)
            hit(ev, "after")
        else:
            orig_write(path, data, mode)

    util.atomic_write_bytes = write
    store.atomic_write_bytes = write

    orig_action = reconciler.Engine.action

    def action(self, kind, dep, **kw):
        hit(f"action:{kind}", "before")
        orig_action(self, kind, dep, **kw)
        hit(f"action:{kind}", "after")

    reconciler.Engine.action = action

    def wrap(cls, name, ev):
        orig = getattr(cls, name)

        def f(*args, **kw):
            hit(ev, "before")
            r = orig(*args, **kw)
            hit(ev, "after")
            return r
        setattr(cls, name, f)

    wrap(process.ProcessDriver, "apply", "drv:apply")
    wrap(process.ProcessDriver, "remove", "drv:remove")
    wrap(reconciler.Engine, "health_gate", "gate")

    real_popen = subprocess.Popen

    class _Sub:
        def __getattr__(self, k):
            return getattr(subprocess, k)

        @staticmethod
        def Popen(*args, **kw):
            p = real_popen(*args, **kw)
            hit("spawn", "before")   # both after Popen: the process exists, the unit table does not know it yet
            hit("spawn", "after")
            return p

    process.subprocess = _Sub()
    orig_spawn = process.ProcessDriver._spawn

    def spawn(self, dep_id):
        in_spawn[0] = True
        try:
            return orig_spawn(self, dep_id)
        finally:
            in_spawn[0] = False

    process.ProcessDriver._spawn = spawn

    with open(a.config) as f:
        eng = reconciler.Engine(json.load(f))
    if a.import_bundle:
        eng.import_bundle(a.import_bundle)
    eng.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

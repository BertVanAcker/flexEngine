#!/usr/bin/env python3
"""Dry runs of the EV-HW desk runbooks against simulated targets (EV-15, plan 7.2 layer 9).

    python flexengine/hwtest/dryrun.py EV-HW-02             # one runbook, full dry run
    python flexengine/hwtest/dryrun.py --all                # every runbook
    python flexengine/hwtest/dryrun.py EV-HW-02 --pc-only   # only the PC-side steps (run.py --only)
    python flexengine/hwtest/dryrun.py EV-HW-02 --keep      # keep the simulated desk's directory

For each runbook: `simdesk.py serve --profile <profile>` starts the simulated desk in a short directory under /tmp
(unix socket paths < 108 characters) and writes the runbook variables that point at it (sim.json); the runbook
then runs unchanged through run.py with `--unattended --yes --set dry_run=1 --set <every sim variable>`, so manual
steps run their `sim:` command and power cycles their `simulate:` command. Results go to hwtest-results/ like a
desk run (result.json says `"target": "sim"`, `"dry_run": true`), plus a copy of the simulated engine's log.
Exit code 0 = every selected runbook passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNBOOKS = HERE / "runbooks"
RESULTS = HERE / "hwtest-results"

# runbook id -> (simulated desk profile, PC-side step ids for --pc-only)
SIM = {
    "EV-HW-01": ("install", ["stack", "token"]),
    "EV-HW-02": ("lp", ["stack", "publish"]),
    "EV-HW-03": ("lp", ["stack", "publish"]),
    "EV-HW-04": ("mcu", ["stack", "publish"]),
    "EV-HW-05": ("mcu", ["stack", "publish"]),
    "EV-HW-06": ("applab", ["stack"]),
    "EV-HW-07": ("plc", ["stack", "publish"]),
    "EV-HW-08": ("lp", ["stack", "publish"]),
    "EV-HW-09": ("lp", ["stack", "publish"]),
    "EV-HW-10": ("lp", ["stack", "publish"]),
    "EV-HW-11": ("rauc", ["stack", "publish"]),
}


def runbook_path(rid: str) -> Path:
    hits = sorted(p for p in RUNBOOKS.glob(f"{rid}_*.yaml") if not p.name.endswith(".gui.yaml"))
    if not hits:
        raise SystemExit(f"no runbook {rid} in {RUNBOOKS}")
    return hits[0]


def env() -> dict:
    e = dict(os.environ)
    src = [str(HERE.parent / "src"), str(HERE.parent.parent / "flexmodel" / "src")]
    e["PYTHONPATH"] = os.pathsep.join(src + ([e["PYTHONPATH"]] if e.get("PYTHONPATH") else []))
    e["PYTHONUNBUFFERED"] = "1"
    return e


def start_desk(profile: str, d: Path, timeout: float = 900) -> tuple[subprocess.Popen, dict]:
    log = open(d / "simdesk.log", "wb")
    p = subprocess.Popen([sys.executable, str(HERE / "simdesk.py"), "serve", "--dir", str(d), "--profile", profile],
                         stdout=log, stderr=subprocess.STDOUT, env=env(), start_new_session=True)
    end = time.time() + timeout
    while time.time() < end:
        if (d / "sim.json").exists():
            return p, json.loads((d / "sim.json").read_text())["vars"]
        if (d / "sim.json.error").exists() or p.poll() is not None:
            break
        time.sleep(0.5)
    err = (d / "sim.json.error").read_text() if (d / "sim.json.error").exists() else ""
    stop_desk(p, d)
    raise RuntimeError(f"simulated desk ({profile}) did not come up: {err}\n"
                       f"{(d / 'simdesk.log').read_text(errors='replace')[-3000:]}")


def stop_desk(p: subprocess.Popen, d: Path) -> None:
    if p.poll() is None:
        subprocess.run([sys.executable, str(HERE / "simdesk.py"), "ctl", "--dir", str(d), "stop"], env=env(),
                       capture_output=True, timeout=60)
        try:
            p.wait(120)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(10)


def newest_result(rid: str, results: Path, after: float) -> Path | None:
    cands = [p for p in results.glob(f"*_{rid}") if p.is_dir() and p.stat().st_mtime >= after - 1]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def dry_run(rid: str, results: Path, pc_only: bool = False, keep: bool = False, extra: list[str] | None = None) -> int:
    profile, pc_steps = SIM[rid]
    rb = runbook_path(rid)
    d = Path(tempfile.mkdtemp(prefix=f"rb-{rid[-2:]}-", dir="/tmp"))
    t0 = time.time()
    print(f"== {rid}: simulated desk '{profile}' in {d}", flush=True)
    rc = 1
    p = None
    try:
        p, sim_vars = start_desk(profile, d)
        args = [sys.executable, str(HERE / "run.py"), str(rb), "--unattended", "--yes", "--results", str(results),
                "--set", "operator=dry-run"]
        for k, v in sim_vars.items():
            args += ["--set", f"{k}={v}"]
        if pc_only:
            args += ["--only", ",".join(pc_steps)]
        args += extra or []
        r = subprocess.run(args, env=env(), stdin=subprocess.DEVNULL, timeout=3600)
        rc = r.returncode
    finally:
        if p is not None:
            stop_desk(p, d)
        res = newest_result(rid, results, t0)
        if res is not None:
            for name, src in (("sim-engine.log", d / "lp" / "engine.log"), ("simdesk.log", d / "simdesk.log")):
                if src.exists():
                    data = src.read_bytes()
                    (res / name).write_bytes(data[-150_000:])
        if keep:
            print(f"   kept {d}")
        else:
            shutil.rmtree(d, ignore_errors=True)
    print(f"== {rid}: {'PASS' if rc == 0 else 'FAIL'} ({time.time() - t0:.0f} s)", flush=True)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="runbook ids (EV-HW-02 ...)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pc-only", action="store_true", help="only the PC-side steps (run.py --only)")
    ap.add_argument("--keep", action="store_true", help="keep the simulated desk directory")
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--set", action="append", default=[], help="extra runbook variable (NAME=VALUE)")
    a = ap.parse_args(argv)
    ids = sorted(SIM) if a.all else a.ids
    if not ids:
        ap.error("name runbook ids or --all")
    extra = [x for kv in a.set for x in ("--set", kv)]
    failed = [rid for rid in ids if dry_run(rid, a.results, a.pc_only, a.keep, extra) != 0]
    print(f"dry run: {len(ids) - len(failed)}/{len(ids)} passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Desk-runbook runner for flexEngineV2: the pinned flexNode-arduino runner (tools/hwtest/runbook.py, unmodified)
with the flex step types of flexsteps.py registered into its ACTIONS table (plan 8.2 option (c)).

    python flexengine/hwtest/run.py flexengine/hwtest/runbooks/EV-HW-02_container.yaml
    python flexengine/hwtest/run.py RUNBOOK --list                  # steps + format check (exit 1 on a problem)
    python flexengine/hwtest/run.py --check-all                     # --list for every runbook below runbooks/
    python flexengine/hwtest/run.py RUNBOOK --only s1,s2 --set fe_api=http://lattepanda.lab:8781
    python flexengine/hwtest/run.py RUNBOOK --unattended --yes --set dry_run=1 ...   # what dryrun.py does

Every option of the runner works (--list, --from, --only, --set, --yes, --unattended, --results). What the wrapper
adds: the flex step types; `--list` also checks every step's parameters, `${var}` references and marks
(flexsteps.validate) and fails on a problem; built-in variables ${hwtest}, ${flexengine}, ${repo}; results default
to flexengine/hwtest/hwtest-results/ (where the GUI looks); result.json also records the upstream pin and hides
secret-looking variables (as the runner's GUI commit does).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parent
REPO = FLEXENGINE.parent
RESULTS = HERE / "hwtest-results"
RUNBOOKS = HERE / "runbooks"
SECRET_VAR = re.compile(r"pass|secret|token(?!_file)", re.I)

sys.path.insert(0, str(HERE))
import flexsteps  # noqa: E402
import upstream  # noqa: E402

_LOADED = {}


def load_runner():
    """The pinned runner module (`runbook`), with the flex steps registered and the wrapper's hooks installed."""
    if "R" in _LOADED:
        return _LOADED["R"]
    rdir = upstream.runner_dir()
    if str(rdir) not in sys.path:
        sys.path.insert(0, str(rdir))
    R = importlib.import_module("runbook")
    if Path(R.__file__).resolve().parent != rdir.resolve():
        raise RuntimeError(f"`runbook` was imported from {R.__file__}, not from the pinned runner in {rdir}")
    flexsteps.register(R)
    _install_hooks(R)
    _LOADED["R"] = R
    return R


def builtins() -> dict:
    return {"hwtest": str(HERE), "flexengine": str(FLEXENGINE), "repo": str(REPO), "project_dir": str(REPO)}


def _install_hooks(R) -> None:
    orig_resolve, orig_report = R.resolve_vars, R.write_report

    def resolve_vars(ctx):
        for k, v in builtins().items():
            ctx.vars.setdefault(k, v)
        ctx.vars.setdefault("results_dir", ctx.results_dir)
        return orig_resolve(ctx)

    def write_report(ctx, results, started, aborted):
        shown = {k: ("(hidden)" if v else "") if SECRET_VAR.search(k) else v for k, v in ctx.vars.items()}
        real, ctx.vars = ctx.vars, shown
        try:
            base, summary = orig_report(ctx, results, started, aborted)
        finally:
            ctx.vars = real
        try:  # the pin and the plugin into result.json (same file, one more key)
            p = Path(base + ".json")
            data = json.loads(p.read_text())
            lock = upstream.lock()
            data["flexhwtest"] = {
                "upstream": {s["name"]: {"commit": s["commit"], "ref": s["ref"]} for s in lock["sources"]},
                "repository": lock["repository"], "steps": flexsteps.NEW,
                "dry_run": flexsteps.truthy(real.get("dry_run", "")),
                "target": real.get("target", "desk"),
            }
            p.write_text(json.dumps(data, indent=2) + "\n")
        except (OSError, ValueError):
            pass
        return base, summary

    R.resolve_vars, R.write_report = resolve_vars, write_report


def list_runbook(R, path: str) -> int:
    try:
        rb = R.load_runbook(path)
    except SystemExit as e:
        print(f"{path}: {e}")
        return 1
    errs = flexsteps.validate(rb, R.ACTIONS)
    R.main([path, "--list"])
    for e in errs:
        print(f"  ERROR {e}")
    if errs:
        print(f"{rb.get('id')}: {len(errs)} problem(s)")
    return 1 if errs else 0


def all_runbooks() -> list[Path]:
    return sorted(p for p in RUNBOOKS.glob("*.yaml") if not p.name.endswith(".gui.yaml"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    R = load_runner()
    if argv[:1] == ["--check-all"]:
        rc = 0
        for p in all_runbooks():
            rc |= list_runbook(R, str(p))
            print()
        return rc
    if "--list" in argv:
        rb = next((a for a in argv if not a.startswith("-") and a.endswith((".yaml", ".yml"))), None)
        if rb is None:
            print("usage: run.py RUNBOOK --list")
            return 2
        return list_runbook(R, rb)
    if not any(a == "--results" or a.startswith("--results=") for a in argv):
        argv += ["--results", str(RESULTS)]
    rb = next((a for a in argv if not a.startswith("-") and a.endswith((".yaml", ".yml"))), None)
    if rb:
        errs = flexsteps.validate(R.load_runbook(rb), R.ACTIONS)
        if errs:
            print("runbook problems (run.py RUNBOOK --list):\n  " + "\n  ".join(errs))
            return 2
    return R.main(argv)


if __name__ == "__main__":
    sys.exit(main())

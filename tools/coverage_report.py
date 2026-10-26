"""Coverage gate of flexEngineV2 (docs/flexengine-v2-plan.md §7.3) from a coverage.py JSON report.

    python tools/coverage_report.py <coverage.json> [--out summary.json] [--no-gate]

Groups (paths below src/flexengine2/):
  core      the reconciler and what it needs to accept, verify and report: core/, artefacts/, identity/, cloud/,
            config/, contracts.py, util.py, host.py
  services  local API / CLI / doctor, logging, history, OTel, the MCU relay: local/, obs/, gateway/
  drivers   one group per driver (drivers/base.py counts to every driver's SPI, so it is reported on its own)
  overall   everything except testing/ (reference fakes and lab helpers used by the tests and the desk runbooks)

Targets: core >= 90 % lines, branch >= 80 % for core/reconciler.py and artefacts/store.py (release verification;
the plan's core/health.py and artefacts/verify.py live inside these two modules), every driver >= 80 % lines,
overall >= 85 % lines. Exit 1 when a target is missed (unless --no-gate).

Without flexmodel (not on PyPI; the relay extra needs it, decision FE-D3) gateway/relay.py cannot run: it is left out
of every group and listed under notMeasured. FLEX_COVERAGE_DEFER=<driver>[,<driver>...] reports a driver's miss under
deferred instead of failing the gate: for a driver whose integration suite needs the framework (applab: probe P4's
board simulator) when the run has no framework checkout; the framework's CI gates it with the full suite.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

CORE = ("core/", "artefacts/", "identity/", "cloud/", "config/", "contracts.py", "util.py", "host.py")
SERVICES = ("local/", "obs/", "gateway/")
DRIVERS = {
    "spi-base": ("drivers/base.py",),
    "container": ("drivers/container.py",),
    "native-process": ("drivers/process.py", "drivers/native/pyenv.py"),
    "native-systemd": ("drivers/native/systemd.py",),
    "applab": ("drivers/applab/",),
    "mcu": ("drivers/mcu/",),
    "plc-verify": ("drivers/plc/",),
    "os-rauc": ("drivers/os_rauc.py",),
    "arduino-ota": ("drivers/arduino_ota.py", "drivers/arduino_wire.py"),
}
BRANCH = ("core/reconciler.py", "artefacts/store.py")
TARGETS = {"core": 90.0, "drivers": 80.0, "overall": 85.0, "branch": 80.0}
NEEDS_FLEXMODEL = ("gateway/relay.py",)


def rel(path: str) -> str | None:
    p = path.replace("\\", "/")
    i = p.rfind("flexengine2/")
    return p[i + len("flexengine2/"):] if i >= 0 else None


def agg(files: dict, prefixes) -> dict:
    n = c = nb = cb = 0
    names = []
    for f, s in files.items():
        if any(f == p or f.startswith(p) for p in prefixes):
            n += s["num_statements"]
            c += s["covered_lines"]
            nb += s.get("num_branches", 0)
            cb += s.get("covered_branches", 0)
            names.append(f)
    return {"statements": n, "covered": c, "lines": round(100.0 * c / n, 1) if n else None,
            "branches": nb, "coveredBranches": cb, "branch": round(100.0 * cb / nb, 1) if nb else None,
            "files": sorted(names)}


def summarise(cov: dict, defer: tuple[str, ...] = ()) -> dict:
    files = {}
    not_measured = {}
    skip = () if importlib.util.find_spec("flexmodel") else NEEDS_FLEXMODEL
    for path, d in cov["files"].items():
        r = rel(path)
        if r is None or r.startswith("testing/") or r.endswith("__init__.py") and d["summary"]["num_statements"] == 0:
            continue
        if r in skip:
            not_measured[r] = "needs flexmodel, not importable in this run"
            continue
        files[r] = d["summary"]
    core = agg(files, CORE)
    out = {
        "targets": TARGETS,
        "core": core,
        "services": agg(files, SERVICES),
        "overall": agg(files, ("",)),
        "drivers": {k: agg(files, v) for k, v in DRIVERS.items()},
        "branch": {b: agg(files, (b,)) for b in BRANCH},
        "perFile": {f: {"lines": round(100.0 * s["covered_lines"] / s["num_statements"], 1) if s["num_statements"] else None,
                        "missing": s["missing_lines"], "statements": s["num_statements"]}
                    for f, s in sorted(files.items())},
    }
    for g in ("core", "services", "overall"):
        out[g].pop("files")
    misses, deferred = [], []
    if (core["lines"] or 0) < TARGETS["core"]:
        misses.append(f"core {core['lines']} % < {TARGETS['core']} %")
    for k, v in out["drivers"].items():
        if k != "spi-base" and v["statements"] and (v["lines"] or 0) < TARGETS["drivers"]:
            (deferred if k in defer else misses).append(f"driver {k} {v['lines']} % < {TARGETS['drivers']} %")
    if (out["overall"]["lines"] or 0) < TARGETS["overall"]:
        misses.append(f"overall {out['overall']['lines']} % < {TARGETS['overall']} %")
    for b, v in out["branch"].items():
        if v["branches"] and (v["branch"] or 0) < TARGETS["branch"]:
            misses.append(f"branch {b} {v['branch']} % < {TARGETS['branch']} %")
    out["misses"] = misses
    out["deferred"] = deferred
    out["notMeasured"] = not_measured
    out["met"] = not misses
    return out


def render(s: dict) -> str:
    rows = [("core", s["core"]), ("services", s["services"]), ("overall", s["overall"])]
    rows += [(f"driver {k}", v) for k, v in s["drivers"].items()]
    lines = [f"{'group':<24} {'lines %':>8} {'branch %':>9} {'stmts':>7}"]
    for name, v in rows:
        lines.append(f"{name:<24} {v['lines'] if v['lines'] is not None else '-':>8} "
                     f"{v['branch'] if v['branch'] is not None else '-':>9} {v['statements']:>7}")
    for b, v in s["branch"].items():
        lines.append(f"branch {b:<17} {v['lines']:>8} {v['branch']:>9} {v['statements']:>7}")
    for f, why in s.get("notMeasured", {}).items():
        lines.append(f"not measured: {f} ({why})")
    if s.get("deferred"):
        lines.append("deferred (FLEX_COVERAGE_DEFER): " + "; ".join(s["deferred"]))
    lines.append("targets met" if s["met"] else "below target: " + "; ".join(s["misses"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("coverage_json")
    ap.add_argument("--out")
    ap.add_argument("--no-gate", action="store_true")
    a = ap.parse_args(argv)
    defer = tuple(d.strip() for d in os.environ.get("FLEX_COVERAGE_DEFER", "").split(",") if d.strip())
    s = summarise(json.loads(Path(a.coverage_json).read_text()), defer)
    if a.out:
        Path(a.out).write_text(json.dumps(s, indent=2) + "\n")
    print(render(s))
    return 0 if (s["met"] or a.no_gate) else 1


if __name__ == "__main__":
    sys.exit(main())

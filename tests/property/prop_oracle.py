"""Invariant oracles shared by the in-process and the subprocess properties (EV-13).

The ground truth is what is durable on disk (`state.json`, `actions.jsonl`), what the executor runs and, with a link,
what flexCloud received. A *crash record* is taken by the harness at every kill / power cut:
    {"line": n_actions_before_restart, "journal": (dep, digest) | None, "running": {dep: digest}}
(`journal` from the on-disk state.json after the kill, `running` = what the executor runs when the engine restarts).
"""

from __future__ import annotations

import json
from pathlib import Path

TERMINAL = ("Installed", "Failed", "Removed")


def record_stats(kind: str, crashes: list[dict], actions: list[dict], **extra):
    """One line per schedule in tests/work/property/stats.jsonl (what the schedules really exercised)."""
    import collections
    import os
    import time
    p = Path(__file__).resolve().parents[1] / "work" / "property" / "stats.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": time.time(), "kind": kind, "test": os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0],
           "crashes": len(crashes), "powerCuts": sum(1 for c in crashes if c.get("powerCut")),
           "actions": dict(collections.Counter(a.get("action") for a in actions)), **extra}
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")


class Violation(AssertionError):
    pass


def read_actions(path: Path) -> tuple[list[dict], list[str]]:
    """(records, unparseable lines). Each action line is fsynced before the engine goes on, so a completed record
    must always parse; only a torn *last* line (power cut during the append) may be garbage."""
    recs, bad = [], []
    if not Path(path).exists():
        return recs, bad
    lines = Path(path).read_bytes().split(b"\n")
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict) or "action" not in rec:
                raise ValueError
            recs.append(rec)
        except (ValueError, UnicodeDecodeError):
            bad.append(raw.decode("utf-8", "replace"))
    return recs, bad


def duplicate_applies(actions: list[dict], crashes: list[dict]) -> list[str]:
    """At most one forward `apply` per (deployment, digest) "run", except what a crash makes necessary.

    A run of (dep, D) starts with the first apply of D and ends with an apply of another digest, a removal by
    absence (or of an abandoned install), or a `repair` that took D off the executor (`abandoned` / `observed`: the
    engine restored the committed release over an interrupted install). Re-adding later is a new run. `rollback` and
    the other `repair`s are declared actions and never count. A second apply of D inside the run is allowed only when
    a crash happened since the previous apply of D while the on-disk journal named (dep, D) and the executor did not
    run D at the restart (the apply never completed). It is never allowed once the engine declared D failed in that
    run (rollback with failed=D, or "failed first install" remove): a known-bad release must not be started again,
    not even after a crash inside the rollback."""
    out = []
    runs: dict[str, dict] = {}
    marks = sorted(crashes, key=lambda c: c["line"])
    for i, a in enumerate(actions):
        dep, kind = a.get("dep"), a.get("action")
        r = runs.get(dep)
        if kind == "apply":
            d = a.get("digest")
            if r is None or r["digest"] != d:
                runs[dep] = {"digest": d, "last": i, "failed": False, "version": a.get("version")}
                continue
            between = [c for c in marks if r["last"] < c["line"] <= i]
            ok_crash = [c for c in between if c.get("journal") == (dep, d) and c.get("running", {}).get(dep) != d]
            if r["failed"]:
                out.append(f"{dep}: apply #{i} re-applies {a.get('version')} ({d[:19]}) after it was declared failed "
                           f"in the same run (crashes in between: {len(between)})")
            elif not ok_crash:
                out.append(f"{dep}: duplicate apply #{i} of {a.get('version')} ({d[:19]}) without a crash that left "
                           f"it incomplete (crashes in between: {between})")
            r["last"] = i
        elif kind == "rollback" and r is not None and a.get("failed") == r["version"]:
            r["failed"] = True
        elif kind == "repair":
            if r is not None and a.get("digest") != r["digest"] and ("abandoned" in a or "observed" in a):
                runs.pop(dep, None)
        elif kind == "remove":
            if a.get("why") == "failed first install":
                if r is not None and r["digest"] == a.get("digest"):
                    r["failed"] = True
            else:
                runs.pop(dep, None)
    return out


# Known engine bugs found by these properties: the broad properties tolerate exactly these so they keep exploring,
# every entry has a strict xfail test with its minimal schedule; remove an entry once its fix lands. The eight EV-13
# findings (stale journal, re-apply after a crash in the rollback, torn action line, unsynced payload, orphan after a
# kill in spawn, onboarding not idempotent x2, unsigned release in a bundle) are fixed: none is tolerated any more.
KNOWN_BUGS: frozenset = frozenset()


def committed_from_reports(reports: list[dict]) -> dict[str, str | None]:
    """What the device *reported* as running per deployment (digest or version key), from terminal reports in order:
    Installed(digest) -> that digest; Removed -> None; Failed of a first install -> None; other Failed -> unchanged."""
    cur: dict[str, str | None] = {}
    for r in reports:
        dep, st = r.get("dep") or r.get("deploymentId"), r.get("state")
        if dep is None:
            continue
        if st == "Installed" and r.get("digest"):
            cur[dep] = r["digest"]
        elif st == "Removed":
            cur[dep] = None
        elif st == "Failed" and "rolledBackTo" in r and r["rolledBackTo"] is None:
            cur[dep] = None
    return {k: v for k, v in cur.items() if v is not None}


def seq_strictly_increasing(outbox: list[dict]) -> list[str]:
    seqs = [r["seq"] for r in outbox]
    return [f"outbox seq not strictly increasing: {seqs}"] if any(b <= a for a, b in zip(seqs, seqs[1:])) else []


def check(cond: bool, msg: str, errs: list[str]):
    if not cond:
        errs.append(msg)


def raise_if(errs: list[str], context: str = ""):
    if errs:
        raise Violation("; ".join(errs) + (f"\n{context}" if context else ""))

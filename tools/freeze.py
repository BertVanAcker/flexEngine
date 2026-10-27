"""Freeze gate of flexEngine 2.0 (FE-3): the public surface only grows.

    python tools/freeze.py check [GATE ...]              compare the surface with api/freeze/*.json (default: all)
    python tools/freeze.py record GATE ... --why TEXT    re-record those baselines (explicit, never automatic)
    python tools/freeze.py record all --why TEXT         re-record every baseline
    python tools/freeze.py list GATE                     the keys a gate collects now (for CHANGES.md rows)

Gates (golden file api/freeze/<name>.json):

  FE-F1  files      every machine-readable spec in src/flexengine2/api/ and its sha256
  FE-F2  specs      the structure of those specs: every JSON Schema / OpenAPI node, leaf by leaf (docs text excluded)
  FE-F3  codes      the reason-code registry: per code its field, stage, profile, meaning and retired flag
  FE-F4  cli        the flexengine CLI: commands, sub-commands, options and positionals (flags, nargs, choices, ...)
  FE-F5  config     the engine configuration schema (src/flexengine2/config/schema.json), as FE-F2
  FE-F6  vectors    the CP-3/CP-4 test vectors in api/vectors/ and their sha256

How `check` decides (api/freeze/README.md):
  * an entry that is gone fails (removed file, field, route, code, option);
  * an entry whose value changed fails (retyped field, tightened pattern, new required field, new constraint on an
    existing node, changed meaning, changed vector); FE-F1 only notes a changed hash, FE-F2 decides;
  * an entry that is new passes only when a row of the contract-change table in CHANGES.md names the gate and
    matches the entry (a back-quoted key or glob). New means a new child of a container (a property, $defs entry,
    path, method, response, media type, parameter, oneOf/anyOf alternative, reason code, command, option), a new
    enum value, or a code marked retired; a new required parameter or option is a change, not an addition.
  * every golden file was recorded with a reason: CHANGES.md must have a row with its date naming its gate.
Exit status: 0 ok, 1 a gate failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

FE = Path(__file__).resolve().parents[1]
SPECS = FE / "src" / "flexengine2" / "api"
CONFIG_SCHEMA = FE / "src" / "flexengine2" / "config" / "schema.json"
VECTORS = FE / "api" / "vectors"
GOLDEN = FE / "api" / "freeze"
CHANGES = FE / "CHANGES.md"

GATES = {"FE-F1": "files", "FE-F2": "specs", "FE-F3": "codes", "FE-F4": "cli", "FE-F5": "config", "FE-F6": "vectors"}
BY_NAME = {v: k for k, v in GATES.items()}

# keys that carry documentation, not contract (ignored by FE-F2 / FE-F5)
DOC_KEYS = {"description", "title", "summary", "$comment", "examples", "example", "help"}
# a new child of one of these is an addition; anything else new under an existing node is a change
CONTAINERS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "paths", "responses",
              "content", "schemas", "headers", "securitySchemes", "parameters", "requestBodies", "oneOf", "anyOf",
              "codes", "commands", "options", "positionals", "attributes", "optional", "byName", "errors", "features"}
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
ADDITIVE_LISTS = {"enum"}


# ======================================================================== collection
def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _spec_files() -> list[Path]:
    return sorted(p for p in SPECS.iterdir() if p.is_file() and p.suffix in (".json", ".yaml"))


def _load(p: Path):
    text = p.read_text(encoding="utf-8")
    return json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)


def _esc(k) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


def flatten(node, ptr: str = "", out: dict | None = None) -> dict:
    """{json pointer: leaf} of a document: scalars, lists of scalars, {} / [] for empty containers. Documentation keys
    are dropped; OpenAPI parameters are keyed by name@in instead of their position."""
    out = {} if out is None else out
    if isinstance(node, dict):
        kept = {k: v for k, v in node.items() if k not in DOC_KEYS}
        if not kept:
            out[ptr] = {}
        for k, v in kept.items():
            flatten(v, f"{ptr}/{_esc(k)}", out)
    elif isinstance(node, list):
        if not node or all(not isinstance(x, (dict, list)) for x in node):
            out[ptr] = list(node)
        elif ptr.endswith("/parameters") and all(isinstance(x, dict) and "name" in x for x in node):
            for x in node:
                flatten(x, f"{ptr}/{_esc(x['name'] + '@' + str(x.get('in', '')))}", out)
        else:
            for i, x in enumerate(node):
                flatten(x, f"{ptr}/{i}", out)
    else:
        out[ptr] = node
    return out


def collect_files() -> dict:
    return {p.name: _sha(p) for p in _spec_files()}


def collect_specs() -> dict:
    out = {}
    for p in _spec_files():
        if p.name.startswith("reason-codes."):
            continue  # FE-F3
        for k, v in flatten(_load(p)).items():
            out[f"{p.name}:{k}"] = v
    return out


def collect_codes() -> dict:
    reg = next(p for p in _spec_files() if p.name.startswith("reason-codes."))
    doc = _load(reg)
    out = {}
    for c in doc["codes"]:
        base = f"{c['code']}"
        out[f"{base}/field"] = c.get("field")
        out[f"{base}/stage"] = c.get("stage")
        out[f"{base}/profile"] = c.get("profile")
        out[f"{base}/meaning"] = c.get("meaning")
        if c.get("retired"):
            out[f"{base}/retired"] = True
    for k, v in sorted((doc.get("fields") or {}).items()):
        out[f"fields/{k}"] = v
    return {f"codes/{k}" if not k.startswith("fields/") else k: v for k, v in out.items()}


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return repr(v)


def collect_cli() -> dict:
    sys.path.insert(0, str(FE / "src"))
    import argparse as ap

    from flexengine2.local.cli import parser

    out: dict = {}

    def walk(p, ptr):
        out[f"{ptr}/prog"] = p.prog.split(" ", 1)[1] if " " in p.prog else p.prog
        for a in p._actions:
            if isinstance(a, ap._HelpAction):
                continue
            if isinstance(a, ap._SubParsersAction):
                out[f"{ptr}/subcommand-required"] = bool(a.required)
                for name, sp in a.choices.items():
                    walk(sp, f"{ptr}/commands/{_esc(name)}")
                continue
            if a.option_strings:
                key = f"{ptr}/options/{_esc(max(a.option_strings, key=len))}"
                out[f"{key}/flags"] = sorted(a.option_strings)
                out[f"{key}/required"] = bool(a.required)
            else:
                key = f"{ptr}/positionals/{_esc(a.dest)}"
            out[f"{key}/action"] = type(a).__name__.lstrip("_")
            out[f"{key}/nargs"] = _jsonable(a.nargs)
            out[f"{key}/default"] = _jsonable(a.default)
            out[f"{key}/type"] = getattr(a.type, "__name__", None) if a.type else None
            if a.choices is not None:
                out[f"{key}/choices"] = [str(c) for c in a.choices]
            out[f"{key}/dest"] = a.dest

    walk(parser(), "flexengine")
    return out


def collect_config() -> dict:
    return {f"schema.json:{k}": v for k, v in flatten(_load(CONFIG_SCHEMA)).items()}


def collect_vectors() -> dict:
    return {str(p.relative_to(VECTORS)): _sha(p) for p in sorted(VECTORS.rglob("*")) if p.is_file()
            and "__pycache__" not in p.parts}


COLLECT = {"files": collect_files, "specs": collect_specs, "codes": collect_codes, "cli": collect_cli,
           "config": collect_config, "vectors": collect_vectors}


# ======================================================================== CHANGES.md rows
ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|([^|]*)\|([^|]*)\|(.*)\|\s*$")


def changes_rows(text: str | None = None) -> list[dict]:
    """Rows of every table in CHANGES.md whose header is | Date | Gate | Entries | Why |."""
    text = CHANGES.read_text(encoding="utf-8") if text is None else text
    rows, in_table = [], False
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^\|\s*Date\s*\|\s*Gate\s*\|\s*Entries\s*\|\s*Why\s*\|\s*$", s):
            in_table = True
            continue
        if not s.startswith("|"):
            in_table = False
            continue
        m = ROW.match(s) if in_table else None
        if m:
            gates = set(re.findall(r"FE-F\d", m.group(2)))
            for a, b in re.findall(r"FE-F(\d)\s*(?:…|\.\.\.|-|–)\s*FE-F(\d)", m.group(2)):
                gates |= {f"FE-F{i}" for i in range(int(a), int(b) + 1)}
            entries = re.findall(r"`([^`]+)`", m.group(3))
            rows.append({"date": m.group(1), "gates": gates, "entries": entries,
                         "all": m.group(3).strip().lower().startswith("all"), "why": m.group(4).strip()})
    return rows


def _row_for(gate: str, key: str, rows: list[dict]) -> dict | None:
    for r in rows:
        if gate in r["gates"] and any(key == pat or fnmatch.fnmatchcase(key, pat) for pat in r["entries"]):
            return r
    return None


# ======================================================================== comparison
def _parents(key: str) -> list[str]:
    """Every proper prefix of a key at a '/' boundary (the key format is '<file>:<pointer>' or a plain path)."""
    out, i = [], key.find("/")
    while i != -1:
        out.append(key[:i])
        i = key.find("/", i + 1)
    return out


def _last(seg_path: str) -> str:
    return seg_path.rsplit("/", 1)[-1].split(":")[-1]


def compare(gate: str, golden: dict, now: dict, rows: list[dict]) -> dict:
    """{"removed", "changed", "added" (with a row), "unmatched" (added, no row), "notes"} of one gate."""
    res = {"removed": [], "changed": [], "added": [], "unmatched": [], "notes": []}
    nodes = set(golden) | {p for k in golden for p in _parents(k)}

    def addition(key, shown):
        r = _row_for(gate, shown, rows)
        (res["added"] if r else res["unmatched"]).append(shown if not r else f"{shown}  ({r['date']})")

    for k in sorted(golden):
        if k not in now:
            res["removed"].append(k)
            continue
        old, new = golden[k], now[k]
        if old == new:
            continue
        if gate == "FE-F1":
            res["notes"].append(f"{k}: content changed (FE-F2 decides whether the change is additive)")
            continue
        if (_last(k) in ADDITIVE_LISTS and isinstance(old, list) and isinstance(new, list)
                and [x for x in new if x in old] == old):
            for x in new:
                if x not in old:
                    addition(k, f"{k}={x}")
            continue
        res["changed"].append(f"{k}: {json.dumps(old)} -> {json.dumps(new)}")

    new_nodes: dict[str, list[str]] = {}
    for k in sorted(set(now) - set(golden)):
        chain = _parents(k) + [k]
        first_new = next((c for c in chain if c not in nodes), k)
        new_nodes.setdefault(first_new, []).append(k)
    for node, keys in sorted(new_nodes.items()):
        parent = node[:node.rfind("/")] if "/" in node else ""
        container = _last(parent) if parent else ""
        grand = _last(parent[:parent.rfind("/")]) if "/" in parent else ""
        if gate == "FE-F3" and node.endswith("/retired"):
            addition(node, node)
            continue
        additive = (gate in ("FE-F1", "FE-F6") or container in CONTAINERS or node.endswith(":")
                    or (grand == "paths" and _last(node) in HTTP_METHODS)
                    or (gate == "FE-F3" and container == "codes"))
        if not additive:
            res["changed"].append(f"{node}: new constraint on an existing node ({', '.join(keys[:3])})")
            continue
        req = [k for k in keys if (k.endswith("/required") and now[k] is True)]
        if req:
            res["changed"].append(f"{node}: added as required ({req[0]}): a new required input breaks callers")
            continue
        addition(node, node)
    return res


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.json"


def load_golden(name: str) -> dict:
    p = _golden_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def check(names: list[str], verbose: bool = True) -> int:
    rows = changes_rows()
    failed = False
    for name in names:
        gate = BY_NAME[name]
        g = load_golden(name)
        if not g:
            print(f"{gate} {name}: FAIL, no baseline {_golden_path(name).relative_to(FE)} (tools/freeze.py record)")
            failed = True
            continue
        rec = g.get("recorded") or {}
        problems = []
        if not any(r["date"] == rec.get("date") and gate in r["gates"] for r in rows):
            problems.append(f"the baseline was recorded on {rec.get('date')} but CHANGES.md has no row of that date "
                            f"naming {gate}")
        res = compare(gate, g["entries"], COLLECT[name](), rows)
        bad = res["removed"] or res["changed"] or res["unmatched"] or problems
        failed |= bool(bad)
        print(f"{gate} {name}: {'FAIL' if bad else 'ok'} ({len(g['entries'])} recorded, {len(res['added'])} "
              f"addition(s) with a CHANGES row)")
        for title, items in (("removed", res["removed"]), ("changed", res["changed"]),
                             ("added without a CHANGES row", res["unmatched"]), ("baseline", problems)):
            for it in items[:50]:
                print(f"    {title}: {it}")
            if len(items) > 50:
                print(f"    ... {len(items) - 50} more {title}")
        if verbose:
            for it in res["added"]:
                print(f"    added: {it}")
            for it in res["notes"]:
                print(f"    note: {it}")
    if failed:
        print("\nfreeze gate FAILED. A removal or a change breaks a frozen contract: undo it, or make it a new version "
              "(api/README.md). An addition needs a row in the contract-change table of CHANGES.md naming the gate "
              "and the entry. Re-recording a baseline is explicit: tools/freeze.py record <gate> --why '...' plus a "
              "CHANGES.md row (api/freeze/README.md).")
    return 1 if failed else 0


def record(names: list[str], why: str, date: str | None = None) -> int:
    date = date or datetime.date.today().isoformat()
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name in names:
        entries = COLLECT[name]()
        doc = {"gate": BY_NAME[name], "name": name, "recorded": {"date": date, "why": why},
               "entries": dict(sorted(entries.items()))}
        _golden_path(name).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{BY_NAME[name]} {name}: recorded {len(entries)} entries -> {_golden_path(name).relative_to(FE)}")
    gates = ", ".join(BY_NAME[n] for n in names)
    print(f"\nnow add this row to the contract-change table of CHANGES.md (a baseline without its row fails check):\n"
          f"| {date} | {gates} | all | {why} |")
    return 0


def _names(args: list[str]) -> list[str]:
    if not args or args == ["all"]:
        return list(GATES.values())
    out = []
    for a in args:
        n = GATES.get(a.upper(), a)
        if n not in BY_NAME:
            raise SystemExit(f"unknown gate {a!r} (one of {', '.join(f'{k} {v}' for k, v in GATES.items())})")
        out.append(n)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("gates", nargs="*")
    p.add_argument("-q", "--quiet", action="store_true", help="do not list the accepted additions")
    p = sub.add_parser("record")
    p.add_argument("gates", nargs="+")
    p.add_argument("--why", required=True, help="the reason (goes into the golden file and the CHANGES row)")
    p.add_argument("--date", help="record date (default today, UTC)")
    p = sub.add_parser("list")
    p.add_argument("gate")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return check(_names(a.gates), verbose=not a.quiet)
    if a.cmd == "record":
        return record(_names(a.gates), a.why, a.date)
    for k, v in sorted(COLLECT[_names([a.gate])[0]]().items()):
        print(f"{k} = {json.dumps(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

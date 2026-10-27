"""Record one layer of a run_tests.sh run in <results>/run-summary.json (read by tools/alpha_exit_check.py), or merge
the results of several CI jobs of one workflow run into one run.

    python tools/run_summary.py <results dir> <layer> <exit code> <seconds> <commit> <python>
    python tools/run_summary.py merge <out dir> <results dir> ... [--meta KEY=VALUE ...]

merge: every input directory is one job's results (its run-summary.json and JUnit files, as uploaded by ci.yml /
nightly.yml). The out directory gets every JUnit file (renamed <file stem>-<job>.xml when two jobs wrote the same
name) and one run-summary.json whose layers sum the jobs that ran the same layer (per job under "jobs"). All inputs
must be of one commit; --meta adds keys such as the workflow run id and URL. Exit status 1 when an input is missing
or the commits differ.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def junit_counts(path: Path) -> dict:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not path.exists():
        return counts
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return counts
    for s in (root.findall("testsuite") if root.tag == "testsuites" else [root]):
        for k in counts:
            counts[k] += int(s.get(k, 0))
    return counts


COUNTS = ("tests", "failures", "errors", "skipped", "passed")


def merge(argv: list[str]) -> int:
    meta, dirs = {}, []
    it = iter(argv)
    for a in it:
        if a == "--meta":
            k, _, v = next(it).partition("=")
            meta[k] = v
        else:
            dirs.append(Path(a))
    if len(dirs) < 2:
        print("usage: run_summary.py merge <out dir> <results dir> ... [--meta KEY=VALUE]", file=sys.stderr)
        return 2
    out, ins = dirs[0], dirs[1:]
    out.mkdir(parents=True, exist_ok=True)
    doc = {"layers": {}, "merged": [], **({"meta": meta} if meta else {})}
    commits = set()
    for d in ins:
        p = d / "run-summary.json"
        if not p.exists():
            print(f"{d}: no run-summary.json", file=sys.stderr)
            return 1
        s = json.loads(p.read_text())
        commits.add(s.get("commit"))
        job = d.name
        doc["merged"].append(job)
        doc.setdefault("python", s.get("python"))
        for layer, v in s.get("layers", {}).items():
            src = d / v.get("junit", "")
            name = v.get("junit", "")
            if src.is_file():
                if (out / name).exists():
                    name = f"{Path(name).stem}-{job}.xml"
                shutil.copy(src, out / name)
            entry = {**v, "junit": name}
            agg = doc["layers"].get(layer)
            if agg is None:
                doc["layers"][layer] = {**entry, "jobs": {job: entry}}
                continue
            for k in COUNTS:
                agg[k] = agg.get(k, 0) + v.get(k, 0)
            agg["exit"] = agg["exit"] or v.get("exit", 1)
            agg["seconds"] = round(agg.get("seconds", 0) + v.get("seconds", 0), 1)
            agg["finished"] = max(agg.get("finished", ""), v.get("finished", ""))
            agg["junit"] = ", ".join(sorted(set(agg["junit"].split(", ")) | {name}))
            agg["jobs"][job] = entry
    if len(commits) != 1:
        print(f"inputs are of different commits: {sorted(map(str, commits))}", file=sys.stderr)
        return 1
    doc["commit"] = commits.pop()
    doc["green"] = all(v["exit"] == 0 and not v.get("failures") and not v.get("errors")
                       for v in doc["layers"].values())
    (out / "run-summary.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    tot = sum(v.get("passed", 0) for v in doc["layers"].values())
    print(f"merged {len(ins)} job(s) of {doc['commit']} into {out}: layers {', '.join(sorted(doc['layers']))}; "
          f"{tot} passed; {'green' if doc['green'] else 'RED'}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "merge":
        return merge(argv[1:])
    res, layer, code, secs, commit, py = argv
    junit = Path(res) / f"junit-{layer}.xml"
    c = junit_counts(junit)
    p = Path(res) / "run-summary.json"
    doc = json.loads(p.read_text()) if p.exists() else {"python": py, "layers": {}}
    doc["commit"] = commit
    doc["layers"][layer] = {"exit": int(code), "seconds": round(float(secs), 1), "junit": junit.name, **c,
                            "passed": c["tests"] - c["failures"] - c["errors"] - c["skipped"],
                            "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    doc["green"] = all(v["exit"] == 0 for v in doc["layers"].values())
    p.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    v = doc["layers"][layer]
    print(f"{layer} EXIT {v['exit']} {v['passed']} passed, {v['failures'] + v['errors']} failed, "
          f"{v['skipped']} skipped in {v['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

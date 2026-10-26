"""Record one layer of a run_tests.sh run in <results>/run-summary.json (read by tools/alpha_exit_check.py).

    python tools/run_summary.py <results dir> <layer> <exit code> <seconds> <commit> <python>
"""

from __future__ import annotations

import json
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


def main(argv: list[str]) -> int:
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

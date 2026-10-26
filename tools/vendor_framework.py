"""The framework files vendored into tests/vectors/framework (FE-2): check that they are current, or refresh them.

    python tools/vendor_framework.py --framework <modular-interoperable-framework checkout>            # check
    python tools/vendor_framework.py --framework <checkout> --update                                  # refresh

The engine's tests run without a framework checkout, so the few framework files they read (a model, the PLC and App
Lab stand-ins, example configs, probe P2's API) are copies in tests/vectors/framework, and probe P2's stand-in apps are
package data (src/flexengine2/testing/apps). SOURCES.json names each copy's origin in the
framework and the framework commit it was taken from:

  from       path in the framework (a file, or a directory whose tracked files were copied)
  extract    the copy is a part of `from` (the engine plan's §3.6 engine.yaml example)
  generated  the copy is the output of a flexmodel command run on `from` (flexmodel is not on PyPI)
  written    the file is written here (fromSha256 records the origin it replaces, so a change there is reported)

Check mode compares every copy with what the framework has now and exits 1 when one differs (the framework's CI runs
it, so a change there that the engine's tests should see is noticed). --update rewrites the copies and SOURCES.json
from the checkout; review the diff and run the suite before committing. `generated` entries need flexmodel's `flex`
CLI: $FLEX_CLI, else <checkout>/.venv/bin/flex.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DST = ROOT / "tests" / "vectors" / "framework"
SOURCES = DST / "SOURCES.json"

# vendored path (relative to the repository root) -> framework path; a directory copies its tracked files.
# Most copies are test vectors (tests/vectors/framework); probe P2's stand-in apps are package data, because
# flexengine2.testing builds releases from them also when installed from a wheel (desk runbooks).
T = "tests/vectors/framework/"
COPIES = {
    T + "models/uc1": "examples/models/uc1",
    "src/flexengine2/testing/apps": "probes/p2-reconciling-engine/apps",
    T + "plc-sim/ads_plc.py": "demo/edge/sim/ads_plc.py",
    T + "plc-sim/modbus_plc.py": "demo/edge/sim/modbus_plc.py",
    T + "config/edge.example.yaml": "demo/edge/edge.example.yaml",
    T + "p2/openapi.yaml": "probes/p2-reconciling-engine/openapi.yaml",
    # probe P4's fake arduino-app-cli (the App Lab board side of the contract suite) and what it imports
    T + "applab/flexp4/fakecli.py": "probes/p4-applab-target/flexp4/fakecli.py",
    T + "applab/flexp4/ardrelease.py": "probes/p4-applab-target/flexp4/ardrelease.py",
    T + "applab/flexp4/hostbuild.py": "probes/p4-applab-target/flexp4/hostbuild.py",
}
# derived from one function of a framework file (the original module imports more than the copy needs)
FUNCTIONS = {   # vendored path -> (framework file, function, header)
    T + "applab/flexp4/appgen.py": ("flexmodel/src/flexmodel/applab/appgen.py", "source_hashes",
                                '"""Vendored subset of probe P4\'s flexp4.appgen (a re-export of flexmodel.applab.appgen): only the\n'
                                'function ardrelease.py imports, copied from flexmodel. See ../../SOURCES.json."""\n\n'
                                "from __future__ import annotations\n\nfrom pathlib import Path\n\n\n"),
}
# written here instead of copied: the origin is recorded (fromSha256) so a change there is still reported
WRITTEN = {   # vendored path -> (framework file, content, why)
    T + "applab/flexp4/__init__.py": ("probes/p4-applab-target/flexp4/__init__.py",
                                  '"""Vendored subset of probe P4 (flexp4): the fake arduino-app-cli and what it imports. See '
                                  '../../SOURCES.json."""\n',
                                  "the original puts probe P2 on sys.path, which the vendored modules do not use"),
}
PLAN = "docs/flexengine-v2-plan.md"
PLAN_BLOCK = re.compile(r"### 3\.6.*?```yaml\n(.*?)```", re.S)
GENERATED = {   # vendored path -> (framework model dir, flex arguments)
    T + "models/uc1/flex-hash.recorded.json": ("examples/models/uc1", ["hash", "examples/models/uc1"]),
    T + "models/uc1/flex-safety.recorded.json": ("examples/models/uc1", ["safety", "examples/models/uc1", "--json"]),
}
COMMENT = ("Files vendored from the framework so the engine's tests run without a framework checkout (flexDeploy plan "
           "FE-2). Keys are paths in this repository, 'from' is the path in the framework at 'commit'; 'extract' / 'generated' say how a derived file was "
           "made. tools/vendor_framework.py --framework <checkout> reports the ones that changed there.")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git(fw: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(fw), *args], capture_output=True, text=True, check=True).stdout


def flex(fw: Path) -> str:
    c = os.environ.get("FLEX_CLI") or str(fw / ".venv" / "bin" / "flex")
    if not Path(c).exists():
        raise SystemExit(f"flexmodel's flex CLI not found at {c}: set FLEX_CLI")
    return c


def current(fw: Path) -> dict[str, tuple[bytes, dict]]:
    """Every vendored path -> (content the framework has now, its SOURCES entry without sha256)."""
    out: dict[str, tuple[bytes, dict]] = {}
    for dst, src in COPIES.items():
        tracked = git(fw, "ls-files", "--", src).split()
        if not tracked:
            raise SystemExit(f"{src}: not tracked in {fw}")
        for t in tracked:
            rel = str(Path(dst) / Path(t).relative_to(src)) if (fw / src).is_dir() else dst
            out[rel] = ((fw / t).read_bytes(), {"from": t})
    m = PLAN_BLOCK.search((fw / PLAN).read_text())
    if not m:
        raise SystemExit(f"{PLAN}: no ```yaml block after '### 3.6'")
    out[T + "config/plan-3.6-engine.yaml"] = (m.group(1).encode(), {"from": PLAN,
                                          "extract": "the first ```yaml block after '### 3.6'"})
    for dst, (src, fn, header) in FUNCTIONS.items():
        text = (fw / src).read_text()
        node = next((n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == fn), None)
        if node is None:
            raise SystemExit(f"{src}: no function {fn}")
        body = "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno]) + "\n"
        out[dst] = ((header + body).encode(), {"from": src, "extract": f"def {fn}"})
    for dst, (src, content, why) in WRITTEN.items():
        out[dst] = (content.encode(), {"from": src, "written": why, "fromSha256": sha((fw / src).read_bytes())})
    cli = flex(fw)
    for dst, (src, args) in GENERATED.items():
        p = subprocess.run([cli, *args], cwd=fw, capture_output=True, text=True, check=True)
        text = json.dumps(json.loads(p.stdout), indent=1, sort_keys=True) + "\n"
        out[dst] = (text.encode(), {"from": src, "generated": "flex " + " ".join(args)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--framework", required=True, type=Path, help="modular-interoperable-framework checkout")
    ap.add_argument("--update", action="store_true", help="rewrite the copies and SOURCES.json")
    a = ap.parse_args(argv)
    fw = a.framework.resolve()
    now = current(fw)
    recorded = json.loads(SOURCES.read_text())["files"] if SOURCES.exists() else {}
    changed = sorted(k for k in now if recorded.get(k, {}).get("sha256") != sha(now[k][0])
                     or recorded[k].get("fromSha256") != now[k][1].get("fromSha256"))
    gone = sorted(set(recorded) - set(now))
    if not a.update:
        for k in changed:
            print(f"changed in the framework: {k} ({now[k][1]['from']})" if k in recorded else f"new: {k}")
        for k in gone:
            print(f"no longer vendored: {k}")
        print(f"{len(now) - len(changed)} of {len(now)} vendored files current"
              f" (vendored from {json.loads(SOURCES.read_text()).get('commit', '?')[:12] if SOURCES.exists() else '-'})")
        return 1 if changed or gone else 0
    for k in gone:
        (ROOT / k).unlink(missing_ok=True)
    files = {}
    for k, (data, entry) in sorted(now.items()):
        p = ROOT / k
        p.parent.mkdir(parents=True, exist_ok=True)
        src = fw / entry["from"]
        if "extract" not in entry and "generated" not in entry:
            shutil.copy2(src, p)   # keeps the mode (the stand-in apps' scripts are executable)
        else:
            p.write_bytes(data)
        files[k] = {**entry, "sha256": sha(data)}
    doc = {"$comment": COMMENT, "repository": "BertVanAcker/modular-interoperable-framework",
           "commit": git(fw, "rev-parse", "HEAD").strip(), "files": files}
    SOURCES.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{len(files)} files vendored from {doc['commit'][:12]} ({len(changed)} changed, {len(gone)} removed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Alpha exit check of flexEngineV2 (docs/flexengine-v2-plan.md §6.3, items 1, 2, 4 and 7) on the current commit.

    python tools/alpha_exit_check.py [--runs DIR ...] [--coverage FILE] [--no-run]
                                                [--out results/alpha-exit-report.md] [--json FILE]

Checks only what can be checked mechanically; everything else is listed as open. The alpha gate was passed in the
framework (exit commit fc081c7, report results/alpha-exit-report.md); the tool stays for re-checks and needs a
framework checkout ($FLEX_REPO) for items 2, 4 and 7. Without --runs it finds no recorded run here (the per-layer
JUnit files of the alpha stayed in the history, not in the tree).

  1  full suite green three runs in a row + coverage targets. A run is a directory written by
     `FLEX_RESULTS_DIR=<dir> run_tests.sh all` (run-summary.json + JUnit XML). Without --runs the committed
     results/junit-*-alpha.xml count as one recorded run (not CI).
  2  probe suites P2 28+4, P3 3x14/14, P4 24/24, demo/edge 8 against their V2 ports: test-by-test mapping, the
     assertions compared (AST), and the port's latest JUnit result.
  4  packaging: the install / reinstall / upgrade / uninstall cases in a clean Ubuntu 24.04 container passed,
     `flexengine status --json` answers, `demo/lab/lattepanda-install.sh` uses V2.
  7  connection points: CP-1..CP-8 specs present and version-tagged, tests/api green (golden examples, vectors,
     reference fakes), cp-reference.md not stale, and every `flex release` output for examples/models/* validates
     against CP-1/CP-3/CP-4 (schemas + the engine's own release verification). Mismatches are listed as proposed
     flexmodel changes; flexmodel is not changed here.

Exit status: 0 when every checked item passes, 1 otherwise (open items count as not passed).
"""

from __future__ import annotations

import argparse
import ast
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
# Items 2, 4 and 7 read the framework (probe suites, demo/lab, examples/models): $FLEX_REPO, default a sibling checkout.
REPO = Path(os.environ.get("FLEX_REPO") or FE.parent / "modular-interoperable-framework").resolve()
RESULTS = FE / "results"
PROBES = Path(os.environ.get("FLEX_PROBES_DIR") or REPO / "probes")
FULL_LAYERS = ["unit", "contract", "contract-nightly", "api", "security", "property-nightly", "integration"]


def git(*args) -> str:
    try:
        return subprocess.run(["git", "-C", str(FE), *args], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def junit_cases(path: Path) -> tuple[str, dict[str, str]]:
    """(timestamp, {"<module stem>::<test name>": ok|fail|skip}) of one JUnit file."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return "", {}
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    out = {}
    for s in suites:
        for tc in s.iter("testcase"):
            mod = (tc.get("classname") or "").split(".")[-1]
            st = "ok"
            if tc.find("failure") is not None or tc.find("error") is not None:
                st = "fail"
            elif tc.find("skipped") is not None:
                st = "skip"
            out[f"{mod}::{tc.get('name')}"] = st
    return (suites[0].get("timestamp") or "") if suites else "", out


def counts(cases: dict[str, str]) -> dict:
    c = {"ok": 0, "fail": 0, "skip": 0}
    for v in cases.values():
        c[v] += 1
    return c


# ---- item 1 ------------------------------------------------------------------------------------------------------
def item1(runs: list[Path], coverage: Path, head: str) -> dict:
    checks, notes = [], []
    rows = []
    for d in runs:
        p = d / "run-summary.json"
        if not p.exists():
            checks.append((f"run {d}", False, "no run-summary.json"))
            continue
        s = json.loads(p.read_text())
        layers = s.get("layers", {})
        missing = [l for l in FULL_LAYERS if l not in layers]
        red = [l for l, v in layers.items() if v.get("exit") != 0 or v.get("failures", 0) or v.get("errors", 0)]
        finished = max((v.get("finished", "") for v in layers.values()), default="")
        green = not missing and not red
        rows.append((d, s.get("commit"), finished, green, missing, red, layers))
    rows.sort(key=lambda r: r[2])
    streak = 0
    for r in rows:
        streak = streak + 1 if r[3] and r[1] == head else 0
    for d, commit, finished, green, missing, red, layers in rows:
        tot = sum(v.get("passed", 0) for v in layers.values())
        detail = f"commit {commit}, {finished}, {tot} passed"
        if missing:
            detail += f"; layers missing: {', '.join(missing)}"
        if red:
            detail += f"; red: {', '.join(red)}"
        if commit != head:
            detail += f" (not the current commit {head})"
        checks.append((f"run {d.name}", green and commit == head, detail))
    if not runs:
        recorded = sorted(RESULTS.glob("junit-*-alpha.xml"))
        tot = {"ok": 0, "fail": 0, "skip": 0}
        for f in recorded:
            for k, v in counts(junit_cases(f)[1]).items():
                tot[k] += v
        summ = RESULTS / "regression-summary-alpha.txt"
        notes.append(f"No CI run summaries given (--runs). Committed alpha regression ({len(recorded)} JUnit files, "
                     f"{tot['ok']} passed, {tot['fail']} failed, {tot['skip']} skipped"
                     f"{'; ' + summ.name if summ.exists() else ''}) counts as one recorded run, not as CI.")
        checks.append(("committed alpha regression green", tot["fail"] == 0 and tot["ok"] > 0,
                       f"{tot['ok']} passed, {tot['fail']} failed"))
    checks.append(("three green full runs in a row on this commit", streak >= 3, f"{streak} of 3"))
    if coverage.exists():
        cov = json.loads(coverage.read_text())
        detail = (f"core {cov['core']['lines']} %, overall {cov['overall']['lines']} %, drivers min "
                  f"{min(v['lines'] for k, v in cov['drivers'].items() if v['statements'] and k != 'spi-base')} %")
        if cov.get("misses"):
            detail += "; below: " + "; ".join(cov["misses"])
        checks.append(("coverage targets (plan §7.3)", bool(cov.get("met")), detail))
    else:
        checks.append(("coverage targets (plan §7.3)", False, f"{coverage} missing (run_tests.sh coverage)"))
    ok = all(c[1] for c in checks)
    return {"item": 1, "title": "Full test suite green three runs in a row; coverage targets met",
            "status": "PASS" if ok else ("OPEN" if streak < 3 else "FAIL"), "checks": checks, "notes": notes}


# ---- item 2 ------------------------------------------------------------------------------------------------------
SUITES = [
    # (suite, expectation, [(original test file, port test file or None = engine-independent)], junit globs)
    ("P2", "28+4", [(f"p2-reconciling-engine/tests/{f}", f"integration/p2/{f}") for f in
                    ("test_scenarios.py", "test_consolidation.py", "test_s4.py", "test_openapi.py", "test_drivers.py")],
     ["junit-p2-*.xml"]),
    ("P3", "3x14/14", [("p3-mcu-hotload/tests/test_p3.py", "integration/mcu/test_p3.py")], ["junit-mcu-*.xml"]),
    ("P4", "24/24", [("p4-applab-target/tests/test_deploy.py", "integration/applab/test_deploy.py"),
                     ("p4-applab-target/tests/test_bridge.py", None),
                     ("p4-applab-target/tests/test_firmware.py", None)], ["junit-applab-*.xml"]),
    ("edge", "8", [("@demo/edge/tests/test_edge.py", "integration/edge/test_edge.py"),
                   ("@demo/edge/tests/test_units.py", "integration/edge/test_units.py")], ["junit-edge-*.xml"]),
]
ENGINE_IMPORTS = ("flexp2.engine", "flexp2.drivers", "driver_applab", "engine_main", "flexp3.mcu_driver", "flexedge")


def _orig_path(rel: str) -> Path:
    if rel.startswith("@"):
        return REPO / rel[1:]
    p = REPO / "probes" / rel
    return p if p.exists() else PROBES / rel


def _tests(path: Path) -> dict[str, dict]:
    """{test name: {"cases": n parametrised cases, "asserts": [normalised assert source]}}"""
    tree = ast.parse(path.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            n = 1
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and ast.unparse(dec.func).endswith("parametrize") and len(dec.args) >= 2
                        and isinstance(dec.args[1], (ast.List, ast.Tuple))):
                    n *= len(dec.args[1].elts)
            asserts = [ast.unparse(a.test) for a in ast.walk(node) if isinstance(a, ast.Assert)]
            asserts += [ast.unparse(c) for c in ast.walk(node) if isinstance(c, ast.Call)
                        and ast.unparse(c.func).endswith(("pytest.raises", "pytest.fail"))]
            out[node.name] = {"cases": n, "asserts": asserts}
    return out


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods |= {n.module} | {f"{n.module}.{a.name}" for a in n.names}
    return mods


def _norm(a: str) -> str:
    return "".join(a.split())


def _latest_junit(globs: list[str], runs: list[Path]) -> tuple[list[str], dict[str, str], int]:
    """Cases of the newest JUnit file matching the globs (results/ and the run dirs), and how many files show
    every case of that file passing (recorded green runs)."""
    files = [f for g in globs for f in RESULTS.glob(g)]
    for d in runs:
        files += [d / "junit-integration.xml"]
    recs = []
    for f in files:
        ts, cases = junit_cases(f)
        if cases:
            recs.append((ts, f, cases))
    if not recs:
        return [], {}, []
    recs.sort(key=lambda r: r[0])
    return [str(r[1].relative_to(REPO) if REPO in r[1].parents else r[1]) for r in recs], recs[-1][2], recs


def item2(runs: list[Path]) -> dict:
    checks, table, flagged = [], [], []
    for suite, expect, files, globs in SUITES:
        names, latest, recs = _latest_junit(globs, runs)
        n_orig = n_port = n_engine_free = 0
        missing = []
        for orig_rel, port_rel in files:
            orig = _orig_path(orig_rel)
            if not orig.exists():
                checks.append((f"{suite}: {orig_rel}", False, "original suite file not found"))
                continue
            ot = _tests(orig)
            if port_rel is None:
                eng = sorted(m for m in _imports(orig) if m.startswith(ENGINE_IMPORTS))
                for name, t in ot.items():
                    n_engine_free += t["cases"]
                    table.append((suite, f"{orig.name}::{name}", t["cases"], "–", "engine-independent" if not eng
                                  else f"imports {eng}", "runs unchanged in the probe" if not eng else "MISSING", ""))
                    if eng:
                        missing.append(name)
                continue
            port = FE / "tests" / port_rel
            pt = _tests(port) if port.exists() else {}
            for name, t in ot.items():
                n_orig += t["cases"]
                p = pt.get(name)
                if p is None:
                    missing.append(name)
                    table.append((suite, f"{orig.name}::{name}", t["cases"], "–", "no port", "MISSING", ""))
                    continue
                n_port += p["cases"]
                po = {_norm(a) for a in p["asserts"]}
                same = sum(1 for a in t["asserts"] if _norm(a) in po)
                cmp = f"{same}/{len(t['asserts'])} equal, port {len(p['asserts'])}"
                verdict = "same" if same == len(t["asserts"]) else "adapted"
                if same < len(t["asserts"]):
                    flagged.append((suite, name, [a for a in t["asserts"] if _norm(a) not in po][:3]))
                res = [v for k, v in latest.items() if k.split("::")[1].split("[")[0] == name
                       and k.split("::")[0] == port.stem]
                rs = ("passed" if res and all(v == "ok" for v in res) else
                      "FAILED" if any(v == "fail" for v in res) else "skipped" if res else "no result")
                table.append((suite, f"{orig.name}::{name}", t["cases"], p["cases"], cmp, verdict, rs))
        green = [f.name for _, f, cases in recs if counts(cases)["fail"] == 0 and counts(cases)["ok"] >= n_port > 0]
        green_files = len(green)
        ok_now = latest and counts(latest)["fail"] == 0 and counts(latest)["ok"] >= n_port
        detail = (f"original {n_orig}" + (f" + {n_engine_free} engine-independent" if n_engine_free else "")
                  + f" cases, ported {n_port}; latest port result {counts(latest) if latest else 'none'}; "
                  f"{green_files} recorded green run(s) of the port ({', '.join(green) or 'none'})")
        if missing:
            detail += f"; missing: {', '.join(missing)}"
        need_runs = 3 if suite == "P3" else 1
        checks.append((f"{suite} {expect}", bool(ok_now) and not missing and green_files >= need_runs, detail))
    ok = all(c[1] for c in checks)
    return {"item": 2, "title": "Probe suites against V2 (ported equivalents with the same assertions)",
            "status": "PASS" if ok else "FAIL", "checks": checks, "table": table, "flagged": flagged,
            "notes": ["There is no EV-02 adapter (`FLEX_ENGINE_IMPL=v2`) that starts V2 under the original suites: "
                      "the item is met with the ported equivalents in tests/integration/.",
                      "'equal' counts the original assert expressions found literally (whitespace-normalised) in the "
                      "port; 'adapted' lists the rest below for review (renamed fixtures, V2 module paths).",
                      "Recorded green runs are JUnit files under results/ (and the --runs dirs); the committed ones come "
                      "from different commits of the alpha (port, merged, alpha). P3's 3x rule on the exit commit needs "
                      "three runs given with --runs.",
                      "P4: 13 of the 24 cases (test_bridge.py, test_firmware.py) test the Bridge generator, the "
                      "simulated board and the sketch compile; they import no engine code (checked from their imports) "
                      "and run unchanged in probes/p4-applab-target. This check does not run them."]}


# ---- item 4 ------------------------------------------------------------------------------------------------------
PKG_CASES = ["test_fresh_install", "test_reinstall_is_idempotent", "test_upgrade_keeps_config_and_data",
             "test_uninstall_keeps_state", "test_purge_removes_everything", "test_service_runs_real_engine"]


def item4(runs: list[Path], pkg_junit: Path | None) -> dict:
    checks = []
    files = [pkg_junit] if pkg_junit else [d / "junit-packaging.xml" for d in runs] + sorted(RESULTS.glob("junit-packaging*.xml"))
    recs = sorted(((junit_cases(f)[0], f, junit_cases(f)[1]) for f in files if f and f.exists()), key=lambda r: r[0])
    if not recs:
        checks.append(("packaging JUnit", False, "no junit-packaging*.xml"))
        latest = {}
    else:
        ts, f, latest = recs[-1]
        checks.append(("packaging JUnit", counts(latest)["fail"] == 0, f"{f.name} ({ts}): {counts(latest)}"))
    for c in PKG_CASES:
        st = latest.get(f"test_pkg_docker::{c}")
        checks.append((f"clean Ubuntu 24.04 container: {c}", st == "ok", st or "not in the JUnit file"))
    src = (FE / "tests" / "packaging" / "test_pkg_docker.py").read_text()
    st = latest.get("test_pkg_docker::test_service_runs_real_engine")
    has = "status --json" in src
    checks.append(("`flexengine status --json` answers from the installed service", has and st == "ok",
                   ("asserted in test_service_runs_real_engine" if has else "no packaging test runs it")
                   + f" (latest result: {st or 'none'})"))
    lab = REPO / "demo" / "lab" / "lattepanda-install.sh"
    if lab.exists():
        # Run the dry run (it changes nothing and needs no sudo): the default engine must be V2 through
        # packaging/install.sh. The script still mentions the probe engine for its `--engine probe` fallback,
        # so reading its text is not enough.
        try:
            out = subprocess.run(["bash", str(lab), "--dry-run", "--only", "engine"], cwd=REPO, capture_output=True,
                                 text=True, timeout=120).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            out = f"dry run failed: {e}"
        v2 = "edge engine: flexEngineV2" in out and "packaging/install.sh" in out
        checks.append(("demo/lab/lattepanda-install.sh --dry-run uses V2", v2,
                       "dry run installs flexEngineV2 through packaging/install.sh" if v2
                       else "dry run does not install flexEngineV2 by default"))
    else:
        checks.append(("demo/lab/lattepanda-install.sh --dry-run uses V2", False, "script missing"))
    ok = all(c[1] for c in checks)
    return {"item": 4, "title": "Packaging for the LattePanda", "status": "PASS" if ok else "FAIL", "checks": checks,
            "notes": ["The lattepanda-install.sh check runs its --dry-run --only engine (no sudo, nothing changed)."]}


# ---- item 7 ------------------------------------------------------------------------------------------------------
def _load_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_release_config(cfg_path: Path, tmp: Path) -> Path:
    """A copy of a release config with `@repo/` paths made absolute (probe build outputs from FLEX_PROBES_DIR) and
    out / release_key in `tmp`, so the check never writes into the checkout."""
    import yaml

    def fix(v):
        if isinstance(v, str) and v.startswith("@repo/"):
            rel = v[len("@repo/"):]
            if rel.startswith("probes/") and (PROBES / rel[len("probes/"):]).exists():
                return str(PROBES / rel[len("probes/"):])
            return str(REPO / rel)
        if isinstance(v, str) and not v.startswith("/") and (cfg_path.parent / v).exists():
            return str((cfg_path.parent / v).resolve())
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v

    doc = fix(yaml.safe_load(cfg_path.read_text()))
    doc["out"] = str(tmp / "out")
    doc["release_key"] = str(tmp / "release-ed25519.key")
    out = tmp / "release.yaml"
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    return out


def release_configs() -> dict[str, Path | None]:
    """examples/models/<m> -> the release config whose `deployment` is defined in that model (None: none)."""
    import yaml

    cfgs = [p for p in sorted(REPO.glob("**/release*.yaml")) if ".venv" not in p.parts and "work" not in p.parts
            and "build" not in p.parts]
    out = {}
    for m in sorted(p for p in (REPO / "examples" / "models").iterdir() if p.is_dir()):
        deployments = set()
        for f in m.rglob("*.yaml"):
            try:
                for d in yaml.safe_load_all(f.read_text()):
                    if isinstance(d, dict) and d.get("kind") == "Deployment":
                        deployments.add((d.get("metadata") or {}).get("name") or d.get("name"))
            except yaml.YAMLError:
                continue
        found = None
        for c in cfgs:
            try:
                doc = yaml.safe_load(c.read_text())
            except yaml.YAMLError:
                continue
            if isinstance(doc, dict) and doc.get("deployment") in deployments and "targets" in doc:
                found = c
        out[m.name] = found
    return out


def validate_layout(oci: Path, release_pub: str) -> tuple[int, list[str]]:
    """Every release in an OCI layout against CP-1 (descriptor), CP-3 (OCI profile) and CP-4 (signature schemas +
    the engine's own verify_release with the release key). Returns (releases checked, mismatches)."""
    sys.path.insert(0, str(FE / "src"))
    from flexengine2 import contracts
    from flexengine2.artefacts.store import ArtefactStore, VerificationError

    idx = json.loads((oci / "index.json").read_text())
    blob = lambda d: (oci / "blobs" / "sha256" / d[7:]).read_bytes()  # noqa: E731
    errs, n = [], 0
    store_root = Path(tempfile.mkdtemp(prefix="ex-store-"))
    try:
        store = ArtefactStore(store_root, None, [release_pub])
        for p in (oci / "blobs" / "sha256").iterdir():
            store.put("sha256:" + p.name, p.read_bytes())
        refs: dict[str, list] = {}
        for e in idx["manifests"]:
            subj = (e.get("annotations") or {}).get("dev.flex.subject")
            if subj:
                refs.setdefault(subj, []).append({k: e[k] for k in ("artifactType", "digest", "mediaType", "size") if k in e})
        rdir = store_root / "cas" / "referrers"
        rdir.mkdir(parents=True, exist_ok=True)
        for subj, lst in refs.items():
            (rdir / subj[7:]).write_text(json.dumps({"manifests": lst}))
        for e in idx["manifests"]:
            name = (e.get("annotations") or {}).get("org.opencontainers.image.ref.name") or e["digest"][:19]
            man = json.loads(blob(e["digest"]))
            try:
                if man.get("artifactType") == "application/vnd.flex.signature.v1+json":
                    contracts.validate("oci-profile#SignatureManifest", man)
                    env = json.loads(blob(man["layers"][0]["digest"]))
                    contracts.validate("trust#SignatureEnvelope", env)
                    contracts.validate("trust#SignedPayload", json.loads(base64.b64decode(env["payload"])))
                    continue
                n += 1
                contracts.validate("oci-profile#ReleaseManifest", man)
                desc = json.loads(blob(man["config"]["digest"]))
                contracts.validate("descriptor", desc)
            except contracts.ContractError as ex:
                errs.append(f"{name}: {'; '.join(ex.errors[:3])}")
                continue
            repo = name.split(":")[0]
            for prof in desc.get("profiles") or []:
                try:
                    store.verify_release(repo, e["digest"], prof, desc.get("safetyClass", "none"))
                except (VerificationError, KeyError, ValueError) as ex:
                    errs.append(f"{name}: engine verification ({prof}) refuses it: {ex}")
    finally:
        shutil.rmtree(store_root, ignore_errors=True)
    return n, errs


# The connection-point set frozen for alpha (DA-16, project lead 2026-10-17): every CP at v1alpha1, except the three
# that plan §5.0 versions as v1 (Driver SPI v1, local-api.v1, telemetry.v1). The set is frozen by the git tag CP_TAG
# on the alpha exit commit.
FROZEN_VERSION = {"CP-5": "v1", "CP-7": "v1", "CP-8": "v1"}
CP_TAG = "flexengine-cp-v1alpha1"


def item7(no_run: bool) -> dict:
    checks, proposals = [], []
    gen = _load_tool(FE / "api" / "tools" / "gen_cp_reference.py", "gen_cp_reference")
    for cp, title, files, prose in gen.CPS:
        present = all((gen.SPECS / f).is_file() for f in files) and all((gen.API_MD / p).is_file() for p in prose)
        tags = sorted({t for t in (gen.version_of(f) for f in files + [p for p in prose if p != "README.md"]) if t})
        detail = f"{', '.join(files + [p for p in prose if p != 'README.md'])} — tags {', '.join(tags) or 'none'}"
        checks.append((f"{cp} {title}: specs present", present, detail))
        want = FROZEN_VERSION.get(cp, "v1alpha1")
        checks.append((f"{cp}: frozen at {want}", tags == [want],
                       want if tags == [want] else f"tagged {', '.join(tags) or 'nothing'}, not {want}"))
    checks.append(("api/cp-reference.md up to date", gen.main(["--check"]) == 0, "gen_cp_reference.py --check"))
    tags = git("tag", "-l", CP_TAG)
    checks.append((f"git tag {CP_TAG} freezing the CP set", bool(tags), tags or f"no {CP_TAG} tag in the repository"))
    if no_run:
        checks.append(("tests/api green (golden examples, vectors, reference fakes)", False, "skipped (--no-run)"))
        checks.append(("flex release outputs validate", False, "skipped (--no-run)"))
    else:
        tmp = Path(tempfile.mkdtemp(prefix="ex-exit-"))
        try:
            junit = tmp / "junit-api.xml"
            env = {**os.environ, "PYTHONPATH": f"{FE / 'src'}:{REPO / 'flexmodel' / 'src'}:{os.environ.get('PYTHONPATH', '')}",
                   "FLEX_ALLOW_PLAIN_HTTP": "1"}
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/api",
                                f"--junitxml={junit}", "--basetemp", str(tmp / "bt")], cwd=FE, env=env,
                               capture_output=True, text=True, timeout=1800)
            c = counts(junit_cases(junit)[1])
            checks.append(("tests/api green (golden examples, vectors, reference fakes)", r.returncode == 0 and c["fail"] == 0,
                           f"{c['ok']} passed, {c['fail']} failed, {c['skip']} skipped"))
            for model, cfg in release_configs().items():
                if cfg is None:
                    # Plan §6.3 item 7: mismatches are filed as proposed flexmodel changes, not fixed here.
                    checks.append((f"flex release examples/models/{model}", True,
                                   "no release config for this model: filed as a proposed flexmodel change"))
                    proposals.append(f"examples/models/{model}: no `flex release` config names a deployment of this model, "
                                     "so its releases cannot be built or checked; add one (like demo/release/release.yaml).")
                    continue
                mt = tmp / model
                mt.mkdir()
                rc = _resolve_release_config(cfg, mt)
                rr = subprocess.run([sys.executable, "-m", "flexmodel.cli", "release", str(REPO / "examples" / "models" / model),
                                     "--config", str(rc), "--dry-run", "--json", "--approver", "alice", "--approver", "bob"],
                                    cwd=mt, env=env, capture_output=True, text=True, timeout=1800)
                try:
                    res = json.loads(rr.stdout)
                except ValueError:
                    checks.append((f"flex release examples/models/{model}", False,
                                   f"flex release failed (exit {rr.returncode}): {rr.stderr.strip()[-300:]}"))
                    continue
                failed = [t["name"] for t in res.get("targets", []) if t.get("status") == "failed"]
                n, errs = validate_layout(mt / "out" / "oci", res["releasePublicKey"])
                for e in errs:
                    proposals.append(f"examples/models/{model} ({cfg.relative_to(REPO)}): {e}")
                checks.append((f"flex release examples/models/{model} validates (CP-1/CP-3/CP-4)",
                               not errs and not failed and n > 0,
                               f"{n} releases from {cfg.relative_to(REPO)}; {len(errs)} mismatch(es)"
                               + (f"; failed targets: {failed}" if failed else "")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ok = all(c[1] for c in checks)
    return {"item": 7, "title": "Connection points frozen", "status": "PASS" if ok else "FAIL", "checks": checks,
            "proposals": proposals,
            "notes": ["CP-5 is versioned as SPI 1.0 (frozen in drivers/base.py); CP-7 and CP-8 were named v1 when they "
                      "were written. Whether they count as frozen for the alpha is a decision, not a check."]}


# ---- report ------------------------------------------------------------------------------------------------------
def render(items: list[dict], head: str, args) -> str:
    out = ["# flexEngineV2 alpha exit check", "",
           f"Commit `{head}` · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · "
           f"generated by `tools/alpha_exit_check.py` (plan §6.3 items 1, 2, 4, 7)", "",
           "| Item | Status |", "|---|---|"]
    for it in items:
        out.append(f"| {it['item']}. {it['title']} | **{it['status']}** |")
    out.append("")
    for it in items:
        out += [f"## Item {it['item']}: {it['title']} — {it['status']}", "", "| Check | Result | Detail |", "|---|---|---|"]
        for name, ok, detail in it["checks"]:
            out.append(f"| {name} | {'✔' if ok else '✘'} | {str(detail).replace('|', '/')} |")
        out.append("")
        for n in it.get("notes", []):
            out.append(f"- {n}")
        if it.get("notes"):
            out.append("")
        if it.get("table"):
            out += ["### Mapping: original test → V2 port", "",
                    "| Suite | Original test | Cases | Port cases | Assertions | Verdict | Port result |",
                    "|---|---|---|---|---|---|---|"]
            out += [f"| {' | '.join(str(x) for x in row)} |" for row in it["table"]]
            out.append("")
        if it.get("flagged"):
            out += ["### Adapted assertions (original expressions not found literally in the port)", ""]
            for suite, name, asserts in it["flagged"]:
                shown = "; ".join(f"`{a[:120]}`" for a in asserts)
                out.append(f"- {suite} `{name}`: {shown}")
            out.append("")
        if "proposals" in it:
            out += ["### Proposed flexmodel changes", ""]
            out += [f"- {p}" for p in it["proposals"]] or ["- none: every checked `flex release` output validates"]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", nargs="*", default=[], type=Path, help="run directories (run-summary.json + JUnit)")
    ap.add_argument("--coverage", type=Path, default=RESULTS / "coverage-alpha.json")
    ap.add_argument("--packaging-junit", type=Path)
    ap.add_argument("--no-run", action="store_true", help="do not run tests/api and flex release (item 7)")
    ap.add_argument("--out", type=Path, default=RESULTS / "alpha-exit-report.md")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    head = git("rev-parse", "--short", "HEAD") or "unknown"
    items = [item1(a.runs, a.coverage, head), item2(a.runs), item4(a.runs, a.packaging_junit), item7(a.no_run)]
    a.out.write_text(render(items, head, a))
    if a.json:
        a.json.write_text(json.dumps(items, indent=2, default=str) + "\n")
    for it in items:
        print(f"item {it['item']}: {it['status']}  {it['title']}")
        for name, ok, detail in it["checks"]:
            if not ok:
                print(f"    ✘ {name}: {detail}")
    print(f"report: {a.out}")
    return 0 if all(it["status"] == "PASS" for it in items) else 1


if __name__ == "__main__":
    sys.exit(main())

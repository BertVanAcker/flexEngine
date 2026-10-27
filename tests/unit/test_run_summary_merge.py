"""FE-3: tools/run_summary.py merge turns the jobs of one CI workflow run into one run (the evidence of alpha exit
item 1, read by tools/alpha_exit_check.py --runs), and nightly.yml produces such a run with every layer item 1 needs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

FE = Path(__file__).resolve().parents[2]


def _tool(name):
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", FE / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _tool("run_summary")


def _job(d: Path, commit: str, layers: dict) -> Path:
    d.mkdir(parents=True)
    doc = {"commit": commit, "python": "py", "layers": {}}
    for layer, (passed, exit_code) in layers.items():
        (d / f"junit-{layer}.xml").write_text(f'<testsuite name="{layer}" tests="{passed}"/>')
        doc["layers"][layer] = {"exit": exit_code, "tests": passed, "failures": 0, "errors": 0, "skipped": 0,
                                "passed": passed, "seconds": 1.0, "junit": f"junit-{layer}.xml",
                                "finished": "2026-10-27T01:00:00Z"}
    (d / "run-summary.json").write_text(json.dumps(doc))
    return d


def test_merge_sums_layers_and_keeps_every_junit(tmp_path):
    a = _job(tmp_path / "unit-py3.11", "abc1234", {"unit": (10, 0)})
    b = _job(tmp_path / "unit-py3.13", "abc1234", {"unit": (12, 0), "api": (5, 0)})
    out = tmp_path / "run"
    assert rs.main(["merge", str(out), str(a), str(b), "--meta", "runId=42"]) == 0
    doc = json.loads((out / "run-summary.json").read_text())
    assert doc["commit"] == "abc1234" and doc["green"] and doc["meta"] == {"runId": "42"}
    assert doc["layers"]["unit"]["passed"] == 22 and set(doc["layers"]["unit"]["jobs"]) == {"unit-py3.11",
                                                                                           "unit-py3.13"}
    assert sorted(p.name for p in out.glob("*.xml")) == ["junit-api.xml", "junit-unit-unit-py3.13.xml",
                                                         "junit-unit.xml"]


def test_merge_refuses_mixed_commits_and_missing_results(tmp_path):
    a = _job(tmp_path / "a", "abc1234", {"unit": (1, 0)})
    b = _job(tmp_path / "b", "def5678", {"api": (1, 0)})
    assert rs.main(["merge", str(tmp_path / "o1"), str(a), str(b)]) == 1
    (tmp_path / "empty").mkdir()
    assert rs.main(["merge", str(tmp_path / "o2"), str(a), str(tmp_path / "empty")]) == 1


def test_a_red_job_makes_the_run_red(tmp_path):
    a = _job(tmp_path / "a", "abc1234", {"unit": (1, 0)})
    b = _job(tmp_path / "b", "abc1234", {"integration": (1, 1)})
    assert rs.main(["merge", str(tmp_path / "o"), str(a), str(b)]) == 0
    assert json.loads((tmp_path / "o" / "run-summary.json").read_text())["green"] is False


def test_nightly_full_run_covers_every_layer_alpha_exit_item_1_needs():
    wf = yaml.safe_load((FE / ".github" / "workflows" / "nightly.yml").read_text())
    jobs = wf["jobs"]
    layers = set(jobs["nightly"]["strategy"]["matrix"]["layer"])
    push = next(s["run"] for s in jobs["push-layers"]["steps"] if "run_tests.sh" in (s.get("run") or ""))
    layers |= set(push.split("run_tests.sh", 1)[1].split())
    full = _tool("alpha_exit_check").FULL_LAYERS
    assert set(full) <= layers, set(full) - layers
    merge = next(s["run"] for s in jobs["full-run"]["steps"] if "run_summary.py merge" in (s.get("run") or ""))
    for layer in jobs["nightly"]["strategy"]["matrix"]["layer"]:
        assert f"jobs/nightly-{layer}" in merge
    assert "jobs/nightly-push" in merge
    assert set(jobs["full-run"]["needs"]) == {"nightly", "push-layers", "full-coverage"}

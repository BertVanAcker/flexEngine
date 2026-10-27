"""Coverage gate (tools/coverage_report.py, plan §7.3): the relay is gated once it can be measured, and
FLEX_COVERAGE_REQUIRE=relay turns a run without flexmodel into a miss (the framework's CI measures the relay and
applab, which flexEngine's own CI leaves out or defers)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

FE = Path(__file__).resolve().parents[2]


def _tool(name):
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", FE / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _tool("coverage_report")


def _file(n, covered, branches=0, covered_branches=0):
    return {"summary": {"num_statements": n, "covered_lines": covered, "missing_lines": n - covered,
                        "num_branches": branches, "covered_branches": covered_branches}}


def _cov(relay=(100, 90), applab=(100, 90)):
    root = "/src/flexengine2/"
    files = {
        "core/reconciler.py": _file(100, 95, 10, 9),
        "artefacts/store.py": _file(100, 95, 10, 9),
        "gateway/relay.py": _file(*relay),
        "drivers/applab/driver.py": _file(*applab),
        "local/api.py": _file(100, 95),
    }
    return {"files": {root + k: v for k, v in files.items()}}


@pytest.fixture
def flexmodel(monkeypatch):
    def set_(present: bool):
        monkeypatch.setattr(cr, "flexmodel_importable", lambda: present)
    return set_


def test_relay_gated_when_measured(flexmodel):
    flexmodel(True)
    s = cr.summarise(_cov(relay=(100, 90)))
    assert s["met"] and s["relay"]["lines"] == 90.0 and s["notMeasured"] == {}
    s = cr.summarise(_cov(relay=(100, 70)))
    assert not s["met"] and s["misses"] == ["relay 70.0 % < 80.0 %"]


def test_relay_without_flexmodel_is_not_measured_and_passes_unless_required(flexmodel):
    flexmodel(False)
    s = cr.summarise(_cov(relay=(100, 0)))
    assert s["met"] and "gateway/relay.py" in s["notMeasured"] and s["relay"]["statements"] == 0
    s = cr.summarise(_cov(relay=(100, 0)), require=("relay",))
    assert not s["met"] and s["misses"][0].startswith("relay not measured (FLEX_COVERAGE_REQUIRE=relay)")


def test_applab_deferred_only_when_asked(flexmodel):
    flexmodel(True)
    s = cr.summarise(_cov(applab=(100, 50)))
    assert s["misses"] == ["driver applab 50.0 % < 80.0 %"]
    s = cr.summarise(_cov(applab=(100, 50)), defer=("applab",))
    assert s["met"] and s["deferred"] == ["driver applab 50.0 % < 80.0 %"]


def test_cli_reads_require_and_rejects_unknown(tmp_path, monkeypatch, flexmodel, capsys):
    flexmodel(False)
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps(_cov()))
    out = tmp_path / "summary.json"
    monkeypatch.delenv("FLEX_COVERAGE_DEFER", raising=False)
    monkeypatch.delenv("FLEX_COVERAGE_REQUIRE", raising=False)
    assert cr.main([str(cov), "--out", str(out)]) == 0
    monkeypatch.setenv("FLEX_COVERAGE_REQUIRE", "relay")
    assert cr.main([str(cov), "--out", str(out)]) == 1
    assert "relay not measured" in capsys.readouterr().out
    assert json.loads(out.read_text())["met"] is False
    monkeypatch.setenv("FLEX_COVERAGE_REQUIRE", "relay,typo")
    with pytest.raises(SystemExit) as e:
        cr.main([str(cov)])
    assert e.value.code == 2

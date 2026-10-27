"""CP-8 telemetry v1 (telemetry.v1.schema.json): what an engine run emits validates (log lines, action log, span file,
metrics), and every span name, log event and action the sources can emit is registered in the schema (a new one is
an additive change: register it first)."""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path

import pytest

from flexengine2 import contracts

from _api_helpers import SRC

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from fe_unit_fakes import FakeDriver, deployment, make_engine, set_desired  # noqa: E402

SCHEMA = contracts.load("telemetry")
DEFS = SCHEMA["$defs"]
SCANNED = [p for p in sorted(SRC.rglob("*.py")) if "testing" not in p.relative_to(SRC).parts]


def _text(node):
    """A string constant, an f-string as a template with {} for each field, or both arms of `a if c else b`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)]
    if isinstance(node, ast.IfExp):
        return _text(node.body) + _text(node.orelse)
    return []


def scan() -> dict[str, set[str]]:
    out = {"events": set(), "spans": set(), "actions": set(), "driver_ops": set()}
    for p in SCANNED:
        for n in ast.walk(ast.parse(p.read_text())):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in ("log", "_log", "emit", "warning", "error"):
                for k in n.keywords:
                    if k.arg == "event":
                        out["events"].update(_text(k.value))
                if name in ("log", "_log") and len(n.args) >= 3:
                    out["events"].update(_text(n.args[2]))
            elif name == "span" and n.args:
                out["spans"].update(_text(n.args[0]))
            elif name == "action" and n.args:
                out["actions"].update(_text(n.args[0]))
            elif name == "_driver_call" and n.args:
                out["driver_ops"].update(_text(n.args[0]))
    return out


def _matches(def_name: str, value: str) -> bool:
    return contracts.is_valid(f"telemetry#{def_name}", value)


def test_every_log_event_of_the_sources_is_registered():
    found = scan()["events"]
    assert len(found) > 20
    for ev in sorted(found):
        if ev == "action.{}":
            continue  # action.<kind>: checked through ActionKind below
        if ev == "build.{}":
            for phase in ("started", "progress", "log", "succeeded", "failed"):
                assert contracts.is_valid("telemetry#LogRecord", _rec(event=f"build.{phase}"))
            continue
        assert "{}" not in ev, f"dynamic log event {ev!r}: register its pattern in telemetry#LogRecord.event"
        assert contracts.is_valid("telemetry#LogRecord", _rec(event=ev)), f"log event {ev!r} is not in telemetry.v1"


def test_every_action_is_registered_and_has_its_log_event():
    found = scan()["actions"]
    assert {"apply", "commit", "rollback", "rollback-resume", "remove"} <= found
    assert found <= set(DEFS["ActionKind"]["enum"]), found - set(DEFS["ActionKind"]["enum"])
    for kind in DEFS["ActionKind"]["enum"]:
        assert contracts.is_valid("telemetry#LogRecord", _rec(event=f"action.{kind}"))


def test_every_span_name_is_registered():
    s = scan()
    assert s["driver_ops"] and "driver.{}" in s["spans"]
    names = (s["spans"] - {"driver.{}"}) | {f"driver.{op}" for op in s["driver_ops"]}
    for name in sorted(names):
        concrete = name.replace("{}", "d1")
        assert _matches("SpanName", concrete), f"span {name!r} is not in telemetry.v1"


def _rec(**kw) -> dict:
    return {"ts": "2026-10-27T10:00:00.123Z", "level": "info", "device": "dev-a", "event": "log", "msg": "m", **kw}


@pytest.fixture
def otel_file(tmp_path, monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from flexengine2.obs import tracing

    monkeypatch.delenv("FLEX_OTEL", raising=False)
    tracing.reset()
    path = tmp_path / "spans.jsonl"
    assert tracing.configure({"enabled": True, "file": str(path)})
    yield path
    tracing.reset()


def test_an_engine_run_emits_cp8_records(tmp_path, otel_file):
    from flexengine2.obs import logging as flog
    from flexengine2.obs import tracing

    out = io.StringIO()
    flog.configure("json", stream=out)
    try:
        drv = FakeDriver()
        e = make_engine(tmp_path, drivers={"native": drv}, healthEveryTicks=100)
        set_desired(e, 1, [deployment("d1")])
        e.tick()
        drv.healthy = False                                  # 1.1.0 fails its gate: rollback to 1.0.0
        set_desired(e, 2, [deployment("d1", "1.1.0")])
        e.tick()
        drv.healthy = True
        set_desired(e, 3, [])                                # removal by absence
        e.tick()
        tracing.flush()
        metrics = e.metrics()
    finally:
        flog.reset()

    logs = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert logs
    for rec in logs:
        contracts.validate("telemetry#LogRecord", rec)
    actions = [json.loads(ln) for ln in e.actions_path.read_text().splitlines()]
    assert {"apply", "rollback", "remove"} <= {a["action"] for a in actions}
    for a in actions:
        contracts.validate("telemetry#ActionRecord", a)
    spans = [json.loads(ln) for ln in otel_file.read_text().splitlines() if ln.strip()]
    assert {"engine.install d1", "driver.apply", "engine.health-gate", "engine.rollback", "driver.remove"} \
        <= {s["name"] for s in spans}
    for s in spans:
        contracts.validate("telemetry#SpanRecord", s)
    contracts.validate("telemetry#Metrics", json.loads(json.dumps(metrics)))


def test_span_attributes_are_closed():
    bad = {"service": "flex-engine-a", "name": "driver.apply", "traceId": "0" * 32, "spanId": "0" * 16,
           "parentSpanId": None, "start": 1.0, "end": 2.0, "kind": "CLIENT", "status": "UNSET",
           "attributes": {"flex.unregistered": 1}, "links": []}
    assert not contracts.is_valid("telemetry#SpanRecord", bad)
    bad["attributes"] = {"flex.driver": "native"}
    assert contracts.is_valid("telemetry#SpanRecord", bad)


def test_prose_spec_names_the_schema_and_every_span():
    text = (SRC.parents[1] / "api" / "telemetry.v1.md").read_text()
    assert "telemetry.v1.schema.json" in text
    for name in DEFS["SpanName"]["anyOf"][0]["enum"]:
        assert f"`{name}`" in text, name

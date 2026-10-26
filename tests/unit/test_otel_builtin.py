"""EV-09 built-in OpenTelemetry: the engine emits engine.install / driver.apply / driver.remove / engine.health-gate /
engine.rollback itself (no monkeypatching), the desired state's traceparent is persisted per manifestVersion and every
status report carries traceparent + correlationId; without the OTel packages everything still works."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap

import pytest

from fe_unit_fakes import FakeDriver, deployment, desired, make_engine
from flexengine2.identity import crypto
from flexengine2.obs import logging as flog
from flexengine2.obs import tracing

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
TP = f"00-{TRACE}-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.delenv("FLEX_OTEL", raising=False)
    monkeypatch.delenv("FLEX_OTEL_FILE", raising=False)
    flog.configure(stream=io.StringIO())
    tracing.reset()
    yield
    tracing.reset()
    flog.reset()


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    assert tracing.configure({"enabled": True}, exporters=[exp])
    return exp


def signed_engine(tmp_path, drv=None, **kw):
    key = crypto.generate()
    e = make_engine(tmp_path, drivers={"native": drv or FakeDriver()}, cloudPublicKey=crypto.public_b64(key),
                    healthEveryTicks=100, **kw)

    def push(mv, deps, tp=TP):
        body = json.dumps(desired(e.device, mv, deps)).encode()
        assert e.accept_manifest(body, crypto.sign(key, body), f'"{mv}"', traceparent=tp)
    return e, push


def by_name(spans):
    return {s.name: s for s in spans}


def test_install_spans_nest_under_the_plan_trace(tmp_path, exporter):
    e, push = signed_engine(tmp_path)
    push(1, [deployment("d1")])
    assert e.state["traceparents"] == {"1": TP}
    e.tick()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert {"engine.install d1", "driver.apply", "engine.health-gate"} <= set(names)
    s = by_name(spans)
    inst = s["engine.install d1"]
    assert f"{inst.context.trace_id:032x}" == TRACE and f"{inst.parent.span_id:016x}" == "00f067aa0ba902b7"
    assert s["driver.apply"].parent.span_id == inst.context.span_id
    assert s["engine.health-gate"].parent.span_id == inst.context.span_id
    assert inst.attributes["flex.deployment"] == "d1" and inst.attributes["flex.version"] == "1.0.0"
    assert inst.attributes["flex.manifest_version"] == 1 and inst.attributes["flex.device"] == "dev-a"
    assert s["engine.health-gate"].attributes["flex.healthy"] is True
    assert s["driver.apply"].attributes["flex.driver"] == "native"
    # every status report carries the trace context; inside the install span it is that span
    reps = e.state["outbox"]
    assert [r["state"] for r in reps] == ["Installing", "Installed"]
    for r in reps:
        assert r["correlationId"] == TRACE
        assert r["traceparent"].startswith(f"00-{TRACE}-") and r["traceparent"] != TP
    assert reps[0]["traceparent"].split("-")[2] == f"{inst.context.span_id:016x}"


def test_rollback_and_remove_spans(tmp_path, exporter):
    drv = FakeDriver()
    e, push = signed_engine(tmp_path, drv)
    push(1, [deployment("d1")])
    e.tick()
    drv.healthy = False
    push(2, [deployment("d1", "2.0.0")])
    e.tick()
    s = exporter.get_finished_spans()
    rb = [x for x in s if x.name == "engine.rollback"]
    assert len(rb) == 1 and rb[0].attributes["flex.failed_version"] == "2.0.0"
    inner = [x for x in s if x.parent is not None and x.parent.span_id == rb[0].context.span_id]
    assert {x.name for x in inner} == {"driver.apply", "engine.health-gate"}
    assert e.state["outbox"][-1]["state"] == "Failed" and e.state["outbox"][-1]["correlationId"] == TRACE
    exporter.clear()
    drv.healthy = True
    push(3, [], tp="00-" + "ab" * 16 + "-" + "cd" * 8 + "-01")
    e.tick()
    rm = [x for x in exporter.get_finished_spans() if x.name == "driver.remove"]
    assert len(rm) == 1 and f"{rm[0].context.trace_id:032x}" == "ab" * 16
    assert e.state["outbox"][-1]["state"] == "Removed" and e.state["outbox"][-1]["correlationId"] == "ab" * 16


def test_traceparent_is_persisted_and_survives_a_restart(tmp_path, exporter):
    e, push = signed_engine(tmp_path)
    push(1, [deployment("d1")])
    del e
    from flexengine2.core.reconciler import Engine

    cfg = json.loads(json.dumps({"deviceId": "dev-a", "apiUrl": "http://127.0.0.1:9", "dataDir": str(tmp_path / "dev-a"),
                                 "onboardingToken": "t", "cloudPublicKey": crypto.public_b64(crypto.generate()),
                                 "releasePublicKeys": []}))
    e2 = Engine(cfg, {"native": FakeDriver()})
    assert e2._mv_traceparent() == TP
    e2.report("d1", "Pending", reason="x")
    assert e2.state["outbox"][-1]["traceparent"] == TP and e2.state["outbox"][-1]["correlationId"] == TRACE


def test_without_otel_enabled_no_spans_but_correlation_still_flows(tmp_path):
    exp = InMemorySpanExporter()
    assert not tracing.configure({"enabled": False}, exporters=[exp])
    e, push = signed_engine(tmp_path)
    assert not e.tracer.active
    push(1, [deployment("d1")])
    e.tick()
    assert exp.get_finished_spans() == ()
    assert all(r["traceparent"] == TP and r["correlationId"] == TRACE for r in e.state["outbox"])


def test_no_traceparent_from_the_cloud_means_no_fields(tmp_path):
    e, push = signed_engine(tmp_path)
    push(1, [deployment("d1")], tp=None)
    e.tick()
    assert all("traceparent" not in r and "correlationId" not in r for r in e.state["outbox"])


def test_env_vars_still_enable_and_set_the_file(tmp_path, monkeypatch):
    f = tmp_path / "spans.jsonl"
    monkeypatch.setenv("FLEX_OTEL", "1")
    monkeypatch.setenv("FLEX_OTEL_FILE", str(f))
    assert tracing.settings(None) == {"enabled": True, "endpoint": None, "file": str(f)}
    assert tracing.configure(None)
    e, push = signed_engine(tmp_path)
    push(1, [deployment("d1")])
    e.tick()
    tracing.flush()
    names = {json.loads(l)["name"] for l in f.read_text().splitlines()}
    assert {"engine.install d1", "driver.apply", "engine.health-gate"} <= names


def test_config_endpoint_settings():
    assert tracing.settings({"enabled": True, "endpoint": "http://c:4318", "file": "/x"}) == {
        "enabled": True, "endpoint": "http://c:4318", "file": "/x"}


def test_legacy_hook_uses_the_built_in_spans(tmp_path, monkeypatch):
    from flexengine2.host import _otel_hook

    e = make_engine(tmp_path)
    orig = e.install
    monkeypatch.setenv("FLEX_OTEL", "1")
    assert _otel_hook([e]) is not None
    assert e.install == orig and e.tracer.active          # no monkeypatching of a flexengine2 Engine


def test_everything_works_without_the_otel_packages(tmp_path):
    code = textwrap.dedent(f"""
        import io, json, sys
        for m in [k for k in sys.modules if k.startswith("opentelemetry")]:
            del sys.modules[m]
        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "opentelemetry" or name.startswith("opentelemetry."):
                    raise ImportError("blocked " + name)
        sys.meta_path.insert(0, Block())
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).parent)!r})
        from fe_unit_fakes import deployment, make_engine, set_desired
        from flexengine2.obs import logging as flog, tracing
        flog.configure(stream=io.StringIO())
        assert tracing.configure({{"enabled": True}}) is False
        e = make_engine(__import__('pathlib').Path({str(tmp_path)!r}))
        set_desired(e, 1, [deployment("d1")])
        e.state["traceparents"] = {{"1": {TP!r}}}
        e.tick()
        assert e.state["applied"]["d1"], e.state
        assert all(r["correlationId"] == {TRACE!r} for r in e.state["outbox"])
        assert not any(m.startswith("opentelemetry") for m in sys.modules)
        print("ok")
    """)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and p.stdout.strip().endswith("ok"), p.stdout + p.stderr
    assert "OpenTelemetry requested but not installed" in p.stderr

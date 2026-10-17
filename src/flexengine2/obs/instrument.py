"""OTel spans for a device engine (IG-13, moved from demo/obs/flexobs/engine_wrapper.py; built in from EV-09).

    FLEX_OTEL=1 FLEX_OTEL_FILE=DATA/obs/spans-engine.jsonl OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318 \
        python -m flexobs.engine_wrapper --config engine.json [--engine-module flexp2.engine.engine]

What it adds (monkeypatching the Engine instance's methods at start-up):
  * the `traceparent` header of a 200 desired-state response is remembered per manifestVersion
    (persisted next to the engine state, so a crash-resume continues the same trace);
  * `engine.install <dep>` spans (child of that context), with `driver.apply`, `driver.remove` and
    `engine.health-gate` children;
  * every status report carries `traceparent` (the current span) and `correlationId`, so the backend's
    `device.status` spans nest under the driver spans.

Works with any engine class that keeps the P2 shape (`client.request`, `install`, `health_gate`,
`report`, `drivers`). `--engine-module` selects another module with an `Engine` class (e.g. the demo
edge engine, IG-05) — see demo/obs/patches/README.md for the hook contract.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from opentelemetry import trace as api
from opentelemetry.trace import SpanKind

from flexengine2.obs import otel



def instrument_engine(eng) -> None:
    tr = otel.tracer(f"flex-engine-{eng.device}")
    ctx_path = Path(eng.dir) / "otel-context.json"
    ctxs: dict = json.loads(ctx_path.read_text()) if ctx_path.exists() else {}
    cur = {"span": None}

    def mv_ctx():
        mv = (eng.state.get("desired") or {}).get("manifestVersion")
        return otel.context_from_traceparent(ctxs.get(str(mv))), mv

    orig_request = eng.client.request

    def request(method, path, body=b"", headers=None, signed=True):
        status, hdrs, data = orig_request(method, path, body, headers, signed)
        if method == "GET" and path.endswith("/deployments") and status == 200 and hdrs.get("traceparent"):
            try:
                mv = json.loads(data).get("manifestVersion")
                ctxs[str(mv)] = hdrs["traceparent"]
                ctx_path.write_text(json.dumps(ctxs))
            except ValueError:
                pass
        return status, hdrs, data
    eng.client.request = request

    orig_install, orig_gate, orig_report = eng.install, eng.health_gate, eng.report

    def install(dep_id, d, rel):
        parent, mv = mv_ctx()
        rv = d["spec"]["deploymentProfile"]["components"][0]["properties"]
        with tr.start_as_current_span(f"engine.install {dep_id}", context=parent, kind=SpanKind.INTERNAL,
                                      attributes={"flex.device": eng.device, "flex.deployment": dep_id,
                                                  "flex.version": rv["revision"], "flex.driver": d["x-flex-driver"],
                                                  "flex.manifest_version": mv}) as sp:
            cur["span"] = sp
            try:
                return orig_install(dep_id, d, rel)
            finally:
                cur["span"] = None

    def health_gate(drv, dep_id, probe, **kw):
        with tr.start_as_current_span("engine.health-gate", attributes={"flex.deployment": dep_id}) as sp:
            ok, dt = orig_gate(drv, dep_id, probe, **kw)
            sp.set_attribute("flex.healthy", ok)
            sp.set_attribute("flex.gate_failure", str(getattr(eng, "gate_failure", "") or ""))
            return ok, dt

    def report(dep, state, **kw):
        sp = api.get_current_span()
        tp = otel.traceparent_of(sp) if sp.get_span_context().is_valid else None
        if not tp:
            parent, _ = mv_ctx()
            tp = otel.traceparent_of(parent) if parent else None
        if tp:
            kw.setdefault("traceparent", tp)
            kw.setdefault("correlationId", tp.split("-")[1])
        return orig_report(dep, state, **kw)

    def wrap_driver(name, drv):
        for meth in ("apply", "remove"):
            orig = getattr(drv, meth, None)
            if orig is None:
                continue

            def make(orig=orig, meth=meth):
                def f(*a, **kw):
                    parent = None if api.get_current_span().get_span_context().is_valid else mv_ctx()[0]
                    with tr.start_as_current_span(f"driver.{meth}", context=parent, kind=SpanKind.CLIENT,
                                                  attributes={"flex.driver": name, "flex.profile":
                                                              getattr(drv, "profile", name)}) as sp:
                        t0 = time.time()
                        try:
                            return orig(*a, **kw)
                        finally:
                            sp.set_attribute("flex.seconds", round(time.time() - t0, 3))
                return f
            setattr(drv, meth, make())

    for name, drv in eng.drivers.items():
        wrap_driver(name, drv)
    eng.install, eng.health_gate, eng.report = install, health_gate, report

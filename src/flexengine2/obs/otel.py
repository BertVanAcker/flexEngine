"""OpenTelemetry set-up shared by the stack, the engine wrapper and the tests.

Enabled by `FLEX_OTEL=1` (or `setup(enabled=True)` from the stack's `--otel`). Off, every helper is a
no-op and nothing is imported beyond opentelemetry-api, so P2/P6 behaviour is unchanged.

Exporters (both optional, both can be on):
* OTLP/HTTP to the collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (compose: otel-collector:4318,
  which forwards to Jaeger);
* a JSONL file (`FLEX_OTEL_FILE`, or the directory given to `setup`), always written by the stack. The
  run report (flexobs.report) reads it, so traces count even when no collector is running.

Correlation id. A plan's trace id and root span id are derived from its plan hash:
trace_id = planHash[7:39], root span id = planHash[39:55]. Every component that knows the plan hash
(gateway, backend, console) or the manifest version it produced (engine, status reports) can attach
its spans to the same trace without a shared store; `traceparent` headers carry it to the devices.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import random
import threading
import time
from pathlib import Path

from opentelemetry import trace as api
from opentelemetry.trace import (Link, NonRecordingSpan, SpanContext, SpanKind, TraceFlags,
                                 set_span_in_context)

_lock = threading.Lock()
_providers: dict[str, object] = {}
_cfg = {"enabled": None, "file": None}
_preset: contextvars.ContextVar = contextvars.ContextVar("flex_preset_ids", default=None)


def enabled() -> bool:
    if _cfg["enabled"] is None:
        _cfg["enabled"] = os.environ.get("FLEX_OTEL") == "1"
        if _cfg["enabled"] and not _cfg["file"] and os.environ.get("FLEX_OTEL_FILE"):
            _cfg["file"] = os.environ["FLEX_OTEL_FILE"]
    return bool(_cfg["enabled"])


def setup(enabled: bool = True, file: str | Path | None = None):
    """Called once by a process that wants spans (the stack with --otel, the engine wrapper)."""
    _cfg["enabled"] = enabled
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        _cfg["file"] = str(file)
    elif os.environ.get("FLEX_OTEL_FILE"):
        _cfg["file"] = os.environ["FLEX_OTEL_FILE"]


# ---- ids ---------------------------------------------------------------------------------------

def plan_ids(plan_hash: str) -> tuple[int, int]:
    h = plan_hash.split(":", 1)[-1]
    return int(h[:32], 16) or 1, int(h[32:48], 16) or 1


def correlation_id(plan_hash: str) -> str:
    return plan_hash.split(":", 1)[-1][:32]


def plan_context(plan_hash: str):
    """Parent context for spans that belong to a plan's trace (root = the `flex.plan` span)."""
    t, s = plan_ids(plan_hash)
    sc = SpanContext(trace_id=t, span_id=s, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return set_span_in_context(NonRecordingSpan(sc))


def traceparent_of(ctx_or_span=None) -> str | None:
    span = ctx_or_span if isinstance(ctx_or_span, api.Span) else api.get_current_span(ctx_or_span)
    sc = span.get_span_context()
    if not sc.is_valid:
        return None
    return f"00-{sc.trace_id:032x}-{sc.span_id:016x}-{int(sc.trace_flags):02x}"


def context_from_traceparent(tp: str | None):
    if not tp:
        return None
    try:
        ver, t, s, f = tp.strip().split("-")
        sc = SpanContext(trace_id=int(t, 16), span_id=int(s, 16), is_remote=True, trace_flags=TraceFlags(int(f, 16)))
    except (ValueError, AttributeError):
        return None
    return set_span_in_context(NonRecordingSpan(sc)) if sc.is_valid else None


def link_to(ctx) -> list:
    if ctx is None:
        return []
    sc = api.get_current_span(ctx).get_span_context()
    return [Link(sc)] if sc.is_valid else []


# ---- providers -----------------------------------------------------------------------------------

def _sdk_provider(service: str):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.id_generator import IdGenerator

    class PresetIdGenerator(IdGenerator):
        """Random ids, except inside `preset_ids(...)`: lets the `flex.plan` root span carry the ids derived
        from the plan hash, so every component's spans for that plan land in one trace."""

        def generate_span_id(self) -> int:
            p = _preset.get()
            if p and p[1]:
                _preset.set((p[0], 0))
                return p[1]
            return random.getrandbits(64) or 1

        def generate_trace_id(self) -> int:
            p = _preset.get()
            return p[0] if p else (random.getrandbits(128) or 1)

    prov = TracerProvider(resource=Resource.create({"service.name": service, "flex.demo": "d1"}),
                          id_generator=PresetIdGenerator())
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        prov.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=500))
    if _cfg["file"]:
        prov.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(_cfg["file"], service)))
    return prov


def tracer(service: str):
    """A tracer for one logical service (gateway, backend, console, engine-<dev>). No-op when disabled."""
    if not enabled():
        return api.NoOpTracer()
    with _lock:
        prov = _providers.get(service)
        if prov is None:
            prov = _providers[service] = _sdk_provider(service)
    return prov.get_tracer("flexobs")


def flush():
    for p in list(_providers.values()):
        try:
            p.force_flush(5000)
        except Exception:  # noqa: BLE001
            pass


def shutdown():
    for p in list(_providers.values()):
        try:
            p.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _providers.clear()


@contextlib.contextmanager
def preset_ids(trace_id: int, span_id: int):
    tok = _preset.set((trace_id, span_id))
    try:
        yield
    finally:
        _preset.reset(tok)


def emit_plan_root(tr, plan_hash: str, attrs: dict, start: float | None = None):
    """The `flex.plan` root span of a plan's trace (emitted once, by the backend that computed the hash)."""
    t, s = plan_ids(plan_hash)
    with preset_ids(t, s):
        sp = tr.start_span("flex.plan", kind=SpanKind.INTERNAL, context=api.set_span_in_context(api.INVALID_SPAN),
                           attributes={"flex.plan_hash": plan_hash, "flex.correlation_id": correlation_id(plan_hash),
                                       **{k: v for k, v in attrs.items() if v is not None}},
                           start_time=int((start or time.time()) * 1e9))
    sp.end()
    return sp


def span_at(tr, name: str, ctx, start: float, end: float, attrs: dict, kind=SpanKind.INTERNAL, links=None):
    """A span with explicit wall-clock times (device reports carry their own timestamps)."""
    sp = tr.start_span(name, context=ctx, kind=kind, attributes={k: v for k, v in attrs.items() if v is not None},
                       start_time=int(start * 1e9), links=links or [])
    sp.end(end_time=int(max(end, start) * 1e9))
    return sp


# ---- file exporter ---------------------------------------------------------------------------------

class JsonlSpanExporter:
    """Minimal SpanExporter: one JSON object per finished span (read by flexobs.report)."""

    def __init__(self, path: str, service: str):
        self.path, self.service = Path(path), service
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        lines = []
        for s in spans:
            ctx, par = s.context, s.parent
            lines.append(json.dumps({
                "service": self.service, "name": s.name, "traceId": f"{ctx.trace_id:032x}",
                "spanId": f"{ctx.span_id:016x}", "parentSpanId": f"{par.span_id:016x}" if par else None,
                "start": s.start_time / 1e9, "end": (s.end_time or s.start_time) / 1e9,
                "kind": s.kind.name, "status": s.status.status_code.name,
                "attributes": {k: (list(v) if isinstance(v, tuple) else v) for k, v in (s.attributes or {}).items()},
                "links": [f"{l.context.trace_id:032x}" for l in (s.links or [])]}, default=str))
        with self.lock, open(self.path, "a") as f:
            f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000):
        return True


def read_spans(paths) -> list[dict]:
    out = []
    for p in paths:
        p = Path(p)
        if p.exists():
            out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return out

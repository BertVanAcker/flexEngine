"""Built-in OpenTelemetry spans of a device engine (EV-09; replaces the IG-13 monkeypatching wrapper).

    tracing.configure(cfg.get("otel"))           # process-wide; FLEX_OTEL=1 / FLEX_OTEL_FILE / OTEL_EXPORTER_OTLP_*
    t = EngineTracer("panda")                    # still work; returns whether spans are recorded
    with t.span("engine.install d1", parent_traceparent=tp, **{"flex.deployment": "d1"}) as sp: ...
    t.current_traceparent()                      # traceparent of the active span, or None

Span names (one trace per plan; the parent is the `traceparent` header of the desired-state response that carried
the manifest version, persisted in state.json `traceparents`): `engine.install <dep>` with children
`driver.apply`, `engine.health-gate`, `engine.rollback` (containing its own `driver.apply` + gate) and
`driver.remove`; a removal is `driver.remove` directly under the plan context.

Without the OpenTelemetry packages (optional extra `otel`), or when neither FLEX_OTEL=1 nor `otel.enabled` is set,
every span is a no-op (`span()` yields None) and nothing from opentelemetry is imported.
"""

from __future__ import annotations

import contextlib
import os
import sys

_state = {"enabled": False, "warned": False}


def settings(cfg_otel: dict | None) -> dict:
    """Effective settings from cfg `otel` + environment (env FLEX_OTEL=1 also enables)."""
    c = dict(cfg_otel or {})
    enabled = bool(c.get("enabled")) or os.environ.get("FLEX_OTEL") == "1"
    return {"enabled": enabled, "endpoint": c.get("endpoint") or None,
            "file": c.get("file") or os.environ.get("FLEX_OTEL_FILE") or None}


def configure(cfg_otel: dict | None = None, exporters=None) -> bool:
    """Enable spans for this process when asked; returns True when spans are recorded."""
    s = settings(cfg_otel)
    if not s["enabled"]:
        _state["enabled"] = False
        return False
    try:
        from flexengine2.obs import otel
    except ImportError as ex:
        if not _state["warned"]:
            _state["warned"] = True
            print(f"flexengine: OpenTelemetry requested but not installed ({ex}); running without spans",
                  file=sys.stderr)
        _state["enabled"] = False
        return False
    otel.setup(True, file=s["file"], endpoint=s["endpoint"], exporters=exporters)
    _state["enabled"] = True
    return True


def enabled() -> bool:
    return _state["enabled"]


def flush() -> None:
    if _state["enabled"]:
        from flexengine2.obs import otel

        otel.flush()


def reset() -> None:
    """Tests: disable and drop the providers."""
    if _state["enabled"]:
        from flexengine2.obs import otel

        otel.reset()
    _state["enabled"] = False


class EngineTracer:
    def __init__(self, device: str):
        self.device = device
        self._tr = None
        self._otel = None
        if _state["enabled"]:
            from flexengine2.obs import otel

            self._otel = otel
            self._tr = otel.tracer(f"flex-engine-{device}")

    @property
    def active(self) -> bool:
        return self._tr is not None

    @contextlib.contextmanager
    def span(self, name: str, parent_traceparent: str | None = None, client: bool = False, **attrs):
        """A span under the active one; at the top of a stack (no active span) under `parent_traceparent`."""
        if self._tr is None:
            yield None
            return
        from opentelemetry import trace as api
        from opentelemetry.trace import SpanKind

        ctx = None
        if not api.get_current_span().get_span_context().is_valid:
            ctx = self._otel.context_from_traceparent(parent_traceparent)
        with self._tr.start_as_current_span(name, context=ctx, kind=SpanKind.CLIENT if client else SpanKind.INTERNAL,
                                            attributes={"flex.device": self.device,
                                                        **{k: v for k, v in attrs.items() if v is not None}}) as sp:
            yield sp

    def current_traceparent(self) -> str | None:
        if self._tr is None:
            return None
        from opentelemetry import trace as api

        sp = api.get_current_span()
        return self._otel.traceparent_of(sp) if sp.get_span_context().is_valid else None


def set_attrs(sp, **attrs) -> None:
    if sp is not None:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)

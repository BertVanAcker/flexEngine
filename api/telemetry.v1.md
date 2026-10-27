# CP-8 Telemetry, v1

What the engine emits for observability (`obs/logging.py`, `obs/tracing.py`, `obs/otel.py`, `core/reconciler.py`), so
the collector, Jaeger, `flexobs.report` and a control plane can rely on it. **Machine-readable spec:
`src/flexengine2/api/telemetry.v1.schema.json`** (`contracts.validate("telemetry#LogRecord", rec)`; `$defs`
`LogRecord`, `LogEvent`, `ActionRecord`, `SpanRecord`, `SpanName`, `SpanAttributes`, `Metrics`). Where this page and
the schema differ, the schema wins. Additive only within v1: a new span, span attribute, log event, action kind or
metric key; renaming or removing one, or changing its type, is v2.

[V] = checked by `tests/api/test_telemetry_contract.py` (an engine run's log lines, action log, span file and metrics
validate; every span name, log event and action the sources can emit is registered in the schema) or by
`tests/unit/test_otel_builtin.py`.

## 1. Enabling

`FLEX_OTEL=1` or `otel.enabled: true` in `engine.yaml` instruments every device engine of the host process; without
it, or without the OTel packages (`pip install flexEngine[otel]`), nothing changes and nothing from opentelemetry is
imported. Exporters: OTLP/HTTP when `otel.endpoint` or `OTEL_EXPORTER_OTLP_ENDPOINT` (or `…_TRACES_ENDPOINT`) is
set; a JSON Lines file when `otel.file` or `FLEX_OTEL_FILE` is set [V]. Resource: `service.name =
flex-engine-<deviceId>` (one tracer per device) [V].

## 2. Spans

| Span | Kind | Parent | Attributes |
|---|---|---|---|
| `engine.install <dep>` | INTERNAL | the `traceparent` of the desired-state answer that carried the current `manifestVersion` (none → new trace) | `flex.device`, `flex.deployment`, `flex.version`, `flex.driver`, `flex.manifest_version` |
| `engine.start <dep>` | INTERNAL | as install (FE-4: start of a staged or stopped deployment) | `flex.device`, `flex.deployment`, `flex.version`, `flex.driver` |
| `engine.health-gate` | INTERNAL | current span | `flex.deployment`, `flex.rollback` (bool), `flex.healthy` (bool), `flex.gate_failure` ("" when healthy), `flex.seconds` |
| `engine.rollback` | INTERNAL | current span (install) | `flex.deployment`, `flex.version` (rolled back to), `flex.failed_version` |
| `engine.build` | INTERNAL | manifest-version context (FE-4) | `flex.deployment`, `flex.build` |
| `driver.apply`, `driver.remove`, `driver.stage`, `driver.start`, `driver.stop` | CLIENT | current span; outside an install (removal, drift repair) the manifest-version context | `flex.driver` (profile key), `flex.profile`, `flex.deployment`, `flex.seconds` |

Every engine span also carries `flex.device`. The attribute set is closed (`SpanAttributes`) [V]. Backend-side spans
of the same trace (not the engine's): `flex.plan` root (`flex.plan_hash`, `flex.correlation_id`), `device.status`.

## 3. Correlation

- **Trace id from the plan**: `trace_id = planHash hex[0:32]`, root span id = `hex[32:48]`; correlation id =
  `planHash hex[0:32]` (`otel.correlation_id`). Every component that knows the plan hash attaches to the same trace
  without a shared store [V].
- **Propagation to the device**: W3C `traceparent` (`00-<32 hex>-<16 hex>-<2 hex>`) on the desired-state response;
  the engine keeps it per manifestVersion in `state.json` (`traceparents`), so a crash-resume continues the trace
  [V]. The reference fake (`testing/fakecloud.py`) does not send it by itself.
- **Into status reports**: every CP-2 record gets `traceparent` (current span, else the manifest-version context)
  and `correlationId` (= its trace id) when instrumentation is on (fields of `status.v1alpha1`) [V].

## 4. Logs and action log

- **Engine log** (`LogRecord`): one line per record on stdout (journald adds unit and time). `logFormat: json` (the
  default) writes `{"ts", "level", "device", "event", "dep"?, "version"?, "msg", ...event-specific keys}`;
  `logFormat: text` writes `<ISO-8601 UTC ms>Z [<deviceId>] <message>` (warnings and errors prefixed) for people,
  with no contract beyond that. Event names: `LogEvent`, plus `action.<kind>` per action-log line, `build.<phase>`
  (FE-4: started, progress, log, succeeded, failed) and, for stdlib records of the package, the logger name
  (`flexengine2.cloud.client`) [V].
- **Action log** `dataDir/actions.jsonl` (`ActionRecord`, fsync per line; read by the SIL scenario and the EV-13
  oracles) [V]: `{"t": unix s, "action": apply|adopt|commit|reject|remove|rollback|rollback-resume|repair|retry|stage|
  start|stop|build, "dep": id, "version"?, "digest"?, "reason"? (reject), "failed"? (rollback: failed version),
  "why"? (remove after a failed first install), "build"? (build id)}`.

## 5. JSONL span file (`FLEX_OTEL_FILE`)

One object per finished span (`SpanRecord`): `service, name, traceId (32 hex), spanId (16 hex), parentSpanId|null,
start, end (unix s), kind, status, attributes, links[] (trace ids)` [V].

## 6. Metrics

`Engine.metrics()` per device, served as `GET /v1/metrics` (`{deviceId: Metrics}`, CP-7) [V]: `ticks`, `startedAt`,
`lastTickAt`, the duration summaries `reconcileSeconds`, `tickSeconds`, `gateSeconds` (`{last, max, avg, count}`),
`outboxLength`, `outboxMax`, `outboxDropped`, `backpressure`, `errors` and `errorsByType` (counters), `lastError`,
`manifestVersion`, `applied`. Status records carry the timings of one install (`applySeconds`,
`healthGateSeconds`, `detectSeconds`, `totalSeconds`, `faultToHealthySeconds`, CP-2). No OTel metrics are exported in
v1; adding them (e.g. `flexengine_reconcile_seconds`) is an additive change.

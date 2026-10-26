# CP-8 Telemetry, v1

What the engine emits for observability today (`obs/instrument.py`, `obs/otel.py`, `core/reconciler.py`), so the
collector, Jaeger and `flexobs.report` can rely on it. Additive only within v1. [V] = from the code (exercised by the
demo/obs tests and SIL M8, not by `tests/api`); [I] = planned (EV-09).

## 1. Enabling

`FLEX_OTEL=1` instruments every device engine of the host process (`host._otel_hook`); without it, or without the
OTel packages (`pip install flexengine-v2[otel]`), nothing changes. Exporters: OTLP/HTTP when
`OTEL_EXPORTER_OTLP_ENDPOINT` (or `…_TRACES_ENDPOINT`) is set; a JSON Lines file when `FLEX_OTEL_FILE` is set [V].
Resource: `service.name = flex-engine-<deviceId>` (one tracer per device), `flex.demo = d1` [V].

## 2. Spans

| Span | Kind | Parent | Attributes |
|---|---|---|---|
| `engine.install <dep>` | INTERNAL | context of the `traceparent` header of the 200 desired-state answer for the current `manifestVersion` (none → new trace) | `flex.device`, `flex.deployment`, `flex.version`, `flex.driver`, `flex.manifest_version` |
| `engine.health-gate` | INTERNAL | current span (install) | `flex.deployment`, `flex.healthy` (bool), `flex.gate_failure` (string, "" when healthy) |
| `driver.apply`, `driver.remove` | CLIENT | current span; outside an install (removal, drift repair) the manifest-version context | `flex.driver` (profile key), `flex.profile`, `flex.seconds` |

Backend-side spans of the same trace (not the engine's): `flex.plan` root (`flex.plan_hash`, `flex.correlation_id`),
`device.status` (backend ingest) [V, demo/obs].

## 3. Correlation

- **Trace id from the plan**: `trace_id = planHash hex[0:32]`, root span id = `hex[32:48]`; correlation id =
  `planHash hex[0:32]` (`otel.correlation_id`). Every component that knows the plan hash attaches to the same trace
  without a shared store [V].
- **Propagation to the device**: W3C `traceparent` (`00-<32 hex>-<16 hex>-<2 hex>`) on the desired-state response;
  the engine stores it per manifestVersion in `dataDir/otel-context.json` (`{"<mv>": "<traceparent>"}`), so a
  crash-resume continues the trace [V]. The reference fake (`testing/fakecloud.py`) does not send it by itself;
  the demo/obs backend instrumentation (`demo/obs/flexobs/instrument.py`) adds it for the plan that produced the
  manifest version [V, demo/obs tests].
- **Into status reports**: every CP-2 record gets `traceparent` (current span, else the manifest-version context)
  and `correlationId` (= its trace id) when instrumentation is on (schema fields of `status.v1alpha1`) [V].

## 4. Logs and action log

- Engine log: one line per event on stdout, `<ISO-8601 UTC ms>Z [<deviceId>] <message>` (plain text; journald adds
  unit/time). Structured JSON logging with fields `ts, level, device, deployment, event, reasonCode, traceId` is
  EV-05/EV-09 [I].
- Action log `dataDir/actions.jsonl` (fsync per line; read by the SIL scenario and EV-13 oracles) [V]:
  `{"t": unix s, "action": apply|adopt|commit|reject|remove|rollback|repair, "dep": id, "version"?, "digest"?,
  "reason"? (reject), "failed"? (rollback: failed version), "why"? (remove after a failed first install)}`.

## 5. JSONL span file (`FLEX_OTEL_FILE`)

One object per finished span: `service, name, traceId (32 hex), spanId (16 hex), parentSpanId|null, start, end (unix
s), kind, status, attributes, links[] (trace ids)` [V].

## 6. Metrics

None exported today. Status records carry timings (`applySeconds`, `healthGateSeconds`, `detectSeconds`,
`totalSeconds`, `faultToHealthySeconds`). Planned (EV-09) [I]: `flexengine_reconcile_seconds`,
`flexengine_outbox_length`, `flexengine_gate_seconds{result}`, `flexengine_downgrades_refused_total`.

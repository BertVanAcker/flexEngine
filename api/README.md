# flexEngineV2 connection-point contracts (EV-00)

Specs for the engine's edges (docs/flexengine-v2-plan.md §5.0). Each connection point (CP) has a versioned spec, golden
examples, a contract test the engine passes (`flexengine/tests/api/`) and a reference fake of the other side.
Evidence marks: **[V]** checked by a test in `tests/api/`; **[I]** inferred or proposed, not checked.

## Where the files are

Machine-readable specs are **package data** in [`../src/flexengine2/api/`](../src/flexengine2/api/), so they ship in
the wheel and `flexengine2.contracts` finds them with `importlib.resources` (no repo checkout needed on a device or in
the Rust lite agent's CI). This directory holds the prose specs, the test vectors and their generators. Chosen over
"canonical files here + path fallback" because a fallback silently breaks in a wheel install; the OpenAPI files sit
next to the schemas they `$ref`, so relative refs work in both places.

```python
from flexengine2 import contracts
contracts.validate("desired-state", doc)                # ContractError lists every violation
contracts.validate("oci-profile#ReleaseManifest", man)  # a $defs entry
contracts.reason_code("health check failed")            # -> {"code": "FE-REC-004", ...}
```

## Index

| CP | Connection point | Spec | Reference fake / producer | Contract tests |
|---|---|---|---|---|
| CP-1 | Desired-state model | `desired-state.v1alpha1.schema.json`, `descriptor.v1alpha1.schema.json` (release descriptor: health, restart, runOptions, `x-flex-mcu`) | `testing/fakecloud.py` (serves), `flex release` (descriptors) | `test_schemas.py`, `test_engine_contracts.py` [V] |
| CP-2 | Status, capabilities, onboarding | `status.v1alpha1.schema.json`, `device-event.v1alpha1.schema.json`, `capabilities.v1alpha1.schema.json`, `onboarding.v1alpha1.schema.json`, `reason-codes.v1alpha1.json` | fakecloud (ingest) | `test_engine_contracts.py`, `test_reason_codes.py`, `test_refusals.py` [V] |
| CP-3 | Artefact access (OCI subset), offline bundle | `oci-profile.v1alpha1.schema.json`, [bundle.v1alpha1.md](bundle.v1alpha1.md), `vectors/release`, `vectors/bundle` | fakecloud `/v2/…`, `/bundle` | `test_vectors.py` [V] |
| CP-4 | Trust | [trust.v1alpha1.md](trust.v1alpha1.md), `trust.v1alpha1.schema.json`, `vectors/` | fakecloud signer, `testing/release.py` | `test_vectors.py` [V] |
| CP-5 | Driver SPI v1 | [cp5-driver-spi.md](cp5-driver-spi.md) (frozen `drivers/base.py`) | fake executors | `tests/contract/` (EV-03, other package) |
| CP-6 | Child-device interfaces | [cp6-children.md](cp6-children.md) | QEMU, fake CLI, pyads/pymodbus/shm stand-ins | driver suites (EV-03) |
| CP-7 | Local API + CLI | `local-api.v1.openapi.yaml`, `cli-status.v1.schema.json` (spec only; EV-07 implements) | – | spec validity only [V] |
| CP-8 | Telemetry | [telemetry.v1.md](telemetry.v1.md) | collector / JSONL exporter | – (EV-09) |
| all | Southbound OpenAPI | `openapi-southbound.yaml` (P2 0.1.0-p2 → `v1alpha1`, `$ref`s the schemas) | fakecloud | route ⊆ spec, P2 paths kept [V] |

## Versioning and compatibility

- Version in the file name and `$id` (`…v1alpha1.schema.json`); the wire carries `apiVersion` where the document has one
  (`flex.dev/v1alpha1`, Margo's `application.margo.org/v1alpha1`, `device.margo.org/v1alpha1`).
- **Within a version only additive changes**: a new optional field, a new enum value (e.g. driver profile `os-rauc`
  when it ships), a newly registered `x-flex-*` key, a new reason code. Removing or retyping a field, tightening a
  pattern, or making a field required needs the next version (`v1alpha2`); the engine then accepts both for one
  release.
- **Unknown keys.** Flex-owned objects (DesiredState, `x-flex-activation`, the `x-flex` capabilities block, driver
  fragments, status records, health/restart/runOptions) are closed. Margo-owned objects accept unknown Margo keys
  (ignored) but reject every unregistered `x-` key. Registered: `x-flex-driver`, `x-flex-safetyClass`,
  `x-flex-activation` (ApplicationDeployment), `x-flex` (DeviceCapabilities), `x-flex-mcu` (release descriptor).
- Rules JSON Schema cannot express are checked by `contracts.validate` (desired state: unique deployment ids,
  `metadata.deviceId` = `deviceId`) [V].
- Since 2026-10-17 the engine validates every desired state against CP-1 before acceptance (see the decisions
  section at the end).
- Reason codes are stable: never reused or renumbered; since 2026-10-17 the engine sends the code as `reasonCode`
  next to every reported reason.

## CP-1 fields (DesiredState → ApplicationDeployment)

| Field | Type | Req. | Read by |
|---|---|---|---|
| `apiVersion` / `kind` | `flex.dev/v1alpha1` / `DesiredState` | ✔ | – |
| `deviceId` | id `^[A-Za-z0-9_-]+(\.[…])*$`; child = `<gw>.<child>` | ✔ | reconciler (`accept_manifest`: other device ignored) |
| `manifestVersion` | int ≥ 0, strictly increasing | ✔ | reconciler (downgrade refused) |
| `deployments[]` | ApplicationDeployment; absence = removal | ✔ | reconciler |
| `.apiVersion` / `.kind` | Margo consts | ✔ | – |
| `.metadata.id` | id (URL path, container name) | ✔ | reconciler, every driver |
| `.metadata.name/namespace/deviceId` | string; deviceId = top-level | – | – (console) |
| `.spec.applicationId` | string | ✔ | reconciler → `Workload.module` |
| `.spec.deploymentProfile.type` | `custom` | ✔ | – (only custom supported) |
| `.…components` | exactly 1 | ✔ | reconciler reads `[0]` |
| `.…properties.repository` | `oci://<host>/<repo>` | ✔ | store (`/v2/<repo>/…`) |
| `.…properties.revision` | string | ✔ | reconciler → status `version` |
| `.…properties.digest` | `sha256:<64 hex>` | ✔ | store (manifest hash), drivers (identity) |
| `.…properties.size` | int | – | – |
| `.spec.parameters` | map name → string/number/bool; `_interfaceHash` (FLEX01), `plc` reserved | – | container/native (env `FLEX_PARAM_*`), mcu-module (`k=v;`, `_interfaceHash`), applab (`inst.name` split), plc-verify (`plc`, `_interfaceHash`) |
| `.x-flex-driver` | `container\|native\|applab\|mcu-module\|mcu-firmware\|plc-verify\|os-rauc\|arduino-ota` | ✔ | reconciler (driver choice, layer `dev.flex.profile`) |
| `.x-flex-safetyClass` | `none\|safety-relevant` (default none) | – | store (= descriptor class, 2 approvers), gate (implicit FE-S6 signals) |
| `.x-flex-activation.requires[]` | signal name or `plc:<dep>[@FLEX01_…]` | – | reconciler `_gate` (+ plc-verify `verified()`) |
| `.x-flex-activation.window` | `{start, end}` `HH:MM` UTC | – | `SafeState.check` |

Health spec, restart policy and run options are **not** in the desired state: they come from the signed release
descriptor (`descriptor.v1alpha1.schema.json`), so the cloud cannot widen them.

## Regenerating

```bash
cd flexengine
.venv/bin/python api/tools/gen_reason_codes.py   # after changing a reason text in the engine (test fails otherwise)
.venv/bin/python api/tools/gen_vectors.py        # deterministic; the test regenerates and compares byte for byte
.venv/bin/python -m pytest tests/api -q
```

`tests/api/golden/` holds valid examples (recorded ones listed with their origin in `SOURCES.json`) and
`invalid.json` (mutations of a valid base + the expected error).

## Decisions of 2026-10-17 (project lead)

- **CP-1 is enforced:** the engine checks every signed desired state against `desired-state.v1alpha1` before it reads
  it. A document that fails is refused as a whole (FE-DS-005) and the previous state keeps running.
- **Refusals are reported:** a refused desired state (FE-DS-001…005: bad signature, not JSON, schema, another device,
  downgrade) is sent once per distinct (document, reason) as a `DeviceEvent` to `POST /api/v1/devices/{id}/events`
  (`device-event.v1alpha1.schema.json`, same seq space as the status records) and shown as `lastRefusal` in the local
  API (`GET /v1/status`) and `flexengine status`.
- **`reasonCode`:** every status record and device event with a reason carries the stable code from
  `reason-codes.v1alpha1.json` (additive field).
- **Plain HTTP** to the control plane only with `allowPlainHttp: true` (or `FLEX_ALLOW_PLAIN_HTTP=1`).

## Additive changes within v1alpha1

- **2026-10-17, EV-17 `os-rauc`** (whole-OS A/B update with RAUC, health-gated `mark-good`): CP-1 `DriverProfile` +
  `os-rauc` (desired state, descriptor `profiles`, descriptor `health.type` `os-rauc`); CP-3 payload media type
  `application/vnd.flex.payload.os-rauc.v1+raucb`, `dev.flex.profile` `os-rauc` and the annotation
  `dev.flex.compatible` (required on os-rauc layers); CP-2 capabilities fragment `$defs/OsRauc` (`compatible`,
  `booted`, `bootPrimary`, `slots[]`, `sharedDataPath`, `dataDirOnSharedPartition`, `available`/`reachable`/`detail`);
  reason codes FE-RAU-PRE-001…007 and FE-RAU-001…014; CP-5: optional class attribute `reboots_on_apply` (the
  reconciler journals a rollback before a rebooting apply and reports it after the next start, see
  [cp5-driver-spi.md](cp5-driver-spi.md#driver-os-rauc-ev-17-driversos_raucpy)).
- **2026-10-17, EV-38 `arduino-ota`** (flexNode-arduino board firmware over the Portenta OTA path): CP-1
  `DriverProfile` + `arduino-ota` (desired state, descriptor `profiles`, descriptor `health.type` `arduino-ota`; the
  deployment parameter `board` names the board); CP-3 payload media type `application/vnd.flex.payload.arduino-ota.v1+ota`,
  `dev.flex.profile` `arduino-ota` and the annotations `dev.flex.board` (FQBN) and `dev.flex.firmwareVersion`
  (both required on arduino-ota layers); CP-2 capabilities fragment `$defs/ArduinoOta` (`boards{fqbn, node,
  reachable, version, status, online}`, `serveUrl`, `signedCommands`, `commit: false`); reason codes
  FE-AOTA-PRE-001…005, FE-AOTA-IMG-001…005, FE-AOTA-KEY-001, FE-AOTA-001…019; CP-6 §5 (board protocol: signed
  `OTA_UPDATE` over MQTT, `ota` / `heartbeat` keys, proposed upstream in `proposals/flexnode-arduino-remote-ota/`).

# flexEngineV2 connection-point contracts (EV-00)

Specs for the engine's edges (docs/flexengine-v2-plan.md §5.0). Each connection point (CP) has a versioned spec, golden
examples, a contract test the engine passes (`tests/api/`) and a reference fake of the other side.
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

Generated reference of every spec (fields, `$defs`, routes, reason codes): [cp-reference.md](cp-reference.md).
Driver authors: [../docs/spi-guide.md](../docs/spi-guide.md).

| CP | Connection point | Spec | Reference fake / producer | Contract tests |
|---|---|---|---|---|
| CP-1 | Desired-state model | `desired-state.v1alpha1.schema.json`, `descriptor.v1alpha1.schema.json` (release descriptor: health, restart, runOptions, `x-flex-mcu`) | `testing/fakecloud.py` (serves), `flex release` (descriptors) | `test_schemas.py`, `test_engine_contracts.py` [V] |
| CP-2 | Status, capabilities, onboarding | `status.v1alpha1.schema.json`, `device-event.v1alpha1.schema.json`, `capabilities.v1alpha1.schema.json`, `onboarding.v1alpha1.schema.json`, `reason-codes.v1alpha1.json` | fakecloud (ingest) | `test_engine_contracts.py`, `test_reason_codes.py`, `test_refusals.py` [V] |
| CP-3 | Artefact access (OCI subset), offline bundle | `oci-profile.v1alpha1.schema.json`, [bundle.v1alpha1.md](bundle.v1alpha1.md), `vectors/release`, `vectors/bundle` | fakecloud `/v2/…`, `/bundle` | `test_vectors.py` [V] |
| CP-4 | Trust | [trust.v1alpha1.md](trust.v1alpha1.md), `trust.v1alpha1.schema.json`, `vectors/` | fakecloud signer, `testing/release.py` | `test_vectors.py` [V] |
| CP-5 | Driver SPI v1 (SPI 1.0) | `driver-spi.v1.schema.json` (members in `x-flex-spi`, `Workload`, `Observed`, `SuperviseEvent`, `BuildRequest`, `BuildResult`), [cp5-driver-spi.md](cp5-driver-spi.md); normative text `drivers/base.py` | fake executors, `FakeDriver` | `test_driver_spi_contract.py` [V], `tests/contract/` (every in-tree driver) [V] |
| CP-6 | Child-device interfaces | [cp6-children.md](cp6-children.md) | QEMU, fake CLI, pyads/pymodbus/shm stand-ins | driver suites (EV-03) |
| CP-7 | Local API + CLI | `local-api.v1.openapi.yaml`, `cli-status.v1.schema.json`; commands and options of the CLI recorded by the freeze gate (`freeze/cli.json`) | `local/api.py` (EV-07) | `test_local_api_contract.py` (every operation, both transports) [V] |
| CP-8 | Telemetry | `telemetry.v1.schema.json` (`LogRecord`, `ActionRecord`, `SpanRecord`, `Metrics`), [telemetry.v1.md](telemetry.v1.md) | collector / JSONL exporter | `test_telemetry_contract.py` [V] |
| all | Southbound OpenAPI | `openapi-southbound.yaml` (P2 0.1.0-p2 → `v1alpha1`, `$ref`s the schemas) | fakecloud | route ⊆ spec, P2 paths kept [V] |

## Versioning and compatibility

- **Two version lines.** CP-1…CP-4 and CP-6 are `v1alpha1`; CP-5 (driver SPI 1.0), CP-7 (local API, CLI) and CP-8
  (telemetry) are `v1`: they are the engine's own interfaces (drivers, operators, collectors) and were written as
  `v1` (plan §5.0). Since FE-3 the three are versioned like the others: a machine-readable spec in the package with
  the version in its file name and `$id` (`driver-spi.v1.schema.json`, `local-api.v1.openapi.yaml` +
  `cli-status.v1.schema.json`, `telemetry.v1.schema.json`), golden examples in `tests/api/golden/`, a contract test,
  a section in `cp-reference.md`, and the same rule: additive only within the version.
- **Freeze gate (FE-3).** The public surface is recorded in [`freeze/`](freeze/README.md) (every spec file, the
  reason codes, the CLI, the engine configuration schema, the vectors); `tools/freeze.py check` (CI job `freeze`, and
  `tests/api/test_freeze.py`) fails on a removal or a change and lets an addition in only with a row in
  `CHANGES.md`. The tag `flexengine-cp-v1alpha1` marks the commit whose baseline froze the connection-point set.
- Version in the file name and `$id` (`…v1alpha1.schema.json`); the wire carries `apiVersion` where the document has one
  (`flex.dev/v1alpha1`, Margo's `application.margo.org/v1alpha1`, `device.margo.org/v1alpha1`).
- **Within a version only additive changes**: a new optional field, a new enum value (e.g. driver profile `os-rauc`
  when it ships), a newly registered `x-flex-*` key, a new reason code. Removing or retyping a field, tightening a
  pattern, or making a field required needs the next version (`v1alpha2`); the engine then accepts both for one
  release.
- **Unknown keys.** Flex-owned objects (DesiredState, `x-flex-activation`, the `x-flex` capabilities block, driver
  fragments, status records, health/restart/runOptions) are closed. Margo-owned objects accept unknown Margo keys
  (ignored) but reject every unregistered `x-` key. Registered: `x-flex-driver`, `x-flex-safetyClass`,
  `x-flex-activation`, `x-flex-dependsOn`, `x-flex-runState`, `x-flex-runtime`, `x-flex-build` (ApplicationDeployment),
  `x-flex` (DeviceCapabilities), `x-flex-mcu` (release descriptor).
- Rules JSON Schema cannot express are checked by `contracts.validate` (desired state: unique deployment ids,
  `metadata.deviceId` = `deviceId`; `x-flex-dependsOn` names other deployments of the document, once each, without
  cycles) [V].
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
| `.x-flex-dependsOn[]` | `{deployment: <id in this document>, condition: started\|healthy}` (default healthy); no unknown id, duplicate or cycle (else the document is refused, FE-DS-006) | – | reconciler (install order + hold FE-GATE-004, reverse removal order FE-GATE-005, crash-resume FE-REC-010) |

Health spec, restart policy and run options are **not** in the desired state: they come from the signed release
descriptor (`descriptor.v1alpha1.schema.json`), so the cloud cannot widen them.

## Regenerating

```bash
cd flexengine
.venv/bin/python api/tools/gen_reason_codes.py   # after changing a reason text in the engine (test fails otherwise)
.venv/bin/python api/tools/gen_vectors.py        # deterministic; the test regenerates and compares byte for byte
.venv/bin/python api/tools/gen_cp_reference.py   # cp-reference.md after any spec change (tests/api/test_cp_reference.py)
.venv/bin/python -m pytest tests/api -q
```

`tests/api/golden/` holds valid examples (recorded ones listed with their origin in `SOURCES.json`) and
`invalid.json` (mutations of a valid base + the expected error).

## Decisions of 2026-10-17 (project lead)

- **CP-1 is enforced:** the engine checks every signed desired state against `desired-state.v1alpha1` before it reads
  it. A document that fails is refused as a whole (FE-DS-005) and the previous state keeps running.
- **Refusals are reported:** a refused desired state (FE-DS-001…006: bad signature, not JSON, schema, another device,
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
- **2026-10-17, EV-42 `x-flex-dependsOn`** (deployment ordering on one device): CP-1 ApplicationDeployment key
  `x-flex-dependsOn` (`$defs/DependsOn`: `[{deployment, condition: started|healthy}]`, default healthy) with the
  semantic rules above; the engine installs and updates in dependency order, holds a deployment whose dependency is
  not ready yet at the GATE (Pending `held: waiting for <ids>`, FE-GATE-004, reported once, re-checked every tick) on
  its previous version, removes dependents before their dependencies (`held: waiting for removal of <ids>`,
  FE-GATE-005) and, after a crash, takes an uncommitted release off again when its dependency is not ready
  (FE-REC-010). A failed dependency holds its dependents; its rollback does not cascade to them. A document with an
  unknown id, a duplicate or a cycle is refused as a whole (FE-DS-006). CP-2 status field `waitingFor`. Southbound
  plan item `dependsOn` (reference cloud: served as `x-flex-dependsOn`, checked against CP-1 at plan time).
- **2026-10-17, EV-38 `arduino-ota`** (flexNode-arduino board firmware over the Portenta OTA path): CP-1
  `DriverProfile` + `arduino-ota` (desired state, descriptor `profiles`, descriptor `health.type` `arduino-ota`; the
  deployment parameter `board` names the board); CP-3 payload media type `application/vnd.flex.payload.arduino-ota.v1+ota`,
  `dev.flex.profile` `arduino-ota` and the annotations `dev.flex.board` (FQBN) and `dev.flex.firmwareVersion`
  (both required on arduino-ota layers); CP-2 capabilities fragment `$defs/ArduinoOta` (`boards{fqbn, node,
  reachable, version, status, online}`, `serveUrl`, `signedCommands`, `commit: false`); reason codes
  FE-AOTA-PRE-001…005, FE-AOTA-IMG-001…005, FE-AOTA-KEY-001, FE-AOTA-001…019; CP-6 §5 (board protocol: signed
  `OTA_UPDATE` over MQTT, `ota` / `heartbeat` keys, proposed upstream in `proposals/flexnode-arduino-remote-ota/`).
- **2026-10-26, FE-4 run state, runtime per deployment, compose, build step** (the four buttons of flexDeploy: Push,
  Build, Run, Stop). CP-1 ApplicationDeployment keys `x-flex-runState` (`$defs/RunState`: staged | running | stopped,
  absent = running), `x-flex-runtime` (`$defs/Runtime`, its profile in `$defs/RuntimeProfiles`, checked by
  `flexengine2.contracts`: FE-DS-005) and `x-flex-build` (`$defs/Build`: `{id}`); descriptor key `build` (command,
  output, dockerfile, timeoutSeconds). CP-2: status states `Staged`, `Stopped`; DeviceEvent types `BuildStarted`,
  `BuildProgress` (`progress`), `BuildLog` (`line`), `BuildSucceeded` (`resultDigest`), `BuildFailed` (`reason`,
  `reasonCode`), each with `deployment` and `buildId` (the fields `reason`, `documentDigest` and
  `acceptedManifestVersion` stay required for `DesiredStateRefused`); capabilities `x-flex.features` (`runState`,
  `runtime`, `build`, `retry`) and `x-flex.runtimes`, container fragment driver `compose`. CP-3 payload media types
  `application/vnd.flex.payload.compose.v1.tar+gzip` and `application/vnd.flex.payload.source.v1.tar+gzip`. Reason
  codes FE-DS-007…009 (a key whose feature the device does not report, a runtime it does not allow, a run state its
  driver cannot carry: refused as a whole), FE-REC-011/012 (start failed), FE-BLD-001…004 (build failed, timed out,
  no source payload, no build step for the profile) with details FE-BLD-010…019, FE-CMP-* (compose), FE-CTR-015,
  FE-NAT-002, FE-SYD-013. CP-5 (additive in 1.0): optional `stage`, `start`, `stop`, `build` and the attribute
  `runtime`; `Workload.build`. Retry: a newer `manifestVersion` whose deployments equal those of the manifest a
  release failed under installs (or starts, or builds) that release again; any other change keeps the verdict.
  Southbound plan items `runState`, `runtime`, `build` (reference cloud: served only to a device that reports the
  feature / runtime).

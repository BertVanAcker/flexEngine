# flexEngine 2.0: implementation

How flexEngine 2.0 is built, for people who integrate with it (a control plane such as flexDeploy, a driver author,
an operator, the flexIA portal) and for people who change it. The operator guide is [README.md](../README.md); the
contracts are in [api/](../api/README.md) (generated reference: [api/cp-reference.md](../api/cp-reference.md));
what changed from 0.6 is in [CHANGES.md](../CHANGES.md).

## 1. What it is

flexEngine 2 runs on an edge device (a LattePanda, an UNO Q's Linux side, a gateway in front of MCUs and PLCs) and
keeps that device in the state a control plane asks for. It is **declarative**: the control plane signs a desired
state (which release of which application runs where, in which run state), the engine pulls it, verifies it,
downloads and verifies the signed releases, and converges the device to it with a health gate and an automatic
rollback per deployment. Nothing on the device is changed by a command; offline the engine keeps reconciling the last
accepted state, and a signed offline bundle can bring a new one.

```
            control plane (flexDeploy, the reference fake cloud)
              │  southbound API (HTTPS, openapi-southbound.yaml, v1alpha1)
              │  onboarding · desired state (CP-1) · status/events (CP-2) · OCI releases (CP-3) · trust (CP-4)
  ┌───────────▼───────────────────────────────────────────────────────────────────┐
  │ host process (host.py): one engine per device, one worker thread each        │
  │  ┌───────────────── device engine (core/reconciler.py) ──────────────────┐   │
  │  │ cloud client ─ outbox ─ desired state ─ reconcile ─ supervise          │   │
  │  │      │                        │                                        │   │
  │  │ artefact store (CAS, verify)  └─ driver SPI (CP-5) ─▶ drivers          │   │
  │  └───────────────────────────────────────────────────────────────────────┘   │
  │ local API (CP-7, Unix socket) · CLI · telemetry (CP-8) · sd_notify/watchdog   │
  └───────────────────────────────────────────────────────────────────────────────┘
        drivers: container (docker, podman, compose, ankaios) · native (systemd, process) · os-rauc ·
                 arduino-ota · mcu-module / mcu-firmware (SMP) · applab · plc-verify
```

Package `flexengine2` (PyPI `flexEngine`), Python ≥ 3.11, Linux (x86_64, aarch64) on devices. Dependencies:
`cryptography`, `PyYAML`, `jsonschema`; extras per driver (`mcu`, `plc`, `applab`, `otel`, `relay`).

## 2. The reconciler (`core/reconciler.py`)

One `Engine` per device. Its loop: flush the status outbox → pull the desired state (ETag) → reconcile → supervise →
sleep (poll interval, back-off when offline). `tick()` never raises: every phase catches what a driver or the
reconciler throws, logs it, counts it (`metrics()`) and backs the device off.

**Accepting a desired state.** The document must carry a valid cloud signature (`cloudPublicKey`), be valid JSON,
match the CP-1 schema (`desired-state.v1alpha1`) and its semantic rules (unique ids, `x-flex-dependsOn` without
unknown ids or cycles), be for this device and have a strictly increasing `manifestVersion`. A key whose feature the
device does not report, a runtime it does not allow or a run state its driver cannot carry is refused too. A refused
document is refused as a whole (reason codes FE-DS-001…009), reported once as a `DesiredStateRefused` device event,
and the previous state keeps running.

**Reconcile** is level-triggered: it compares the last accepted desired state (persisted in `state.json`, so it works
offline and after a restart) with what the drivers observe:

1. *prepare*: download and verify every changed release, all or nothing per manifest; pick the payload layer for the
   driver's platform; run the driver's `preflight`.
2. *remove*: deployments absent from the document (absence = removal), dependents before their dependencies.
3. *install*: per changed deployment, dependencies first: activation gate (safe-state signals, maintenance window,
   PLC verification) → `driver.apply` (or `stage` for a staged/stopped run state) → health gate (the descriptor's
   probe, fail-fast through `driver.failed`) → `driver.commit` → commit (the previous release stays the rollback
   target) or automatic rollback to the previous release (a first install is removed).
4. *drift*: a committed workload that disappeared is re-created; one that runs another digest is re-applied.

Every state transition is a status record (CP-2: `Pending`, `Installing`, `Installed`, `Failed`, `Staged`,
`Stopped`, ...) with a per-device sequence number in a persisted, bounded outbox, so reports survive network cuts
and restarts and are deduplicated upstream. A crash in the middle of an install or a rollback resumes from the
journal in `state.json` (`rolling-back`, `starting`), idempotently: drivers are asked what runs (`status`) before
anything is applied again.

**FE-4: run state, runtime, build (2.0.0rc1).** The four buttons a control plane offers (Push, Build, Run, Stop)
are desired-state keys, not commands:

| Key | Meaning |
|---|---|
| `x-flex-runState: staged \| running \| stopped` | staged installs without starting; stopped stops and keeps the release; a change to running is a start (dependencies first, activation gate, health gate; a failed start rolls back or stops again) |
| `x-flex-runtime` | which runtime of the profile runs it (`docker`, `podman`, `docker-compose`, `systemd`, `process`, ...); the device reports what it allows in `x-flex.runtimes` |
| `x-flex-build: {id}` | build on the device from the release's signed source payload with the descriptor's `build` step, once per build id; `BuildStarted` / `BuildProgress` / `BuildLog` / `BuildSucceeded` / `BuildFailed` events |
| retry | the same deployments in a newer `manifestVersion` try a release that failed again |

The device lists which of these it honours in `x-flex.features`; a control plane serves a key only to a device that
reports its feature.

## 3. Drivers and the SPI (CP-5)

The reconciler knows executors only through the driver SPI 1.0, `drivers/base.py` (normative docstring; machine-
readable `driver-spi.v1.schema.json`; guide [spi-guide.md](spi-guide.md); sheet
[api/cp5-driver-spi.md](../api/cp5-driver-spi.md)). Required: `plan`, `apply`, `remove`, `status`, `list`,
`health`. Optional: `supervise`, `failed`, `preflight`, `commit`, `platform`, `stage` / `start` / `stop` (run
state), `build`. Rules every driver keeps: `apply` is idempotent and rolls back without re-transfer, failures are
`DriverError` only, `status` / `list` / `health` / `capabilities` never raise, an unreachable executor is never
reported as absent, `list` is rebuilt from the executor so a restarted engine adopts what runs. Every in-tree driver
passes the shared conformance suite `tests/contract/`.

| Profile | Drivers | Executor |
|---|---|---|
| `container` | docker, podman, compose, ankaios | container CLI, labels, digest-pinned images loaded from the signed payload |
| `native` | systemd, process | a systemd unit per deployment (or a supervised process); Python payloads with their own wheels |
| `os-rauc` | rauc | whole-OS A/B update, health-gated `mark-good`, rollback across the reboot |
| `arduino-ota` | arduino-ota | flexNode-arduino boards over signed `OTA_UPDATE` (MQTT) |
| `mcu-module`, `mcu-firmware` | mcu | an MCU behind the gateway over SMP (module hot-load, MCUboot image) |
| `applab` | arduino-app-cli | Arduino App Lab apps on the UNO Q |
| `plc-verify` | ads, modbus, shm | verifies the PLC runs the program with the interface hash the release expects |

Board and runtime differences live only in drivers; the reconciler and the wire format do not change per driver.

## 4. Artefact store and trust (CP-3, CP-4)

Releases are OCI artefacts (`oci-profile.v1alpha1`): a release manifest whose config is the release descriptor
(health probe, restart policy, run options, build step: CP-1 `descriptor.v1alpha1`) and whose layers are payloads per
profile and platform, plus an SBOM and a referrer signature manifest. `artefacts/store.py` fetches them into a
content-addressed store and `verify_release` is the supply-chain gate: the manifest hashes to the digest the signed
desired state names; a signature by a trusted release key (`releasePublicKeys`) names exactly that digest and
repository; a safety-relevant release carries approvals by two distinct people; descriptor and payload hash to their
digests. Health, restart and run options come from the signed descriptor, never from the desired state, so the
control plane cannot widen them.

Trust (`trust.v1alpha1.md`): the device creates its own Ed25519 key (`identity/keys.py`, 0600, owned by `flex`),
onboards once with a one-time token, then signs every request; the control plane signs every desired state with its
cloud key. TLS to the control plane is required (`caFile`); plain HTTP only with `allowPlainHttp` in the lab.
Secrets are read from files only. Details: [security-alpha.md](security-alpha.md).

**Offline bundles** (`bundle.v1alpha1.md`): a signed tar with a desired state and the blobs it needs;
`flexengine bundle import` verifies it like an online pull and the engine reconciles to it.

## 5. Local API and CLI (CP-7)

`local/api.py` serves `local-api.v1.openapi.yaml` over HTTP/1.1 on the Unix socket `/run/flexengine/api.sock`
(group `flex`) and, optionally, a read-only TCP bind with a bearer token for desk runbooks: health, status
(`cli-status.v1`), deployments, history, logs, capabilities, metrics, bundle import, reconcile now. No operation
changes the desired state. The CLI (`local/cli.py`: `run`, `status`, `deployments`, `logs`, `history`, `bundle
import`, `enroll`, `doctor`, `config check`, `plc-read`, `version`) is a client of that API, and answers the read
operations from the data directories when no engine runs. `flexengine status --json` prints exactly the
`GET /v1/status` body.

## 6. Telemetry (CP-8)

`telemetry.v1.schema.json` and [api/telemetry.v1.md](../api/telemetry.v1.md): structured JSON log lines on stdout
(journald), the action log `dataDir/actions.jsonl`, the local status history, OpenTelemetry spans
(`engine.install <dep>`, `engine.health-gate`, `engine.rollback`, `engine.start <dep>`, `engine.build`,
`driver.apply|remove|stage|start|stop`; OTLP or a JSONL file; one trace per plan, joined through the `traceparent`
of the desired-state response) and `Engine.metrics()` (`GET /v1/metrics`).

## 7. The connection points and the freeze

| CP | What | Version | Spec |
|---|---|---|---|
| CP-1 | desired-state model, release descriptor | v1alpha1 | `desired-state`, `descriptor` |
| CP-2 | status, device events, capabilities, onboarding, reason codes | v1alpha1 | `status`, `device-event`, `capabilities`, `onboarding`, `reason-codes` |
| CP-3 | artefact access (OCI subset), offline bundle | v1alpha1 | `oci-profile`, `bundle.v1alpha1.md`, vectors |
| CP-4 | trust | v1alpha1 | `trust`, `trust.v1alpha1.md`, vectors |
| CP-5 | driver SPI | v1 (SPI 1.0) | `driver-spi.v1`, `drivers/base.py` |
| CP-6 | child-device interfaces | v1alpha1 | `cp6-children.md` |
| CP-7 | local API and CLI | v1 | `local-api.v1.openapi.yaml`, `cli-status.v1` |
| CP-8 | telemetry | v1 | `telemetry.v1` |
| – | southbound API | v1alpha1 | `openapi-southbound.yaml` |

The machine-readable specs are package data (`flexengine2/api/`), so `flexengine2.contracts.validate(...)` works
from a wheel. Within a version only additive changes are allowed. Since 2.0.0rc1 the **freeze gate** holds this
mechanically: `api/freeze/` records every spec file and its structure, the reason codes and their meanings, the CLI,
the `engine.yaml` schema and the test vectors; `tools/freeze.py check` (CI job `freeze`, required through `ci-ok`)
fails on a removal or a change and lets an addition in only with a row in CHANGES.md
([api/freeze/README.md](../api/freeze/README.md)).

## 8. How a control plane talks to it

A control plane such as flexDeploy implements the southbound API (`openapi-southbound.yaml`, route for route; the
engine's reference fake `flexengine2.testing.fakecloud` is the executable reference):

1. **Onboarding**: the operator creates a one-time token; the engine posts `POST /api/v1/onboarding` with it and its
   public key and stores the cloud key it gets back (it must equal the provisioned trust anchor).
2. **Capabilities**: `PUT /api/v1/devices/{id}/capabilities` (CP-2): drivers, runtimes, features, placement
   fragments (exclusive drivers), SPI version. Plan placement against them.
3. **Desired state**: `GET /api/v1/devices/{id}/deployments` with ETag; the body is the signed CP-1 document
   (`X-Flex-Manifest-Signature`), optionally with a `traceparent`. Bump `manifestVersion` for every change; never
   reuse one.
4. **Releases**: `GET /v2/{repository}/manifests|blobs|referrers/{digest}` (OCI subset, CP-3), signed with a release
   key the device trusts (`flex release`).
5. **Status and events**: `POST .../deployments/{dep}/status` and `POST .../events` (CP-2) with a per-device
   sequence number; deduplicate by it. Every reason carries its stable `reasonCode` (`reason-codes.v1alpha1.json`).
6. **Offline**: `GET .../bundle` serves a signed bundle for a device without a link.

Serve the FE-4 keys only to a device whose capabilities list the feature, and a runtime only when `x-flex.runtimes`
lists it.

## 9. Host process and packaging

`host.py` builds every device engine of one host from `engine.yaml` (schema-checked, unknown keys are errors:
`flexengine config check`; reference [config-reference.md](config-reference.md)), runs one worker thread per engine
and one per MCU relay, and talks to systemd (`Type=notify`, watchdog). On devices flexEngine is installed from the
**offline bundle** of a release: `flexengine-<ver>-linux_<arch>.tar.gz` (GitHub release, with `SHA256SUMS`) holds
`packaging/`, the engine wheel that went to PyPI and a wheelhouse per target Python; its `packaging/install.sh`
installs offline and idempotently (user `flex`, `/etc/flexengine`, `/var/lib/flexengine`, venv in `/opt/flexengine`,
`flexengine.service`), and an upgrade keeps config, tokens and state ([packaging/README.md](../packaging/README.md)).

## 10. Testing

`./run_tests.sh [layer ...]` is the one command for CI and the lab:

| Tier | Layers | Where |
|---|---|---|
| push (every PR) | `unit`, `contract` (SPI conformance on fake executors), `api` (schemas, golden examples, vectors, reference fakes, CP-5/7/8 contract tests, freeze gate), `security`, `property` (push budget), `integration-fast` | `ci.yml`, with the coverage gate (core ≥ 90 %, drivers ≥ 80 %, overall ≥ 85 %), a wheel build and the freeze job; required check `ci-ok` |
| nightly | `contract-nightly` (real docker, podman, systemd, rauc), `property-nightly` (≥ 200 schedules), `integration` (everything, docker-backed cases), `packaging` (clean Ubuntu 24.04 containers with systemd) | `nightly.yml`, plus the push-tier core and full coverage; job `full-run` merges one run into `run-summary.json` |
| release | all of the above | `release.yml` on a `v2.*` tag, then PyPI and the GitHub release |

No framework checkout is needed: the framework files the tests read are vendored (`tests/vectors/framework/`). The
alpha exit check (`tools/alpha_exit_check.py`) reads three merged nightly runs (`--runs`).

## 11. Hardware runbooks

The desk procedures on the lab hardware are YAML runbooks in `hwtest/runbooks/` (EV-HW-01…11), run with
flexNode-arduino's runner and GUI and dry-run against a simulated desk (`hwtest/dryrun.py`) before they go to the lab:
EV-HW-01 install and enrol (LattePanda, from the offline bundle), 02 containers, 03 offline and bundle, 04 Portenta
modules, 05 MCU base image, 06 UNO Q App Lab, 07 plc-verify, 08 security, 09 crash and power cut, 10 observability,
11 RAUC A/B. 2.0.0 is final when the LattePanda runs the published 2.0.0rc bundle and passes EV-HW-01.

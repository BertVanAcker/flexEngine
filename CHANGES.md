# Changes

## 2.0.0 (in progress: 2.0.0a1)

flexEngine 2 replaces flexEngine 0.x. It is a new engine with a new package, configuration, CLI and cloud protocol;
nothing of 0.6 is carried over. What a 0.6 user must do: [MIGRATION.md](MIGRATION.md).

The code was developed as flexEngineV2 in `modular-interoperable-framework/flexengine/` (package `flexengine-v2`) and
moved here with its history (`git subtree split`). 0.6.x stays on branch `v1.x` for fixes while the flexCloud console
still drives v1 devices.

### 2.0.0 compared with 0.6.0

| | 0.6.0 | 2.0.0 |
|---|---|---|
| Model | imperative: the console sends `PUSH`, `BUILD`, `RUN`, `STOP` commands, the engine runs them | declarative: the engine pulls a signed desired state and reconciles the device to it; health gate and automatic rollback per deployment |
| Cloud link | Redis channel `<project>:devices:<device>:cmd`, HMAC-SHA256 with a project shared secret, heartbeat keys | HTTPS southbound API (`openapi-southbound.yaml`, `v1alpha1`): onboarding with a one-time token, per-device Ed25519 key, signed desired state (CP-1), status and events (CP-2), OCI releases (CP-3) |
| What is deployed | a source repository (GitHub or Docker Hub) cloned and built on the device from `flexIA.ini` | signed releases (OCI artefacts, digest-pinned, SBOM, Ed25519 signature, two-person approval for safety-relevant releases); the device never builds |
| Runtimes | `docker build` / `docker run` / `docker compose` | drivers behind a versioned SPI (CP-5): container (docker, podman, ankaios), native (systemd or process, Python wheels), os-rauc, arduino-ota, mcu-module / mcu-firmware (SMP), applab, plc-verify |
| Devices per process | one agent workspace per device | one host process, one engine per device under `devices:`, MCU children through a gateway |
| Offline | none | signed offline bundles (`flexengine bundle import`) |
| Local interface | `flexEngine init/start/stop/status/stats/logs` on a workspace | `flexengine run/status/deployments/logs/history/bundle/enroll/doctor/config/plc-read/version`, a local API (CP-7, Unix socket, optional read-only TCP) |
| Configuration | `configuration/config.yaml` in the workspace (Redis, device credentials, heartbeat) | `/etc/flexengine/engine.yaml`, schema-checked, unknown keys are errors (`flexengine config check`) |
| Installation | `pip install flexEngine` + `flexEngine init`, `scripts/install.sh` / `install.ps1` | `pip install flexEngine` (2.x) for development; on devices the offline bundle with `packaging/install.sh` (user `flex`, systemd unit, polkit rules) |
| Observability | log file, remote log over Redis | structured JSON logs, status history, OpenTelemetry spans and metrics (`otel` extra) |
| Python | ≥ 3.10 | ≥ 3.11 |
| Platforms | Windows, macOS, Linux, Linux ARMv6 | Linux (x86_64, aarch64) for devices; macOS for development. No Windows, no ARMv6 |
| Import name / CLI | `flexEngine` / `flexEngine` | `flexengine2` / `flexengine` (alias `flexengine2`) |
| Dependencies | redis, requests, psutil, PyYAML | cryptography, PyYAML, jsonschema; extras `mcu`, `plc`, `applab`, `otel`, `relay` |

### Contracts

The connection points CP-1 to CP-8 are versioned (`v1alpha1`; the driver SPI, the local API and telemetry `v1`) and
documented in `api/` (`cp-reference.md` is generated). Within `v1alpha1` changes are additive only; a breaking change
is a new version.

### Repository

- `master` holds 2.x; v1's tree (`flexEngine/`, `setup.py`, `requirements.txt`, `examples/`, `scripts/install.*`)
  is on `v1.x` only.
- CI: `ci.yml` (push tier, coverage gate, build; required check `ci-ok`), `nightly.yml`, `release.yml` (tags `v2.*`
  publish to PyPI through the existing trusted publisher).
- The test suite runs without the framework: the framework files it reads are vendored in `tests/vectors/framework/`;
  `flexengine2.testing` ships probe P2's stand-in apps as package data, so its release builders work from a wheel.
- The distribution is `flexEngine` (was `flexengine-v2` in the framework); wheel and bundles are `flexengine-<ver>-…`.

### FE-4: Push, Build, Run, Stop (additions within v1alpha1)

- Run state per deployment (`x-flex-runState: staged | running | stopped`, default running): staged installs without
  starting, stopped stops and keeps the release, the health gate runs on every start; new status states `Staged` and
  `Stopped`; starts follow `x-flex-dependsOn`, stops go in reverse.
- Runtime per deployment (`x-flex-runtime`); `engine.yaml` lists further runtimes per profile
  (`drivers.container.runtimes`, `drivers.native.runtimes`); the device reports `x-flex.runtimes` and refuses a runtime
  it does not allow (FE-DS-008).
- Container runtime `compose` (`runtime: compose`, x-flex-runtime `docker-compose`): a compose project per
  deployment with `docker compose` or `podman compose`, images from the signed payload only, health gate per service,
  roll back as one unit, host access refused unless `composeAllowHostAccess`.
- On-device build (`x-flex-build: {id}`, descriptor `build`) for container (docker, podman, compose) and native
  (process, systemd): once per build id, with `BuildStarted` / `BuildProgress` / `BuildLog` / `BuildSucceeded` /
  `BuildFailed` device events and reason codes FE-BLD-001…004.
- Retry: the same deployments in a newer `manifestVersion` try a failed release again.
- Capabilities `x-flex.features` (`runState`, `runtime`, `build`, `retry`); a key whose feature the device does not
  report is refused as a whole (FE-DS-007), a staged/stopped run state on a driver without it too (FE-DS-009).
- Driver SPI 1.0, additive: optional `stage`, `start`, `stop`, `build`, attribute `runtime`, `Workload.build`.

### FE-3: the 2.0.0 gate

- CP-5, CP-7 and CP-8 are versioned like the other connection points: machine-readable specs in the package
  (`driver-spi.v1.schema.json` with the SPI member table and the `Workload` / `Observed` / `SuperviseEvent` /
  `BuildRequest` / `BuildResult` data, `telemetry.v1.schema.json` with `LogRecord` / `ActionRecord` / `SpanRecord` /
  `Metrics`), golden examples, contract tests and `cp-reference.md` sections; `telemetry.v1.md` describes what the
  engine emits today (JSON logs, FE-4 spans `engine.start`, `engine.build`, `driver.stage|start|stop`, metrics).
- Freeze gate: the public surface (spec files and their structure, reason codes, CLI, engine configuration schema,
  test vectors) is recorded in `api/freeze/`; `tools/freeze.py check` (CI job `freeze`, required through `ci-ok`)
  fails on a removal or a change and lets an addition in only with a row in the table below.

### Contract changes since the freeze

Every addition the freeze gate sees, and every re-recorded baseline, has a row here (api/freeze/README.md).

| Date | Gate | Entries | Why |
|---|---|---|---|
| 2026-10-27 | FE-F1…FE-F6 | all | baseline of the 2.0.0 freeze, recorded on `fe-3/freeze-gate` from master `a2c02eb` (FE-1 move, FE-4 run state) plus the CP-5 / CP-8 specs of FE-3: the connection-point set the tag `flexengine-cp-v1alpha1` freezes |

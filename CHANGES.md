# Changes

## 2.0.0 (in progress: 2.0.0rc1)

flexEngine 2 replaces flexEngine 0.x. It is a new engine with a new package, configuration, CLI and cloud protocol;
nothing of 0.6 is carried over. What a 0.6 user must do: [MIGRATION.md](MIGRATION.md).

The code was developed as flexEngineV2 in `modular-interoperable-framework/flexengine/` (package `flexengine-v2`) and
moved here with its history (`git subtree split`). 0.6.x stays on branch `v1.x` for fixes while the flexCloud console
still drives v1 devices.

| Pre-release | What it is |
|---|---|
| 2.0.0a1 | the alpha: the engine as it passed the alpha exit in the framework (exit commit `fc081c7`), moved here (FE-1) |
| 2.0.0rc1 | the release candidate: 2.0.0a1 + FE-4 (Push, Build, Run, Stop as run state, runtime per deployment, compose, on-device build) + FE-3 (CP-5 / CP-8 specs, the freeze gate, the release bundle). 2.0.0 is this candidate once the bench passes EV-HW-01 from the published bundle |

From 2.0.0a1 to 2.0.0rc1 nothing is removed or renamed: every contract change is an addition within its version
(FE-4 below), so a control plane or driver written against 2.0.0a1 works unchanged. Two things look different: the
offline bundle unpacks into `flexengine-<ver>-linux_<arch>/` (was `flexengine-<ver>/`), and a device reports
`x-flex.features`; a control plane serves the FE-4 keys only to a device that lists their feature.

### 2.0.0 compared with 0.6.0

| | 0.6.0 | 2.0.0 |
|---|---|---|
| Model | imperative: the console sends `PUSH`, `BUILD`, `RUN`, `STOP` commands, the engine runs them | declarative: the engine pulls a signed desired state and reconciles the device to it; health gate and automatic rollback per deployment; Push, Build, Run and Stop are desired-state keys (FE-4) |
| Cloud link | Redis channel `<project>:devices:<device>:cmd`, HMAC-SHA256 with a project shared secret, heartbeat keys | HTTPS southbound API (`openapi-southbound.yaml`, `v1alpha1`): onboarding with a one-time token, per-device Ed25519 key, signed desired state (CP-1), status and events (CP-2), OCI releases (CP-3) |
| What is deployed | a source repository (GitHub or Docker Hub) cloned and built on the device from `flexIA.ini` | signed releases (OCI artefacts, digest-pinned, SBOM, Ed25519 signature, two-person approval for safety-relevant releases); the device builds only when a signed release carries a source payload and a build step (FE-4, `x-flex-build`) |
| Runtimes | `docker build` / `docker run` / `docker compose` | drivers behind a versioned SPI (CP-5): container (docker, podman, compose, ankaios), native (systemd or process, Python wheels), os-rauc, arduino-ota, mcu-module / mcu-firmware (SMP), applab, plc-verify |
| Devices per process | one agent workspace per device | one host process, one engine per device under `devices:`, MCU children through a gateway |
| Offline | none | signed offline bundles (`flexengine bundle import`) |
| Local interface | `flexEngine init/start/stop/status/stats/logs` on a workspace | `flexengine run/status/deployments/logs/history/bundle/enroll/doctor/config/plc-read/version`, a local API (CP-7, Unix socket, optional read-only TCP) |
| Configuration | `configuration/config.yaml` in the workspace (Redis, device credentials, heartbeat) | `/etc/flexengine/engine.yaml`, schema-checked, unknown keys are errors (`flexengine config check`) |
| Installation | `pip install flexEngine` + `flexEngine init`, `scripts/install.sh` / `install.ps1` | `pip install flexEngine` (2.x) for development; on devices the offline bundle of the GitHub release with its `packaging/install.sh` (user `flex`, systemd unit, polkit rules) |
| Observability | log file, remote log over Redis | structured JSON logs, status history, OpenTelemetry spans and metrics (`otel` extra) |
| Python | ≥ 3.10 | ≥ 3.11 |
| Platforms | Windows, macOS, Linux, Linux ARMv6 | Linux (x86_64, aarch64) for devices; macOS for development and as a host test machine ([docs/macos.md](docs/macos.md)). No Windows yet ([docs/windows-plan.md](docs/windows-plan.md)), no ARMv6 |
| Import name / CLI | `flexEngine` / `flexEngine` | `flexengine2` / `flexengine` (alias `flexengine2`) |
| Dependencies | redis, requests, psutil, PyYAML | cryptography, PyYAML, jsonschema; extras `mcu`, `plc`, `applab`, `otel`, `relay` |

### Contracts

The connection points CP-1 to CP-8 are versioned (`v1alpha1`; the driver SPI, the local API and telemetry `v1`) and
documented in `api/` (`cp-reference.md` is generated). Within a version changes are additive only; a breaking change
is a new version. From 2.0.0 the freeze gate (`api/freeze/`, `tools/freeze.py`) holds that: every addition gets a row
in "Contract changes since the freeze" below, a removal or a change fails CI.

### Repository

- `master` holds 2.x; v1's tree (`flexEngine/`, `setup.py`, `requirements.txt`, `examples/`, `scripts/install.*`)
  is on `v1.x` only.
- CI: `ci.yml` (push tier, coverage gate, build, freeze gate; required check `ci-ok`), `nightly.yml` (nightly tier,
  one merged full run per workflow run: artifact `full-run-<run id>`), `release.yml` (tags `v2.*` publish to PyPI
  through the existing trusted publisher, and the offline bundles with `SHA256SUMS` to the GitHub release).
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

### FE-3: the 2.0.0 gate (2.0.0rc1)

- CP-5, CP-7 and CP-8 are versioned like the other connection points: machine-readable specs in the package
  (`driver-spi.v1.schema.json` with the SPI member table and the `Workload` / `Observed` / `SuperviseEvent` /
  `BuildRequest` / `BuildResult` data, `telemetry.v1.schema.json` with `LogRecord` / `ActionRecord` / `SpanRecord` /
  `Metrics`), golden examples, contract tests and `cp-reference.md` sections; `telemetry.v1.md` describes what the
  engine emits today (JSON logs, FE-4 spans `engine.start`, `engine.build`, `driver.stage|start|stop`, metrics).
- Freeze gate: the public surface (spec files and their structure, reason codes, CLI, engine configuration schema,
  test vectors) is recorded in `api/freeze/`; `tools/freeze.py check` (CI job `freeze`, required through `ci-ok`)
  fails on a removal or a change and lets an addition in only with a row in the table below.
- CI evidence: `nightly.yml` runs the push-tier core next to the nightly tier and merges every job into one run
  (`tools/run_summary.py merge`, artifact `full-run-<run id>`), the input of `tools/alpha_exit_check.py --runs`; the
  exit check also runs the freeze gate.
- Release bundles: `release.yml` builds the offline bundles around the wheel it publishes to PyPI
  (`build-wheels.sh --wheel`, new `--bundle-only`) and attaches `SHA256SUMS`; a bundle unpacks into
  `flexengine-<ver>-linux_<arch>/`; `tests/packaging/test_pkg_bundle.py` installs from an unpacked bundle (dry run).
- `docs/implementation.md`: how flexEngine 2.0 is built (for the portal's Docs).
- Version 2.0.0rc1.

### Decisions

Owner decisions on the 2.0.0 behaviour (2026-10-27); the code already behaves this way, this records them.

| # | Decision | Where |
|---|---|---|
| D-1 | Retry: a failed release is tried again only when a newer `manifestVersion` carries the same deployments as the manifest it failed under. There is no `x-flex-retry` key; the control plane asks for a retry by publishing the same desired state once more | `core/reconciler.py` `_retry`, feature `retry` |
| D-2 | "Run one": a dependency whose desired run state is `staged` or `stopped` does not hold its dependents (`x-flex-dependsOn`); they start without waiting for it | `core/reconciler.py` `_waiting_for` |
| D-3 | Compose host access (privileged, cap_add, devices, host namespaces, host bind mounts, ...) is refused at preflight by default; `drivers.container.composeAllowHostAccess: true` in `engine.yaml` allows it | `drivers/compose.py` preflight, `config/schema.json` |
| D-4 | Coverage: flexEngine's own coverage gate leaves the MCU relay (`gateway/relay.py`, needs flexmodel) out and defers applab (`FLEX_COVERAGE_DEFER=applab`, its integration suite needs probe P4's board simulator). Both are measured and gated in the framework's CI: [`flexengine-framework.yml`, job `coverage`](https://github.com/BertVanAcker/modular-interoperable-framework/blob/claude/awesome-ramanujan-tntcrz/.github/workflows/flexengine-framework.yml) runs `./run_tests.sh coverage` with `FLEX_REPO`, flexmodel, P4 and the P3 QEMU build (the relay's live path runs only in the MCU-over-QEMU edge case), without `FLEX_COVERAGE_DEFER` and with `FLEX_COVERAGE_REQUIRE=relay` | `tools/coverage_report.py`: relay target 80 % once measured, `FLEX_COVERAGE_REQUIRE=relay` |

### Contract changes since the freeze

Every addition the freeze gate sees, and every re-recorded baseline, has a row here (api/freeze/README.md).

| Date | Gate | Entries | Why |
|---|---|---|---|
| 2026-10-27 | FE-F1…FE-F6 | all | baseline of the 2.0.0 freeze, recorded on `fe-3/freeze-gate` from master `a2c02eb` (FE-1 move, FE-4 run state) plus the CP-5 / CP-8 specs of FE-3: the connection-point set the tag `flexengine-cp-v1alpha1` freezes |

### Coverage gate

- `tools/coverage_report.py` gates the MCU relay (`gateway/relay.py`, group `relay`, ≥ 80 % lines) whenever flexmodel
  is importable; `FLEX_COVERAGE_REQUIRE=relay` fails the gate when it is not measured. flexEngine's own CI is
  unchanged (no flexmodel there); the framework's CI sets it (decision D-4).

### Desk runbooks

- EV-HW-12 "Four buttons on the LattePanda": Push / Run all / Stop all / Run one / Stop one across the process,
  systemd, docker and docker compose runtimes, the health gate only on start, releases kept installed when stopped,
  and one on-device build that succeeds and one that fails with FE-BLD-001; dry run against the new simulated desk
  profile `fe4`. `deskctl.py` plans the FE-4 keys (`deploy --state / --state-all / --runtime / --build / --after`,
  `--keep` carries them, verb `state` for the buttons) and builds `compose` and `native-source` releases.
- EV-HW-01 installs the release bundle of the GitHub release (`fe_version`, default 2.0.0rc1), checked with
  `sha256sum -c SHA256SUMS` first (new step `bundle-check`), and runs `suite` from a checkout at the release tag with a
  dev venv (the bundle ships no tests).
- The desk defaults no longer point at a framework checkout: EV-HW-08's `tamper` runs from a flexEngine checkout
  (`/opt/flex/flexEngine`), the lab catalog uses the vendored UC1 model, and `hwtest/requirements.txt` no longer
  names `./flexmodel`.

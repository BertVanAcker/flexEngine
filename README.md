# flexEngine 2

flexEngine is the on-device deployment engine of the flex* stack. It pulls a signed desired state from flexCloud (or
flexDeploy), verifies every release, installs it through a driver (container, native/systemd, MCU, App Lab, PLC
check, OS update, Arduino board), runs a health gate and rolls back on failure. It reports every step back to the
cloud. It never builds software on the device.

**2.0 is a new engine, not an update of 0.6.** flexEngine 0.x (the Redis command processor the flexCloud console
drives with Push/Build/Run/Stop) is replaced by a declarative reconciler with signed releases, versioned connection
points (`v1alpha1`) and a driver SPI. What changed and what v1 users must do: [CHANGES.md](CHANGES.md) and
[MIGRATION.md](MIGRATION.md). 0.6.x stays maintained on branch [`v1.x`](https://github.com/BertVanAcker/flexEngine/tree/v1.x).

PyPI project `flexEngine` (2.x), import name `flexengine2`, CLI `flexengine`. Python ≥ 3.11. Apache-2.0.
Status: 2.0.0a1 (alpha; connection points frozen at `v1alpha1`). The engine plan and the decisions behind it are
in the framework repository (`modular-interoperable-framework`, `docs/flexengine-v2-plan.md`).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install --pre 'flexEngine[mcu,plc,applab,otel]>=2.0.0a1'   # pick the extras the device needs
.venv/bin/flexengine version
```

Until 2.0.0 is final, a plain `pip install flexEngine` still installs 0.6.x: ask for 2.x explicitly. On a lab device (LattePanda, UNO Q) install
the offline bundle with `packaging/install.sh` instead: it adds the `flex` user, `/etc/flexengine`, the venv and the
systemd unit ([§1](#1-install)). The `relay` extra also needs `flexmodel`, which is not on PyPI (install it from the
framework repository).

# Operator guide

| You want to | Read |
|---|---|
| install it on the LattePanda, UNO Q or a Mac | [§1](#1-install) and [packaging/README.md](packaging/README.md) |
| write `engine.yaml` | [§2](#2-configure-engineyaml) and [docs/config-reference.md](docs/config-reference.md) (every key) |
| know what each driver needs | [§5](#5-driver-profiles) |
| fix a failed deployment | [§8](#8-troubleshooting-by-reason-code) |
| write a driver | [docs/spi-guide.md](docs/spi-guide.md) |
| integrate with the engine (cloud, release tools, children) | [api/README.md](api/README.md), [api/cp-reference.md](api/cp-reference.md) |
| set up TLS and keys | [docs/security-alpha.md](docs/security-alpha.md) |
| move v1 flexIA packages over | [docs/flexia-import.md](docs/flexia-import.md) and [MIGRATION.md](MIGRATION.md) |
| run the tests | [§9](#9-tests) |

## 1. Install

### LattePanda (Ubuntu 24.04, x86_64) — the lab device

Build the bundle once on any Linux host with Docker, then install it offline on the LattePanda:

```bash
packaging/build-wheels.sh                 # -> dist/flexengine-<ver>-linux_x86_64.tar.gz
scp dist/flexengine-*-linux_x86_64.tar.gz lattepanda.lab:

# on the LattePanda
tar xzf flexengine-*-linux_x86_64.tar.gz && cd flexengine-*/
sudo packaging/install.sh --extras mcu,plc,applab,otel --device-id panda --dry-run   # shows the plan
sudo packaging/install.sh --extras mcu,plc,applab,otel --device-id panda
/opt/flexengine/bin/flexengine version
```

`install.sh` creates the `flex` user, `/etc/flexengine` (config, tokens), `/var/lib/flexengine` (state), the venv in
`/opt/flexengine` and the systemd unit `flexengine.service`. It is idempotent: a second run ends with `changes: 0`.
Upgrade = run the new bundle's `install.sh` with the same options; config, tokens and state are kept. Uninstall:
`--uninstall` (keeps config and data) or `--uninstall --purge`. Details, groups (`docker`, `dialout`), the polkit
rules and the UNO Q (arm64): [packaging/README.md](packaging/README.md).

### Development host (Linux or macOS), no service

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # add ,mcu,plc,applab,otel as needed
.venv/bin/flexengine run --config my-engine.yaml
```

Extras: `mcu` (SMP to MCUs), `plc` (ADS, Modbus), `applab` (App Lab RPC), `otel` (OpenTelemetry), `relay` (MCU ⇄
Zenoh relay; also needs the framework's `flexmodel`, not on PyPI), `dev` (tests).

## 2. Configure `engine.yaml`

One host process runs one engine per device listed under `devices`. A device only gets the drivers it lists. Unknown
keys are errors. `${VAR}` is expanded from the environment; an unset variable makes the file invalid. Every key is in
[docs/config-reference.md](docs/config-reference.md) (generated from the schema).

Worked example: the lab LattePanda with containers, native services, a PLC check, a remote UNO Q and a Portenta MCU
child. The keys are placeholders: put in your own (base64 Ed25519 keys, hex P-256 key).

```yaml
# /etc/flexengine/engine.yaml on the lab LattePanda ("panda") with an MCU child and a PLC
cloud:
  apiUrl: https://flexcloud.lab:8700
  caFile: /etc/flexengine/lab-ca.pem              # the only CA the engine trusts for apiUrl
  cloudPublicKey: AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=   # desired-state signer (base64)
  releasePublicKeys: [ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=]
  pollInterval: 1.0
dataDir: /var/lib/flexengine
local:
  group: flex                                     # members of group flex may use the CLI
  tcp: {bind: "127.0.0.1:8781", tokenFile: /etc/flexengine/tokens/api-ro}
devices:
  - deviceId: panda
    onboardingTokenFile: /etc/flexengine/tokens/panda
    safeStateFile: /var/lib/flexengine/safe-state.json
    drivers:
      container:
        runtime: docker
        allowedRunOptions:
          devices: [/dev/dri]
          env: ["APP_*"]
          ports: ["8080"]
          volumes: [data]
        secretsDir: /etc/flexengine/secrets
      native: {runtime: systemd, unitDir: /var/lib/flexengine/units}
      plc-verify:
        plcs:
          panda/cx: {via: ads, amsNetId: 192.168.50.30.1.1, ip: 192.168.50.30, localNetId: 192.168.50.10.1.1}
          opta: {via: modbus, host: 192.168.50.40}
      applab: {board: unoq, ssh: arduino@unoq.lab}
  - deviceId: panda.portenta                       # MCU child, see-thru over SMP
    parent: panda
    modelDevice: portenta
    onboardingTokenFile: /etc/flexengine/tokens/panda.portenta
    mcu:
      smp: {host: 127.0.0.1, port: 5555}
      modulePublicKey: abababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababababab
```

What the parts do:

- `cloud`: where the desired state comes from and which keys sign it. `https://` needs `caFile` (or
  `allowSystemTrust: true`). Plain `http://` only with `allowPlainHttp: true` (lab and tests).
- `dataDir`: state per device in `<dataDir>/<deviceId>/` (`state.json`, status outbox, artefact store, action log,
  history, device key). Never edit it by hand; `flexengine doctor` checks it.
- `local`: the local API socket (`/run/flexengine/api.sock` by default) and an optional read-only TCP bind with a
  bearer token for desk runbooks.
- `devices[].safeStateFile`: `{"machine_parked": true, "operator_confirmed": true}`. Safety-relevant releases wait
  for both signals (FE-S6).
- `devices[].parent` + `mcu`: an MCU child. The gateway talks to it over SMP; flexCloud sees it as its own device.

Check the file after every edit (`--resolve` also reads key files and token files, so run it on the target):

```bash
flexengine config check /etc/flexengine/engine.yaml --resolve
```

## 3. Enroll and run

1. Create a one-time onboarding token in flexCloud (`POST /api/v1/admin/onboarding-tokens`).
2. Store it where the config expects it (mode 0600, owned by `flex`):

   ```bash
   sudo /opt/flexengine/bin/flexengine enroll --token-file token.txt --device panda --owner flex:flex
   ```

3. Start the service and follow it:

   ```bash
   sudo systemctl enable --now flexengine
   journalctl -u flexengine -f
   ```

At the first start the engine creates its device key (0600), onboards with the token, deletes the token after the
cloud confirmed it, and sends its capabilities. Then it polls the desired state every `pollInterval` seconds. The
unit is `Type=notify` with a 30 s watchdog; systemd restarts the engine if it hangs.

## 4. Operate

All commands are clients of the local API. When the engine is not running they read the data directories instead
(`engine.mode: offline`). `--json` prints machine-readable output (schema
[`cli-status.v1`](api/cp-reference.md#cp-7)). Exit codes: 0 ok, 1 failed, 2 usage or config error.

| Command | Shows |
|---|---|
| `flexengine status [--json]` | per device: accepted `manifestVersion`, online, outbox length, every deployment with state, version, health; `lastRefusal` when a desired state was refused |
| `flexengine deployments [--device D] [dep]` | the deployments, or one in detail (plan, failure, `waitingFor`) |
| `flexengine history [--since 2h] [--device D]` | status transitions (PREPARING, TESTING, COMMITTING, ...) from the local ring buffer |
| `flexengine logs [-f] [--device D] [-n N]` | action log and engine log lines |
| `flexengine doctor [--json]` | config, data dir and key permissions, `state.json`, local API, cloud reachability (TLS), each executor |
| `flexengine doctor --repair --yes` | moves a corrupt `state.json` aside; the engine then starts fresh and gets the desired state again |
| `flexengine bundle import <file> [--device D]` | imports a signed offline bundle (below) |
| `flexengine version` | engine version |

Deployment states (CP-2): `Pending` (held at a gate), `Installing`, `Installed`, `Failed` (with `reason`,
`reasonCode`, `failure`, `rolledBackTo`), `Removing`, `Removed`.

### Offline bundles

A device without a network link can be updated with a USB stick. flexCloud exports a bundle for one device
(`GET /api/v1/admin/devices/{deviceId}/bundle`, format [api/bundle.v1alpha1.md](api/bundle.v1alpha1.md) §3). It holds
the signed desired state and every release it names.

```bash
flexengine bundle import /media/usb/panda-bundle.tar --device panda
```

The same rules as for a cloud answer apply: the desired state must be signed by `cloudPublicKey`, its
`manifestVersion` must be higher than the accepted one, every release is verified. With a running engine the import
goes through the socket and installs at once; without one it is stored and installed at the next start.

## 5. Driver profiles

A deployment names its driver with `x-flex-driver` (CP-1). The release descriptor (signed, from `flex release`) brings
the health probe, restart policy and run options; the cloud cannot widen them. Driver internals:
[api/cp5-driver-spi.md](api/cp5-driver-spi.md). Child-device protocols: [api/cp6-children.md](api/cp6-children.md).

| Profile | Driver | Executor | Config (`devices[].drivers`) |
|---|---|---|---|
| `container` | `docker`, `podman`, `ankaios` | container runtime CLI | `container: {runtime, allowedRunOptions, secretsDir}` |
| `native` | `systemd`, `process` | systemd unit files, or plain processes (dev) | `native: {runtime: systemd \| process, scope, unitDir, user, python}` |
| `os-rauc` | `rauc` | RAUC A/B OS update | `os-rauc: {sharedDataPath, healthChecks, ...}` |
| `arduino-ota` | `portenta-ota` | flexNode-arduino boards over MQTT + Redis | `arduino-ota: {boards, mqtt, redis, serve}` |
| `mcu-module`, `mcu-firmware` | `mcumgr-llext`, `mcumgr-mcuboot` | MCU base image over SMP | an MCU child device with `mcu:` |
| `applab` | `arduino-app-cli` | Arduino App Lab (UNO Q), local or over SSH | `applab: {board, ssh, healthVia, ...}` |
| `plc-verify` | `plc-verify` | reads the PLC's interface hash (ADS, Modbus, shm) | `plc-verify: {plcs}` |

### container

- Images are pinned by digest and labelled with the device and deployment, so the engine finds its containers again
  after a restart.
- **Health types** (descriptor `health.type`): `http` (default; GET `path` on the container IP and `port`), `exec`
  (`command` run with `docker|podman exec`, exit 0 within `execTimeoutSeconds`, default 2 s), `process` (running, not
  restarting, no restart since the last probe). Use `exec` or `process` for nodes without an HTTP endpoint.
- **Restart policy**: `on-failure` (default, `maxRetries` 5), `always`, `never`.
- **Run options** (descriptor `runOptions`): `devices`, `gpus`, `runtime`, `groupAdd`, `env`, `envFrom`, `ports`,
  `volumes`. Each value must be on the device's `allowedRunOptions` (deny by default); otherwise preflight refuses the
  release and the previous version keeps running. `envFrom: {NAME: {file: x}}` reads `<secretsDir>/x` (mode 0600).
  Secret-looking `env` values are refused. `volumes` are runtime named volumes `flex_<device>_<dep>_<name>`: kept
  across updates and rollbacks, deleted with the deployment. Bind mounts are never allowed.
- **Multi-CPU releases**: one release can carry an image per CPU; the engine picks the one that matches the runtime.
- `runtime: ankaios` drives an Ankaios server through `ank` (no run options).

### native (systemd and Python wheels)

- `runtime: systemd` writes one unit file `flex-<device>-<dep>.service` per deployment. Units survive an engine
  restart and a reboot. systemd does the restarts; the engine reports them. Permissions (polkit rule, root, or user
  manager): [packaging/README.md](packaging/README.md#native-workloads-on-systemd-driversnative-runtime-systemd-ev-19).
- `runtime: process` runs the payload as a child process. For development and tests; not for production.
- Payload: a tar.gz with `app.py` or a `unit.json` (`ExecStart`). Health: `http` on `127.0.0.1:$FLEX_HEALTH_PORT`, or
  `process` (main process alive / unit active). Parameters arrive as `FLEX_PARAM_<NAME>`.
- **Own Python dependencies**: a payload with `requirements.lock` (every line pinned with `--hash`) and `wheels/`
  gets its own venv at unpack: offline, hash-checked, never the engine's venv. Build such payloads and multi-CPU
  releases with `tools/native_release.py` (`py-payload`, `flcrs`, `release`).

### os-rauc

Whole-OS A/B update: install into the other slot, reboot, health gate on the new slot, `rauc status mark-good`. A
failed gate reboots back; a slot that never commits is dropped by the bootloader. The data dir must be on the
partition both slots share (`sharedDataPath`). Setup: [packaging/README.md](packaging/README.md#os-updates-with-rauc-driversos-rauc-ev-17).
Exclusive: one OS deployment per device.

### arduino-ota

Firmware for flexNode-arduino boards (Portenta H7, Machine Control, Opta). The engine verifies the `.ota` release,
serves it once on the LAN under a one-time URL, sends the board a signed `OTA_UPDATE` over MQTT and follows the
board's keys in flexCloud Redis until it runs the new version. One firmware per configured board (deployment
parameter `board`). The board side needs the remote-OTA change of flexNode-arduino
([proposals/flexnode-arduino-remote-ota/](proposals/flexnode-arduino-remote-ota/)).

### MCU (mcu-module, mcu-firmware)

An MCU is a child device (`parent:` + `mcu:`) reached over SMP (TCP to QEMU or a serial bridge in alpha).
`mcu-module` loads a signed FLXM module (LLEXT) into the running base image: preflight checks the module signature,
ABI, CPU and interface hash before upload; the module runs in a test state and is confirmed after the health gate.
`mcu-firmware` replaces the base image through MCUboot (test, then confirm; revert without re-upload). The gateway's
optional relay maps MCU topics to Zenoh (`relay:`).

### applab

One App per UNO Q board (exclusive). The engine installs an App release with `arduino-app-cli`, on the board itself
or over SSH from the LattePanda (`ssh: user@host`, health from the app logs). Health: the App's contract handshake
over the router socket (`healthVia: router`) or its logs. `keepReleases` older releases stay installed for a fast
rollback.

### plc-verify

Nothing is downloaded to the PLC. The driver reads the interface hash the PLC program publishes (ADS symbol, Modbus
registers, or the SIL's shared memory) and compares it with the release. Other deployments can wait for it: a
`x-flex-activation.requires` entry `plc:<dep>[@FLEX01_...]` holds them (state `Pending`) until the PLC runs the
expected interface.

## 6. Deployment order and gates

Before a deployment is installed or updated it passes the activation gate. While it waits it is `Pending` and keeps
its previous version.

- **Order on one device** (`x-flex-dependsOn: [{deployment: db, condition: healthy}]`, CP-1): the engine installs
  dependencies first and holds a dependent (`held: waiting for db`, FE-GATE-004) until the dependency runs its desired
  release (`started`) or is healthy (`healthy`, the default). Removal runs in reverse order (FE-GATE-005). A failed
  dependency holds its dependents; its rollback does not cascade. A document with an unknown id, a duplicate or a
  cycle is refused as a whole (FE-DS-006).
- **Safe state** (`x-flex-activation.requires`, implicit `machine_parked` + `operator_confirmed` for
  `x-flex-safetyClass: safety-relevant`): signals from `safeStateFile` (FE-GATE-001).
- **Window** (`x-flex-activation.window: {start, end}` UTC): FE-GATE-002.
- **PLC hold** (`plc:<dep>@hash`): FE-GATE-003.

## 7. What happens on a new desired state

1. The signed document is checked: signature (FE-DS-001), JSON (FE-DS-004), CP-1 schema (FE-DS-005), device id
   (FE-DS-002), `manifestVersion` strictly higher (FE-DS-003), dependencies (FE-DS-006). A refused document changes
   nothing; it is reported once as a device event and shown as `lastRefusal` in `flexengine status`.
2. **Prepare** every changed deployment: download, verify the release signature and digests, driver preflight. One
   refused release rejects the whole manifest version (`artefact rejected: ...`, FE-REC-002); the previous state keeps
   running.
3. **Gate**, then **install**: plan, apply, health gate (`successThreshold` good probes within `timeoutSeconds`;
   `minSeconds` soak for MCU modules), commit for two-phase executors.
4. **Rollback** when apply fails or the gate fails: the previous release comes back without a new download; a failed
   first install is removed. Status `Failed` with `rolledBackTo`.
5. **Supervise** every tick: restarts, drift repair (a missing workload is re-created, FE-REC-006), health.

The engine journals each install. After a crash or a power cut it finishes or undoes that install at the next start
(FE-REC-009, FE-REC-010) and adopts what still runs, without a second apply.

## 8. Troubleshooting by reason code

Every reported reason carries a stable code (`reasonCode`). Full registry (all codes and texts):
[api/cp-reference.md, CP-2](api/cp-reference.md#cp-2). Look at `flexengine deployments --device D <dep>` for the
`failure` text and at `flexengine logs`.

| Code | Text (short) | Usual cause | What to do |
|---|---|---|---|
| FE-DS-001 | bad/missing cloud signature | `cloudPublicKey` does not match the flexCloud signer | fix `cloudPublicKey`; `flexengine doctor` |
| FE-DS-003 | manifestVersion ≤ accepted | an old document or bundle (replay) | export a fresh bundle / plan |
| FE-DS-005 | does not match the CP-1 schema | the cloud or a bundle sent an invalid document | read `lastRefusal`; fix the producer |
| FE-DS-006 | invalid deployment dependencies | `x-flex-dependsOn` names an unknown id or forms a cycle | fix the plan |
| FE-REC-001 | no driver for {profile} | the device config lacks that driver | add it under `devices[].drivers` |
| FE-REC-002 | artefact rejected: {detail} | verification or preflight refused the release; see the detail code below | fix the release or the device config |
| FE-ART-SIG-001/002 | release not signed by a trusted key | release key missing from `releasePublicKeys` | add the key; re-sign the release |
| FE-ART-DIG-002 | digest mismatch | download corrupted or tampered | check the registry / bundle |
| FE-ART-SAF-001 | safety-relevant release lacks a two-person approval | released without two approvers | approve the release again |
| FE-ART-REL-002 | no payload for platform | release has no layer for this CPU / board | build the missing layer |
| FE-CTR-PRE-001, FE-NAT-PRE-001 | built for another CPU | arm64 release on x86_64 or vice versa | release a layer for this CPU |
| FE-CTR-003, FE-CTR-PRE-0xx | run option not allowed | `runOptions` value not on `allowedRunOptions` | allow it on the device, or change the release |
| FE-REC-003 / -005 | apply failed: {error} | the executor refused (image pull, unit start, upload); rolled back | see `failure`; `flexengine doctor` checks the executor |
| FE-REC-004 | health check failed | the new version never became healthy; rolled back | see `failure` (FE-HG-001 timeout, or the driver's reason) and the workload's logs |
| FE-HG-001 | timeout after N s | probe never succeeded | check the descriptor's `health` (port, path, command) |
| FE-HG-002 | commit failed | MCU / RAUC refused the confirm | check the child device |
| FE-GATE-001…005 | held / awaiting / outside window | a gate holds the deployment (state `Pending`) | §6 |
| FE-SYD-004 | systemctl …: Interactive authentication required | no polkit rule, engine not root | install `50-flexengine-systemd.rules` |
| FE-SYD-010 | start limit hit | the native service crashes in a loop | fix the payload; see `journalctl -u flex-*` |
| FE-RAU-PRE-002 | data dir not on the shared data partition | `dataDir` not under `sharedDataPath` | move the data dir (packaging/README.md) |
| FE-RAU-010 | bootloader fell back | the new OS did not boot or never committed | check the new image on the console |
| FE-AOTA-011 | board is offline: no heartbeat key | board off, network down, or library without the remote-OTA patch | power / network; flash the patched library |
| FE-ONB-001 (log) | cloud key at onboarding does not match | provisioned `cloudPublicKey` differs from the cloud's | fix the trust anchor, new token |
| FE-BUN-002 (log) | bundle desired state not signed by the cloud key | bundle from another cloud or tampered | export again |

Cloud unreachable is not a reason code: the engine retries with back-off (`cloud.backoff`), keeps the status reports
in its outbox (`outboxMax`) and sends them when the link returns. `flexengine status` shows `online: false`; `doctor`
shows the TLS or network error.

## 9. Tests

One command runs the suite (plan §7):

```bash
./run_tests.sh             # push tier: unit contract api security property integration-fast
./run_tests.sh nightly    # real executors, nightly property budget, full integration, packaging
./run_tests.sh coverage   # layers 1-6 under coverage.py, gate of plan §7.3
./run_tests.sh --help     # every layer
```

JUnit XML and `run-summary.json` go to `results/latest/` (`FLEX_RESULTS_DIR` to change). CI runs the same layers:
`.github/workflows/ci.yml` (push tier + coverage gate + build, aggregated in the required check `ci-ok`),
`nightly.yml` and `release.yml`.

The suite needs no framework checkout. The few framework files it reads are vendored in `tests/vectors/framework/`
and `src/flexengine2/testing/apps/` (origin and commit in `tests/vectors/framework/SOURCES.json`;
`python tools/vendor_framework.py --framework <checkout>` checks them). The
cases that need the framework's code (probe P3/P4 stand-ins, demo stacks, `flex import-flexia`) are marked
`framework` / `flexmodel` and skip with a reason; to run them, point the suite at a framework checkout:

```bash
FLEX_REPO=../modular-interoperable-framework FLEXMODEL_SRC=../modular-interoperable-framework/flexmodel/src \
  ./run_tests.sh contract integration
```

The alpha exit evidence (2026-10-18, framework commit fc081c7) is kept in `results/`: `alpha-exit-report.md`, the
coverage result and the run summaries of the three CI runs. `tools/alpha_exit_check.py` re-checks it (needs a
framework checkout).

## Layout

| Path | What |
|---|---|
| `src/flexengine2/core/` | reconciler, state, outbox |
| `src/flexengine2/artefacts/`, `identity/`, `cloud/` | artefact store and verification, keys and signatures, cloud client |
| `src/flexengine2/drivers/` | SPI (`base.py`) and every driver |
| `src/flexengine2/local/`, `obs/` | local API, CLI, doctor; logging, history, OpenTelemetry |
| `src/flexengine2/api/` | machine-readable contracts (package data) |
| `src/flexengine2/testing/` | reference fakes (fake cloud, release builders, simulators) for tests and runbooks |
| `api/` | prose contracts, test vectors, generators |
| `packaging/` | installer, systemd units, wheel builds |
| `tests/` | the suite; `tests/vectors/framework/` holds the vendored framework files |
| `results/` | the alpha exit evidence; `results/latest/` (gitignored) is the per-run output |
| `hwtest/` | desk runbooks EV-HW-01…11 for the lab devices |

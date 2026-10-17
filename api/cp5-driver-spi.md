# CP-5 Driver SPI v1 — interface sheet

**Normative text: the module docstring of [`src/flexengine2/drivers/base.py`](../src/flexengine2/drivers/base.py)**
(`SPI_VERSION = "1.0"`, reported as `x-flex.engine.spi`). This sheet is the one-page summary; conformance suite:
`tests/contract/test_spi.py` (EV-03). Where this sheet and `base.py` differ, `base.py` wins.

## Shape

| Member | Kind | Contract (short) |
|---|---|---|
| `profile`, `name` | class attrs | profile ∈ CP-1 `DriverProfile`; name = implementation (`docker`, `process`, …); never `"?"` |
| `exclusive`, `max_deployments` | class attrs | at most N deployments per device (applab, os-rauc: 1); fragment `{exclusive, maxDeployments}` |
| `reboots_on_apply` | class attr (default False; EV-17, additive) | `apply`/`remove` may reboot the device and end the engine: the reconciler journals a rollback before it and finishes it at the next start (os-rauc) |
| `plan(current, desired) -> list[str]` | required | dry run, no side effects; feeds status `plan` (Installing) |
| `apply(current, desired)` | required | idempotent; rollback = `apply(failed, previous)` without re-transfer; only `DriverError` |
| `remove(dep_id)` | required | idempotent |
| `status(dep_id) -> Observed` | required | unknown → `present=False`; unreachable → `present=True, running=False`; never raises |
| `list() -> {dep_id: Observed}` | required | rebuilt from the executor + driver files (adopt after restart); never raises (unreachable → `{}`; not "absent", `status` is the per-deployment truth) |
| `health(dep_id, probe) -> bool` | required | one probe; `probe` = descriptor `health` (CP-1 descriptor schema); never raises |
| `capabilities() -> dict` | required | CP-2 fragment (`capabilities.v1alpha1.schema.json` `$defs/<Profile>`); never raises: CLI missing → `available: false`, executor not answering → `reachable: false` (executor facts omitted, not guessed), reason in `detail` |
| `supervise() -> [event]` | optional | `{"dep", "event": restarted\|start-limit-hit\|exited, "restarts"?, "willRestartAt"?}` |
| `failed(dep_id) -> str\|None` | optional | terminal failure text → status `failure` (registered in reason-codes) |
| `preflight(release, dep) -> str\|None` | optional | reason = refusal (manifest version rejected, `artefact rejected: …`); `DriverError` = defer |
| `commit(dep_id)` | optional | after a passed gate; `DriverError` fails the gate (FE-HG-002) |
| `platform() -> dict\|None` | optional | layer annotations to match (CP-3 §2) |
| `failure_detail(dep_id, healthy_at) -> dict` | optional | extra status fields after a rollback (mcu-module: `faultVisibleAt`, `faultToHealthySeconds`) |
| `verified(dep_id, want)` | profile extension (plc-verify only) | used by the activation gate for `plc:<dep>[@hash]` requirements |

Data: `Workload{dep_id, module, version, digest, payload_path, payload_annotations, parameters, health, restart,
run_options}` (built by the reconciler from CP-1 + the verified release), `Observed{present, running, digest,
version, restarts, detail}`.

## How the reconciler calls it (`core/reconciler.py`)

```
prepare   platform() -> store.verify_release(profile, platform) -> preflight(release, dep)
gate      _gate(): plc-verify.verified(...) for plc: requirements, safe-state signals, window
install   status(dep)  -- present+same digest+running: adopt, no apply
          plan(cur, wl) -> report Installing -> apply(cur, wl)
          health_gate: health(dep, probe) every periodSeconds; failed(dep) after each miss; commit(dep) once passed
          failure: apply(wl, cur) + health_gate(rollback=True) | remove(dep) on a first install; failure_detail()
                   reboots_on_apply: journal {phase: rolling-back, failedDigest, reason, failure, ...} saved first;
                   the next start (_resume_rollback) checks status(dep) = previous (else apply(None, previous)),
                   runs the rollback gate and reports Failed {rolledBackTo, rollbackHealthy}
remove    remove(dep) for deployments absent from the manifest (after the activation gate)
supervise supervise() every tick; status(dep) for drift (absent -> apply(None, wl)); health(dep) every N ticks
```

## Driver: os-rauc (EV-17, `drivers/os_rauc.py`)

Whole-OS A/B update with RAUC (≥ 1.11, `rauc` + `rauc-service` over the system D-Bus). Profile `os-rauc`, name `rauc`,
exclusive (`maxDeployments: 1`), `reboots_on_apply`. Payload: a signed `.raucb` as the layer
`application/vnd.flex.payload.os-rauc.v1+raucb` with `dev.flex.compatible` (CP-3). Config: `drivers.os-rauc`
(config-reference: `sharedDataPath` required, `rebootCommand`, `bootTimeoutSeconds`, `keyring`, `healthChecks`, ...).

| SPI | RAUC / behaviour |
|---|---|
| `platform` | `{"dev.flex.compatible": <rauc status compatible>}` (multi-board release: the engine picks the layer) |
| `preflight` | data dir under `sharedDataPath` (FE-RAU-PRE-001/002), annotation = device compatible (-003/-004), `rauc info [--keyring]` verifies the CMS signature (-005/-006), bundle compatible = annotation (-007); rauc unreachable → deferred |
| `apply` | `rauc install` into the other slot (or `mark-active other` when the image is still there: rollback without re-transfer, `mark-bad booted` first when rolling back from an uncommitted slot); state `<dataDir>/os-rauc/state.json` {dep, per-slot digest/version/bundle hash, target {slot, phase, bootId}}; `rebootCommand`; waits up to `bootTimeoutSeconds` (the reboot ends the engine; a simulated boot returns). Boot into the other slot → FE-RAU-010 (fail fast) |
| `status` | booted slot from `rauc status --detailed --output-format=json` → the digest installed there (checked against RAUC's slot `bundle.hash`); foreign/factory image → not present |
| `health` | booted slot = target, boot status not `bad`, http probe on 127.0.0.1 if the descriptor names a port, every `healthChecks` command exits 0 |
| `failed` | after a reboot the booted slot is not the target (bootloader fell back: image did not come up, boot counter exhausted) → FE-RAU-010; target booted but marked bad → FE-RAU-013 |
| `commit` | `rauc status mark-good booted` (not the target → FE-RAU-014) |
| `supervise` | once per boot `mark-good` of the committed slot (GRUB-style counters count every boot) |
| `remove` | stop managing; a never committed image is rolled back first (mark-bad + mark-active + reboot) |

Across the reboot (plan §3.3): `Installing` + journal `{applying}` are saved before `apply`; the reboot kills the
engine; the new slot's engine reconciles the same desired state, `status` shows the target digest running → adopt (no
second install), health gate (the `minSeconds` soak counts from that boot) → `commit` → Installed. Gate fails →
rollback = `apply(failed, previous)` = mark-bad + reboot into the old slot; the old slot's engine resumes the journal
and reports Failed (`health check failed`, `rolledBackTo`). No engine on the new slot (or it never commits) → the
bootloader's boot counter falls back; the old slot's engine sees FE-RAU-010 and reports Failed (`apply failed`).
"All other workloads healthy" is the reconciler's part: each deployment has its own gate and report.

Errors map to CP-2 texts: `DriverError` in `apply` → `failure: "apply failed: <text>"` (FE-REC-005); in `commit` →
FE-HG-002; in `platform`/`preflight` → deferred, logged only.

## Driver: arduino-ota (EV-38, `drivers/arduino_ota.py`)

flexNode-arduino board firmware over the Portenta OTA path (protocol: [cp6-children.md §5](cp6-children.md); the
node side is a proposed upstream change, `proposals/flexnode-arduino-remote-ota/`). Profile `arduino-ota`, name
`portenta-ota`, exclusive with `maxDeployments` = number of configured boards (one firmware per board; the deployment
parameter `board` picks the board, optional with one board). Payload: one `.ota` image as
`application/vnd.flex.payload.arduino-ota.v1+ota` with `dev.flex.board` (FQBN) and `dev.flex.firmwareVersion`
(CP-3). Config: `drivers.arduino-ota` (`boards`, `mqtt`, `redis`, `serve`, timeouts; config-reference).

| SPI | Behaviour |
|---|---|
| `platform` | `{"dev.flex.board": <FQBN>}` when all boards share one FQBN, else `None` (preflight then checks the layer) |
| `preflight` | board configured (FE-AOTA-PRE-001), `dev.flex.board` present and = board FQBN (-002/-003), `dev.flex.firmwareVersion` (-004), `.ota` header length / CRC-32 / board magic (-005 + FE-AOTA-IMG-*), command key readable (FE-AOTA-002/003). No network: never deferred |
| `apply` | installed digest still on the board → nothing. Else serve once under a one-time URL, publish the signed `OTA_UPDATE`, follow the node's `ota` key until `booted` with the target version; rejected / failed / other version / no answer → `DriverError` (FE-AOTA-011…016, fail fast) |
| `status` | version key vs the firmware installed by this engine (`<dataDir>/arduino-ota/state.json`); running = status in `healthyStatus` + heartbeat key; other firmware → not present (drift); Redis down / board offline → present, not running |
| `health` | `status(...).running` and no update in flight |
| `failed` | in-flight update reported failed/rejected or booted into another version (FE-AOTA-017/018); installed firmware reports a `failedStatus` (FE-AOTA-019) |
| `commit` | none (the Portenta bootloader has no confirm): a bad-but-booting firmware is replaced by the reconciler's rollback, which re-serves the previous release (one transfer) |
| `remove` | stop managing the board (the firmware keeps running) |

The board checks no release signature; the engine's release verification plus the HMAC-signed command (carrying the
image's sha256 and size, checked by the patched library) bind what the board installs to the verified release.

## In-tree drivers

| Profile | Driver (`name`) | Module | Executor | Config (`devices[].drivers`) |
|---|---|---|---|---|
| `container` | `docker`, `podman` | `drivers/container.py` `CliContainerDriver` | docker / podman CLI (labels, digest-pinned) | `container: {runtime: docker\|podman}` |
| `container` | `ankaios` | `drivers/container.py` `AnkaiosDriver` | `ank` CLI → Ankaios server | `container: {runtime: ankaios, …}` |
| `native` | `process` | `drivers/process.py` `ProcessDriver` | local processes; plays systemd itself (tests, dev, Mac) | `native: true` or `{runtime: process}` |
| `native` | `systemd` | `drivers/native/systemd.py` `SystemdDriver` | real systemd unit files (EV-19) | `native: {runtime: systemd, scope, unitDir, user}` |
| `applab` | `arduino-app-cli` | `drivers/applab/driver.py` | arduino-app-cli (local or over ssh); exclusive, one app | `applab: {…}` |
| `mcu-module`, `mcu-firmware` | `mcumgr-llext`, `mcumgr-mcuboot` | `drivers/mcu/driver.py` | SMP to the MCU base image | `mcu: {…}` (MCU child) |
| `plc-verify` | `plc-verify` | `drivers/plc/verify.py` | ADS / Modbus / shm interface-hash reads | `plc-verify: {…}` |
| `os-rauc` | `rauc` | `drivers/os_rauc.py` | RAUC A/B OS update (EV-17) | `os-rauc: {sharedDataPath, …}` |
| `arduino-ota` | `portenta-ota` | `drivers/arduino_ota.py` | flexNode-arduino boards: MQTT command + flexCloud Redis keys + LAN image server (EV-38) | `arduino-ota: {boards, mqtt, redis, serve, …}` |

### `native` / `systemd` (EV-19)

- A deployment is the unit file `flex-<deviceId>-<depId>.service` (ids sanitised to `[A-Za-z0-9_.]`; a changed id
  gets an 8-hex hash of the original, so names never collide) in `unitDir` (system manager default
  `/etc/systemd/system`; another directory is linked with `systemctl enable <path>`; `scope: user` = `systemctl
  --user`, default `~/.config/systemd/user`). **Unit files, not transient units**: they survive an engine restart and
  a reboot, so workloads run without the engine; `systemd-run` units would be gone after a boot.
- Identity: `X-Flex-Device/-Deployment/-Module/-Version/-Digest/-Port` in `[Unit]` (systemd ignores `X-` keys);
  `list()`/`status()` rebuild from the unit files + `systemctl show` (adopt after an engine restart, no second
  `apply`). Supervision baseline (`NRestarts` already reported) in `<dataDir>/native/<dep>/systemd-state.json`.
- Payload: unpacked once per digest to `<dataDir>/native/<dep>/<digest16>/` (atomic; current, previous and
  rolled-back-from trees kept: rollback without re-unpack). `ExecStart` = `<engine python> app.py` or the payload's
  `unit.json` `ExecStart` list (`{python}` = the engine's interpreter). Writable: `<dataDir>/native/<dep>/data`.
- `restartPolicy` → `Restart=` on-failure | always | no, `RestartSec=restartSec` (0.5 s), `StartLimitIntervalSec=60`,
  `StartLimitBurst=maxRetries+1`. systemd restarts; the driver only reports (`supervise()`: `restarted` on an
  `NRestarts` increase, `start-limit-hit`, `exited`).
- `failed()`: `ActiveState=failed` (`Result=start-limit-hit` → FE-SYD-010, other results → FE-SYD-011) or a clean
  exit that `Restart=` will not restart (FE-SYD-012). `health()`: `ActiveState=active` and the HTTP probe on
  `127.0.0.1:$FLEX_HEALTH_PORT`.
- Environment: only `FLEX_UNIT`, `FLEX_HEALTH_PORT`, `FLEX_DATA_DIR`, `FLEX_PARAM_<NAME>`, `PYTHONDONTWRITEBYTECODE`,
  `PYTHONUNBUFFERED` (never the engine's environment). Hardening (system scope): `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=strict` + `ReadWritePaths=` the data dir, `User=` (`user`, default the engine's user). The user
  manager gets `NoNewPrivileges` only.
- `preflight()` refuses a payload without `app.py` or a valid `unit.json`, with members outside the tree, or with an
  unknown `restartPolicy.policy` (FE-SYD-PRE-*). Unreachable manager (`systemctl` fails or hangs; 30 s per call,
  10 s for `show`): `status` = present, not running, `executor unreachable: …`; `capabilities()` `reachable: false`;
  `systemctl` missing: `available: false`.
- Permissions: root, or user `flex` with `packaging/polkit/50-flexengine-systemd.rules` and a `unitDir` the engine
  can write (see packaging/README.md), or `scope: user`.
- Conformance: harnesses `native-systemd[fake-systemctl]` (push) and `native-systemd[real-systemd]` (nightly,
  systemd as PID 1 in Ubuntu 24.04): every case passes except `exclusive_backstop` (skipped: not exclusive); no
  `xfail`, no `na`.

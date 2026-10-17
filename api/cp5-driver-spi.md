# CP-5 Driver SPI v1 — interface sheet

**Normative text: the module docstring of [`src/flexengine2/drivers/base.py`](../src/flexengine2/drivers/base.py)**
(`SPI_VERSION = "1.0"`, reported as `x-flex.engine.spi`). This sheet is the one-page summary; conformance suite:
`tests/contract/test_spi.py` (EV-03). Where this sheet and `base.py` differ, `base.py` wins.

## Shape

| Member | Kind | Contract (short) |
|---|---|---|
| `profile`, `name` | class attrs | profile ∈ CP-1 `DriverProfile`; name = implementation (`docker`, `process`, …); never `"?"` |
| `exclusive`, `max_deployments` | class attrs | at most N deployments per device (applab: 1); fragment `{exclusive, maxDeployments}` |
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
remove    remove(dep) for deployments absent from the manifest (after the activation gate)
supervise supervise() every tick; status(dep) for drift (absent -> apply(None, wl)); health(dep) every N ticks
```

Errors map to CP-2 texts: `DriverError` in `apply` → `failure: "apply failed: <text>"` (FE-REC-005); in `commit` →
FE-HG-002; in `platform`/`preflight` → deferred, logged only.

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

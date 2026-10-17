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

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
| `list() -> {dep_id: Observed}` | required | rebuilt from the executor + driver files (adopt after restart) |
| `health(dep_id, probe) -> bool` | required | one probe; `probe` = descriptor `health` (CP-1 descriptor schema); never raises |
| `capabilities() -> dict` | required | CP-2 fragment (`capabilities.v1alpha1.schema.json` `$defs/<Profile>`) |
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

# Writing a driver for flexEngineV2 (SPI v1)

This guide is for people who add an executor to the engine: a new container runtime, a board, a PLC, an OS updater.
A driver is one Python class. The reconciler only talks to executors through it.

The normative text is the module docstring of
[`src/flexengine2/drivers/base.py`](../src/flexengine2/drivers/base.py) (`SPI_VERSION = "1.0"`). Where this guide and
`base.py` differ, `base.py` wins. Related pages:

- [api/cp5-driver-spi.md](../api/cp5-driver-spi.md): one-page summary of the SPI and the in-tree drivers.
- [api/cp-reference.md](../api/cp-reference.md#cp-5): generated list of every SPI member.
- [config-reference.md](config-reference.md): the `drivers.*` keys of `engine.yaml`.

## 1. What the reconciler does with a driver

One deployment (`dep_id`) goes through these steps (`core/reconciler.py`):

```
prepare   platform()                     -> the store picks the payload layer that fits
          store.verify_release(...)      -> signature, digest, descriptor (not the driver's job)
          preflight(release, dep)        -> a reason refuses the manifest version
gate      activation gate (safe state, window, PLC hold, x-flex-dependsOn)
install   status(dep)                    -> present + same digest + running: adopt, no apply
          plan(current, desired)         -> shown in the Installing report
          apply(current, desired)
          health gate: health() every periodSeconds, failed() after each miss, commit() once passed
rollback  apply(failed, previous) + health gate, or remove(dep) after a failed first install
remove    remove(dep) for a deployment that left the desired state
supervise every tick: supervise(); status(dep) for drift; health(dep) every healthEveryTicks ticks
```

The driver never sees the cloud, the desired-state document or a signature. It gets a `Workload` that the engine has
already verified.

## 2. Data

`Workload` (built by the reconciler from CP-1 and the verified release):

| Field | Meaning |
|---|---|
| `dep_id` | deployment id; the driver runs at most one workload per id |
| `module`, `version` | application id and release revision (shown in status) |
| `digest` | release manifest digest: the identity of what runs. Two workloads with the same digest are the same |
| `payload_path` | the verified payload file in the engine's content-addressed store (read it, never change it) |
| `payload_annotations` | annotations of the chosen layer (`dev.flex.profile`, `dev.flex.arch`, ...) |
| `parameters` | `spec.parameters` of the deployment |
| `health`, `restart`, `run_options` | from the signed release descriptor (`health`, `restartPolicy`, `runOptions`) |

`Observed(present, running=False, digest=None, version=None, restarts=0, detail="")` is what `status()` returns.

## 3. Every method

Required methods are abstract in `Driver`. Optional ones have safe defaults in the base class.

| Method | Required | Must do | May raise |
|---|---|---|---|
| `plan(current, desired)` | yes | return human-readable steps; touch nothing | no |
| `apply(current, desired)` | yes | make `desired` the running workload; idempotent; rollback without re-transfer | `DriverError` only |
| `remove(dep_id)` | yes | stop and delete; unknown id is a no-op | `DriverError` (executor problem; the engine retries) |
| `status(dep_id)` | yes | `Observed`; unknown id: `present=False`; unreachable: `present=True, running=False`, reason in `detail` | **never** |
| `list()` | yes | every workload this engine owns, rebuilt from the executor and the driver's files | **never** (unreachable: `{}`) |
| `health(dep_id, probe)` | yes | one probe run; `False` for unknown id or unreachable executor | **never** |
| `capabilities()` | yes (base gives one) | JSON fragment with `profile`, `driver` and the placement fragment | **never** |
| `supervise()` | no | local restart policy where the executor has none; return events | **never** |
| `failed(dep_id)` | no | a reason once the workload failed for good, else `None` | **never** |
| `preflight(release, dep)` | no | refuse a release before anything reaches the executor | `DriverError` = executor unreachable |
| `commit(dep_id)` | no | confirm a two-phase install after a passed gate | `DriverError` fails the gate |
| `platform()` | no | annotations a layer must carry to fit this executor, or `None` | `DriverError` = executor unreachable |
| `failure_detail(dep_id, healthy_at)` | no (looked up by name) | extra fields for the `Failed` report after a rollback | **never** |

### Never raise

`status`, `list`, `health`, `failed`, `capabilities` and `supervise` are called every tick, also while the executor
is down. They must catch their own errors. If one raises anyway, the engine records a typed error for that phase
(sync, reconcile or supervise) and ends the phase for this tick. The host keeps running, but the deployment makes no
progress until the driver answers, and the conformance suite fails.

`apply`, `remove`, `preflight`, `commit` and `platform` raise `DriverError` and nothing else. `DriverError` always
means "executor problem". It never means "this release is bad" when the check could run before `apply`: that is a
`preflight` reason.

### `status()` and unreachable executors

An unreachable executor must not look like a missing workload. `present=False` starts a drift repair (a second
`apply`). Report `present=True, running=False` and start `detail` with `executor unreachable`. The reconciler then
skips drift and health checks for that deployment until the executor answers.

### `list()` after a restart

`list()` is rebuilt from the executor (labels, unit files, device state) and the driver's own files under the
engine's data dir. Never from memory alone. A new driver instance after an engine restart must find, `status` and
health-check a workload that the old instance started. The engine then adopts it without a second `apply`.

### `apply()` rules

- **Idempotent.** Applying the workload that already runs leaves the same observed state, exactly one instance, and
  no second transfer of a payload the executor already holds. A restart of the workload is allowed.
- **Rollback without re-transfer.** `apply(failed, previous)` reuses what the executor still holds (image, other
  slot, installed release, unpacked tree). Keep the previous payload until the next version is committed.
- **Fail fast.** Raise `DriverError` when the executor refuses or the payload is unusable. The engine then skips the
  health gate and rolls back at once (reason `apply failed: <text>`, FE-REC-005).

### `failed()`: fail fast in the health gate

The gate calls `health()` every `periodSeconds`. After each miss it calls `failed()`. Return a reason only when the
workload has *visibly failed for good*: crash loop exhausted, restart policy `never` and exited, app state `failed`,
device reverted the module, PLC reports another interface hash. Return `None` while it may still come up, for an
unknown deployment, for an unreachable executor and for a healthy workload. A reason ends the gate at once instead of
waiting for `timeoutSeconds`.

### `preflight()`: refuse or defer

A returned string is a **refusal**. The whole manifest version is rejected like a bad signature, the previous state
keeps running and status shows `artefact rejected: <reason>` (FE-REC-002). `DriverError` means **executor
unreachable**: the step is deferred and retried. An unreachable executor must never produce a reason. Check only what
you can check locally: ABI, CPU, capabilities, interface hash, artefact shape, allow-lists.

### `commit()`: two-phase executors

Override `commit` only when the executor installs in a test state and needs a confirm (MCUboot test → confirm, LLEXT
module, RAUC `mark-good`). The engine calls it once, after a passed gate, and only when the class overrides it. A
`DriverError` fails the gate (`commit failed: <text>`, FE-HG-002) and the engine rolls back.

### `platform()`: multi-CPU and multi-board releases

Return the annotations a payload layer must carry, e.g. `{"dev.flex.arch": "aarch64"}`. A release can carry one
layer per platform of the same profile; the store picks the layer that matches. `None` means any layer of the profile
fits. Raise `DriverError` when you need the executor to answer and it does not (the prepare step is deferred).

### Class attributes

| Attribute | Default | Meaning |
|---|---|---|
| `profile` | `"?"` (must be set) | workload profile, a CP-1 `DriverProfile` value (`container`, `native`, ...) |
| `name` | `"?"` (must be set) | implementation name (`docker`, `process`, ...); an instance may refine it |
| `exclusive`, `max_deployments` | `False`, `None` | at most N deployments on this executor; `apply` refuses one more with `DriverError` |
| `reboots_on_apply` | `False` | `apply`/`remove` may reboot the device and end the engine (os-rauc). The reconciler journals a rollback before it and finishes it at the next start. On the forward path, after the reboot `status()` must report the new digest running, so the engine adopts it without a second `apply` |
| `repairs_digest_drift` | `True` | `supervise` re-applies a committed workload that runs another digest. Set `False` when the executor may legitimately run another digest for a while (os-rauc after a bootloader fall-back) |

## 4. Worked example: a spool-directory driver

The executor is a directory that another program (a "player") watches. The driver copies the payload in and writes
`meta.json`; the player answers in `state.json`. A missing directory means the executor is unreachable. The driver is
[`docs/examples/spool_driver.py`](examples/spool_driver.py) and passes the conformance suite (harness
`example-spool`, push tier). The code below is that file (a test keeps them equal):

```python
"""Minimal example driver for the flexEngineV2 driver SPI v1 (docs/spi-guide.md).

The executor is a spool directory that another program (the "player") watches, for example a kiosk player on a
mounted share. Per deployment the driver writes ``<spool>/<dep>/payloads/<digest>`` and ``<spool>/<dep>/meta.json``;
the player answers in ``<spool>/<dep>/state.json`` with ``{"state": "running" | "failed", "digest", "detail"}``.
When the spool directory is missing (share not mounted) the executor is unreachable.

The driver passes the shared conformance suite (tests/contract/test_spi.py, harness ``example-spool``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload


class SpoolDriver(Driver):
    profile = "spool"          # example profile: a real one is registered in CP-1 (DriverProfile) first
    name = "spool-dir"

    def __init__(self, device_id: str, spool_dir: str | Path, max_bytes: int = 1 << 20):
        self.device = device_id
        self.spool = Path(spool_dir)
        self.max_bytes = int(max_bytes)

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _reachable(self) -> bool:
        return self.spool.is_dir()

    def _dir(self, dep_id: str) -> Path:
        return self.spool / dep_id

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, doc: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, path)          # atomic: the player never reads half a file

    def _meta(self, dep_id: str) -> dict | None:
        meta = self._read(self._dir(dep_id) / "meta.json")
        return meta if meta and meta.get("device") == self.device else None

    # ---- required ------------------------------------------------------------------------------------------
    def plan(self, current: Workload | None, desired: Workload | None) -> list[str]:
        if desired is None:
            return [f"remove {current.dep_id} from {self.spool}"] if current else []
        return [f"copy payload {desired.digest[:19]} to {self._dir(desired.dep_id)}/payloads",
                f"point {desired.dep_id} at {desired.module} {desired.version}"]

    def apply(self, current: Workload | None, desired: Workload) -> None:
        if not self._reachable():
            raise DriverError(f"executor unreachable: spool dir {self.spool} missing")
        d = self._dir(desired.dep_id)
        payloads = d / "payloads"
        target = payloads / desired.digest.replace(":", "_")
        try:
            payloads.mkdir(parents=True, exist_ok=True)
            if not target.exists():    # idempotent, and a rollback reuses the payload kept from before
                shutil.copyfile(desired.payload_path, target.with_suffix(".part"))
                os.replace(target.with_suffix(".part"), target)
            meta = {"device": self.device, "dep": desired.dep_id, "module": desired.module,
                    "version": desired.version, "digest": desired.digest, "payload": target.name}
            if self._read(d / "meta.json") != meta:
                self._write(d / "meta.json", meta)
        except OSError as e:
            raise DriverError(f"spool write failed: {e}") from e

    def remove(self, dep_id: str) -> None:
        if not self._reachable():
            raise DriverError(f"executor unreachable: spool dir {self.spool} missing")
        if self._meta(dep_id) is not None:
            shutil.rmtree(self._dir(dep_id), ignore_errors=True)

    def status(self, dep_id: str) -> Observed:
        if not self._reachable():
            return Observed(present=True, running=False, detail=f"executor unreachable: spool dir {self.spool} missing")
        meta = self._meta(dep_id)
        if meta is None:
            return Observed(present=False)
        st = self._read(self._dir(dep_id) / "state.json") or {}
        running = st.get("state") == "running" and st.get("digest") == meta["digest"]
        return Observed(present=True, running=running, digest=meta["digest"], version=meta["version"],
                        detail=str(st.get("detail", "")))

    def list(self) -> dict[str, Observed]:
        if not self._reachable():
            return {}
        try:
            deps = sorted(p.name for p in self.spool.iterdir() if p.is_dir())
        except OSError:
            return {}
        return {dep: self.status(dep) for dep in deps if self._meta(dep) is not None}

    def health(self, dep_id: str, probe: dict) -> bool:
        return self.status(dep_id).running

    def capabilities(self) -> dict:
        return {**super().capabilities(), "spoolDir": str(self.spool), "reachable": self._reachable()}

    # ---- optional ------------------------------------------------------------------------------------------
    def failed(self, dep_id: str) -> str | None:
        if not self._reachable():
            return None                # unreachable is "not known", never "failed"
        meta = self._meta(dep_id)
        st = self._read(self._dir(dep_id) / "state.json") or {}
        if meta and st.get("state") == "failed" and st.get("digest") == meta["digest"]:
            return f"spool player failed: {st.get('detail') or 'no detail'}"
        return None

    def preflight(self, release, dep: dict) -> str | None:
        # checks on the verified payload only: nothing reaches the executor, so it never defers
        try:
            size = os.path.getsize(release.payload_path)
        except OSError:
            return "spool payload missing"
        if size == 0:
            return "spool payload is empty"
        if size > self.max_bytes:
            return f"spool payload of {size} B exceeds {self.max_bytes} B"
        return None
```

What it shows:

- `status()` separates "unknown" (`present=False`) from "unreachable" (`present=True, running=False`, detail
  `executor unreachable: ...`).
- `apply()` keeps one payload file per digest, so a second `apply` and a rollback copy nothing. `meta.json` is written
  with an atomic rename.
- `list()` reads the directory, not memory, so a new instance adopts what the old one started.
- `failed()` returns a reason only when the player reports the current digest as failed.
- `preflight()` checks the payload only and so never defers.
- No `commit()` (the player has no confirm step) and no `platform()` (one layer fits every player).

## 5. Add the driver to the conformance suite

The suite is `tests/contract/test_spi.py`. A harness adapts your driver and its executor (fake or real) to the shared
cases. Steps:

1. Write `tests/contract/spi_harness/<name>.py` with a subclass of `spi_harness.base.Harness`. Example:
   [`spi_harness/example_spool.py`](../tests/contract/spi_harness/example_spool.py). Implement:
   - `setup()` (start the fake executor; `pytest.skip` when a real one is missing), `cleanup()`;
   - `make_driver()`: a **new** instance over the **same** executor and data dir (an engine restart);
   - `workload(version, dep_id)`: a valid, healthy workload; versions `1.0.0` and `2.0.0` must differ in digest;
   - `bad_workload()`: passes `apply`, then visibly fails for good (for `fail_fast`), or `None`;
   - `refused_release()`: `(release, deployment)` that `preflight` must refuse, or `None`;
   - `unreachable()`: a context manager in which the executor cannot be reached; yields a driver;
   - `transfers()`, `instances(dep_id)`: counters the idempotency and rollback cases check (count by
     instrumentation, do not change behaviour);
   - optional `destroy_out_of_band()` (drift), `break_commit()` (two-phase executors), `probe()`.
2. List the cases that do not apply in `na` (skipped, with a reason) and known violations in `xfail` (strict: a fix
   shows up as XPASS and fails the run until you remove the entry).
3. Register the harness in `tests/contract/conftest.py`: `PUSH` for fakes, `NIGHTLY` with a marker (`docker`,
   `podman`, `qemu`) for real executors. Add the driver class to `ALL_DRIVERS` in `test_spi.py` (attribute and
   signature checks).
4. Run it:

   ```bash
   run_tests.sh contract                 # push tier
   run_tests.sh contract-nightly         # real executors that are installed
   PYTEST_ARGS="-k example-spool" run_tests.sh contract
   ```

The cases: identity and capabilities, unknown deployment, install healthy, `failed()` is `None` while healthy,
idempotent apply, restart and adopt, idempotent remove, fail fast, rollback without re-transfer, drift repair,
preflight valid and refusal, platform, supervise, a failing `commit()` fails the gate, exclusive backstop, unreachable
defers, no false drift while unreachable.

## 6. Wire the driver into the engine

Conformance is not enough to ship. A new driver also needs:

1. **Construction**: add it to `build_drivers()` in `core/reconciler.py` (device drivers) or to `host.py` (child
   devices such as MCUs), keyed by its profile.
2. **Configuration**: add `drivers.<profile>` to `src/flexengine2/config/schema.json` with a description for every
   key, then regenerate [config-reference.md](config-reference.md) (`python -m flexengine2.config.refdoc`).
3. **Contracts** (additive changes within `v1alpha1`, listed in [api/README.md](../api/README.md)): the profile in
   CP-1 `DriverProfile` (desired state and descriptor `profiles`), a CP-2 capabilities fragment
   `$defs/<Profile>`, a CP-3 payload media type and the layer annotations you read, then regenerate
   [api/cp-reference.md](../api/cp-reference.md) (`python api/tools/gen_cp_reference.py`).
4. **Reason codes**: every text your driver can report (`DriverError` text, `failed()` and `preflight()` reasons)
   gets a code in `api/tools/gen_reason_codes.py`. `tests/api/test_reason_codes.py` fails while one is missing.
5. **Tests**: unit tests for the driver's own logic, an integration test through a real engine process, and a desk
   runbook for real hardware (plan §8).
6. **Docs**: a section in [api/cp5-driver-spi.md](../api/cp5-driver-spi.md) and the operator view in
   [../README.md](../README.md#5-driver-profiles).

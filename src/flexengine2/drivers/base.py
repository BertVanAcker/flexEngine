"""Driver SPI v1 (``SPI_VERSION = "1.0"``): the only thing the reconciler knows about an executor.

This docstring is the normative specification of the SPI (connection point CP-5). Every in-tree and third-party
driver must pass the shared conformance suite ``tests/contract/test_spi.py``; a change to anything
below that is not backwards compatible needs a new SPI major version. The engine reports the version it speaks
as ``x-flex.engine.spi`` in the device capabilities document.

Terms
-----
*Executor*: what actually runs workloads (a container runtime, systemd, arduino-app-cli, an MCU over SMP, a
PLC). *Deployment id* (``dep_id``): the engine's key for one deployment; a driver runs at most one workload per
deployment id. *Workload*: the verified, executor-ready description of one release revision (below).
*Executor unreachable*: the executor cannot be asked right now (daemon down, cable pulled, MCU rebooting).

Attributes
----------
``profile``        class attribute, the workload profile this driver executes (``container``, ``native``,
                   ``applab``, ``mcu-module``, ``mcu-firmware``, ``plc-verify`` ...); never ``"?"``.
``name``           class attribute, the driver implementation (``docker``, ``process``, ``arduino-app-cli`` ...);
                   an instance may refine it (``podman``), but the class sets a real default; never ``"?"``.
``exclusive``      True when the executor runs at most ``max_deployments`` (default 1) deployments per device
                   (App Lab: one app per board).
``max_deployments`` see ``exclusive``; ignored otherwise.
``reboots_on_apply`` True when ``apply``/``remove`` may reboot the device and so end the engine (os-rauc;
                   additive in 1.0, default False; informational since EV-13). The reconciler writes a
                   ``rolling-back`` journal entry before every rollback, whatever the driver, and at the next start
                   checks (or redoes) the rollback, runs its gate and sends the Failed report. The forward path needs
                   nothing extra: after the reboot ``status`` reports the new digest running and the engine adopts it
                   (no second ``apply``).
``repairs_digest_drift`` default True: ``supervise`` re-applies a committed workload whose ``status`` reports it
                   running with another digest (EV-13). False for an executor that may legitimately run another
                   digest for a while (os-rauc after a bootloader fall-back).

Required methods
----------------
``plan(current, desired) -> list[str]``
    Dry run, human-readable steps; must not touch the executor's state.
``apply(current, desired) -> None``
    Make ``desired`` the running workload of ``desired.dep_id``; ``current`` is what the engine last committed (or
    is rolling back from), ``None`` on a first install or a drift repair. Rules:

    * **Idempotent**: applying the workload that already runs again leaves the same observed state (``status``),
      exactly one workload instance for the deployment, and no second transfer of a payload the executor already
      holds (image load, module upload, app install). It may restart the workload; the engine avoids even that
      by adopting (``status`` present + same digest + running -> no ``apply``).
    * **Rollback without re-transfer** where the executor allows it: ``apply(failed, previous)`` reuses what the
      executor still holds (image in the store, other A/B slot, installed release, unpacked tree).
    * Raises ``DriverError`` and nothing else for every failure (executor refused or unreachable, payload
      unusable); the engine then skips the health gate and rolls back at once (fail-fast).
``remove(dep_id) -> None``
    Stop and delete the workload. Idempotent: removing an unknown or already removed deployment is a no-op.
``status(dep_id) -> Observed``
    What the executor runs for that deployment. Unknown deployment -> ``Observed(present=False)``. An unreachable
    executor must not be reported as ``present=False`` (that would trigger a drift repair): report
    ``present=True, running=False`` with the reason in ``detail``. Never raises.
``list() -> {dep_id: Observed}``
    Every workload this engine owns on the executor. Rebuilt from the executor (labels, unit files, device
    state) plus the driver's own files under the engine's data dir, never from memory alone: a new driver
    instance after an engine restart lists, ``status``-es and health-checks a workload that a previous
    instance started, so the engine can adopt it without a second ``apply``.
``health(dep_id, probe) -> bool``
    One execution of the release's health probe; ``False`` for an unknown deployment or unreachable executor.
    Never raises.
``capabilities() -> dict``
    Fragment for the capabilities document. Always contains ``profile`` and ``driver`` (== ``self.name``) and,
    for an exclusive driver, the placement fragment ``{"exclusive": true, "maxDeployments": N}``
    (``placement()``). flexCloud refuses a plan that places more deployments on that driver at plan time; the
    driver's ``apply`` refuses (``DriverError``) a deployment beyond N as the backstop.

Optional methods (defaults are no-ops)
--------------------------------------
``supervise() -> list[dict]``
    Called every tick: local supervision (restart policy) where the executor does not do it itself. Returns
    events ``{"dep": dep_id, "event": "restarted" | "start-limit-hit" | "exited" | ..., ...}``. Never raises.
``failed(dep_id) -> str | None``
    Fail-fast signal for the health gate. A reason string once the workload has *visibly failed for good*
    (crash loop exhausted, restart policy ``never`` and exited, app in ``failed``, device reverted the module,
    PLC answers a different interface hash); ``None`` while it is not (yet) failed, for an unknown deployment,
    for an unreachable executor, and always for a healthy workload.
``preflight(release, dep) -> str | None``
    Checks before anything reaches the executor (signature, ABI, arch, capabilities, interface hash, artefact
    shape). A returned reason is a **refusal**: the manifest version is rejected like a bad signature and the
    previous state keeps running. ``DriverError`` means **executor unreachable**: the step is deferred and
    retried. An unreachable executor must never produce a reason.
``commit(dep_id) -> None``
    Two-phase executors confirm after a passed health gate (MCUboot / LLEXT test -> confirm, RAUC mark-good).
    ``DriverError`` fails the gate (the engine rolls back). Only called when the driver class overrides it.
``platform() -> dict[str, str] | None``
    Annotations a payload layer must carry to fit this executor (e.g. ``{"dev.flex.arch": "armv7m",
    "dev.flex.floatAbi": "soft"}``); a multi-arch release carries one layer per platform of the same profile and
    the engine picks the match. ``None``: any layer of the profile fits. ``DriverError``: executor unreachable
    (deferred).
``failure_detail(dep_id, healthy_at) -> dict``
    Extra fields for the ``Failed`` report after a rollback (e.g. when the fault became visible on the device).

Run state and build (FE-4, additive in 1.0: optional, a driver without them keeps working as before)
-----------------------------------------------------------------------------------------------------
A driver that implements all three of ``stage``, ``start`` and ``stop`` supports the run states of the desired state
(``x-flex-runState``: staged | running | stopped); the engine refuses a staged or stopped deployment for any other
driver (FE-DS-009). ``supports(drv, "runState")`` tells.

``stage(current, desired) -> None``
    Install ``desired`` (transfer, unpack, load, create) without starting it: afterwards ``status`` reports it
    present, not running, with ``desired.digest``. Whatever ``current`` ran for the deployment is stopped and
    replaced. Idempotent like ``apply``; a later ``start`` needs no second transfer. ``DriverError`` only.
``start(dep_id) -> None``
    Start the installed (staged or stopped) workload; the engine runs the health gate afterwards. Idempotent (a
    running workload stays running). ``DriverError`` when it cannot start (the engine handles it like a failed
    apply: fail fast).
``stop(dep_id) -> None``
    Stop the workload and keep it installed (payload, unpacked tree, image, created container, unit file, build
    result) for a fast ``start``; no restart policy brings it back, also not after a reboot. Idempotent; unknown
    deployment: no-op. A stopped workload is ``present=True, running=False``.
``build(desired, request) -> BuildResult``
    On-device build (``x-flex-build``) from the release's source payload (``request.source_path``) with the signed
    build spec (``request.spec``, the descriptor's ``build``); ``supports(drv, "build")`` tells. Reports progress
    and output lines through ``request.emit("progress", value)`` / ``request.emit("line", text)``, returns the
    payload the engine installs instead of the release's binary payload (``BuildResult``: ``digest`` = what was
    built, ``payload_path`` / ``payload_annotations`` for the workload). Raises ``DriverError`` when the build fails
    (FE-BLD-001), ``BuildTimeout`` after ``request.timeout`` seconds (FE-BLD-002). Must not touch what runs.
``runtime``
    Class/instance attribute: the ``x-flex-runtime`` value this driver instance serves (``docker``,
    ``docker-compose``, ``podman``, ``ankaios``, ``systemd``, ``process``, ``applab``, ``mcu-module``), or ``None``
    for a profile without runtimes. The device's ``x-flex.runtimes`` are the runtimes of its driver instances.
``Workload.build``
    ``{"id", "resultDigest"}`` for a workload built on the device, else empty: a driver that keys its files by
    ``digest`` keys them by ``tree_key(workload)`` so a rebuild of the same release is not mistaken for the old one.

Observation (UM-4, additive in 1.0: optional, a driver without them keeps working as before)
-------------------------------------------------------------------------------------------
Both are called by the engine's observer thread (``obs/workloads.py``), never by the reconcile loop, and possibly
while another member runs on the engine's thread: they only read (the executor, the driver's files), never change
what runs, and never raise. They may take a while (a CLI call), bounded by the driver's own timeouts; the engine
calls them at most every ``observe.resourcesEverySeconds`` / ``observe.workloadLogsEverySeconds``.
``supports(drv, "resources")`` / ``supports(drv, "logs")`` tell.

``resources(dep_ids) -> {dep_id: DeploymentResources}``
    What the workloads of these deployments use now (``driver-spi.v1`` ``$defs/DeploymentResources``):
    ``cpuPercent`` (100 = one CPU fully busy, so above 100 on several CPUs), ``memoryBytes``, ``memoryLimitBytes``,
    ``memoryPercent`` (of the limit, else of the device), ``pids``; every key optional, a deployment the driver
    cannot measure (not running, unknown, executor unreachable) is left out. A workload of several containers or
    processes reports their sum.
``logs(dep_id, cursor, limit) -> WorkloadLogBatch``
    The workload's own stdout/stderr written after ``cursor`` (``None``: the newest ``limit`` lines), oldest first,
    at most ``limit`` lines (the newest ones when more are waiting): ``{"lines": [{"msg", "stream"?: stdout |
    stderr, "t"?: unix s, "container"?}], "cursor": str | None, "dropped"?: n}``. ``cursor`` is opaque to the
    engine: the driver's own position (a timestamp, a journal cursor, file offsets), handed back on the next call
    and kept across engine restarts, so a line is collected once. Unknown deployment or unreachable executor:
    ``{"lines": [], "cursor": cursor}``. Lines without ``t`` get the time they were collected.

Errors
------
``DriverError`` is the only exception a driver raises across the SPI. It means "executor problem" (defer or roll
back), never "this release is bad" when the check could run before ``apply`` (that is ``preflight``'s reason).
"""

from __future__ import annotations

import http.client
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = ["SPI_VERSION", "BuildRequest", "BuildResult", "BuildTimeout", "Driver", "DriverError", "Observed",
           "Workload", "http_probe", "supports", "tree_key", "wait_until"]


@dataclass
class Workload:
    dep_id: str
    module: str
    version: str
    digest: str                    # release manifest digest (identity of what runs)
    payload_path: str              # verified payload in the engine's content-addressed store
    payload_annotations: dict
    parameters: dict = field(default_factory=dict)
    health: dict = field(default_factory=dict)
    restart: dict = field(default_factory=dict)
    run_options: dict = field(default_factory=dict)  # from the signed descriptor (`runOptions`), S4
    build: dict = field(default_factory=dict)        # FE-4: {"id", "resultDigest"} of an on-device build, else {}

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Workload":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Observed:
    present: bool
    running: bool = False
    digest: str | None = None
    version: str | None = None
    restarts: int = 0
    detail: str = ""


SPI_VERSION = "1.0"


class DriverError(RuntimeError):
    pass


class BuildTimeout(DriverError):
    """FE-4: an on-device build ran longer than its timeout."""


@dataclass
class BuildRequest:
    """FE-4: one on-device build (Driver.build)."""
    id: str                        # x-flex-build.id
    source_path: str               # verified source payload (tar.gz) in the engine's store
    spec: dict                     # the signed descriptor's `build` (command, output, dockerfile, timeoutSeconds)
    work_dir: str                  # a directory of this build the driver may use (kept until the next build)
    emit: object = None            # callable(kind: "progress" | "line", value)
    timeout: float = 1800.0

    def progress(self, value: float) -> None:
        if callable(self.emit):
            self.emit("progress", max(0.0, min(1.0, float(value))))

    def line(self, text: str) -> None:
        if callable(self.emit):
            self.emit("line", str(text))


@dataclass
class BuildResult:
    digest: str                    # resultDigest: what the build produced (image id, sha256 of the payload)
    payload_path: str = ""
    payload_annotations: dict = field(default_factory=dict)


def tree_key(wl: Workload) -> str:
    """Name of a workload's files (unpacked tree, unit, ...): the release digest's first 16 hex digits, plus the
    build's result for a workload built on the device (FE-4), so a rebuild never reuses the old tree."""
    key = wl.digest.split(":")[-1][:16]
    rd = (wl.build or {}).get("resultDigest") or ""
    return f"{key}-{rd.split(':')[-1][:12]}" if rd else key


def supports(drv, feature: str) -> bool:
    """Does a driver implement an optional part of the SPI? FE-4 runState: stage + start + stop; build: build;
    UM-4 resources: resources; logs: logs."""
    meths = {"runState": ("stage", "start", "stop"), "build": ("build",), "resources": ("resources",),
             "logs": ("logs",)}[feature]
    cls = drv if isinstance(drv, type) else type(drv)
    return all(getattr(cls, m, None) is not None and getattr(cls, m) is not getattr(Driver, m) for m in meths)


class Driver(ABC):
    profile: str = "?"
    name: str = "?"
    # supervise(): a running workload whose status().digest is another release than the committed one is repaired
    # (re-applied). A driver whose executor may legitimately run another digest for a while sets this False.
    repairs_digest_drift: bool = True

    @abstractmethod
    def plan(self, current: Workload | None, desired: Workload | None) -> list[str]: ...

    @abstractmethod
    def apply(self, current: Workload | None, desired: Workload) -> None: ...

    @abstractmethod
    def remove(self, dep_id: str) -> None: ...

    @abstractmethod
    def status(self, dep_id: str) -> Observed: ...

    @abstractmethod
    def list(self) -> dict[str, Observed]: ...

    @abstractmethod
    def health(self, dep_id: str, probe: dict) -> bool: ...

    exclusive: bool = False
    max_deployments: int | None = None
    reboots_on_apply: bool = False
    runtime: str | None = None     # FE-4: the x-flex-runtime value this instance serves (None: no runtimes)

    def supervise(self) -> list[dict]:
        return []

    def failed(self, dep_id: str) -> str | None:
        return None

    def preflight(self, release, dep: dict) -> str | None:
        return None

    def commit(self, dep_id: str) -> None:
        return None

    def platform(self) -> dict | None:
        return None

    # FE-4 (optional; see the module docstring): run state and on-device build
    def stage(self, current: Workload | None, desired: Workload) -> None:
        raise NotImplementedError("stage/start/stop: optional, see supports()")

    def start(self, dep_id: str) -> None:
        raise NotImplementedError("stage/start/stop: optional, see supports()")

    def stop(self, dep_id: str) -> None:
        raise NotImplementedError("stage/start/stop: optional, see supports()")

    def build(self, desired: Workload, request: BuildRequest) -> BuildResult:
        raise NotImplementedError("build: optional, see supports()")

    # UM-4 (optional; see the module docstring): observation, from the engine's observer thread
    def resources(self, dep_ids: list[str]) -> dict[str, dict]:
        return {}

    def logs(self, dep_id: str, cursor: str | None, limit: int) -> dict:
        return {"lines": [], "cursor": cursor}

    def capabilities(self) -> dict:
        return {"profile": self.profile, "driver": self.name, **self.placement()}

    def placement(self) -> dict:
        """Capability fragment the cloud uses for plan-time placement (exclusive executors)."""
        if not self.exclusive:
            return {}
        return {"exclusive": True, "maxDeployments": int(self.max_deployments or 1)}


def http_probe(host: str, port: int, path: str, timeout: float = 1.0) -> bool:
    try:
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("GET", path, headers={"Connection": "close"})
        r = c.getresponse()
        r.read()
        c.close()
        return 200 <= r.status < 300
    except OSError:
        return False
    except http.client.HTTPException:
        return False


def wait_until(pred, timeout: float, period: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(period)
    return pred()

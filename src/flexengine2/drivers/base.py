"""Driver SPI: the only thing the reconciler knows about an executor.

A driver owns one *profile* of workload (container, native, later app-lab, mcumgr, plc ...). It must be
idempotent and must be able to rebuild its view of the world from the executor itself (labels, unit
files, pid files), because the engine may crash at any point and resume from persisted state.

    plan(current, desired)   -> list of human-readable steps (dry run; feeds deploy.plan previews)
    apply(current, desired)  -> make `desired` the running workload for its deployment id
    remove(dep_id)           -> stop and delete the workload
    status(dep_id)           -> Observed (present, running, digest, version, restarts)
    list()                   -> {dep_id: Observed} for every workload this engine owns
    health(dep_id, probe)    -> bool, one probe execution
    supervise()              -> events from local supervision (restart policy), called every tick
    capabilities()           -> fragment for the capabilities document
    failed(dep_id)           -> optional: a reason string when the workload has *visibly* failed for good
                                (crash loop exhausted, app in `failed`), so the health gate fails fast
                                instead of waiting for its timeout (P4 amendment); None = "not (yet) failed"

Optional SPI methods merged from the probes (D-PICK IG-05: one SPI for every driver, defaults are no-ops):

    preflight(release, dep)  -> reason | None   checks before anything reaches the executor (P3: module signature,
                                ABI, float ABI/arch, capabilities, interface hash; plc-verify: artefact shape). A
                                reason rejects the manifest version like a bad signature (the previous state keeps
                                running); a DriverError means "executor unreachable" and defers the step.
    commit(dep_id)           -> None    two-phase executors confirm after a passed health gate (MCUboot / LLEXT
                                test -> confirm, RAUC mark-good); a DriverError fails the gate
    platform()               -> dict | None   annotations a payload layer must carry to fit this executor (e.g.
                                {"dev.flex.arch": "armv7m", "dev.flex.floatAbi": "soft"}); a multi-arch release
                                carries one layer per platform of the same profile and the engine picks the match

A driver may also declare `exclusive = True` (with `maxDeployments`, default 1, in its capabilities): the
executor runs at most that many deployments per device (App Lab: one app per board). flexCloud refuses a plan
that places more deployments on that driver at plan time (P4 amendment, Margo M8 placement).
"""

from __future__ import annotations

import http.client
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


class Driver(ABC):
    profile: str = "?"
    name: str = "?"

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

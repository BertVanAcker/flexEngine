"""Shared fakes of the EV-05/EV-09 unit tests: an in-memory SPI driver with fault injection, a desired-state builder
and an Engine factory that needs no cloud (apiUrl points at a closed port: every sync phase is Offline, reconcile
still runs on the injected desired state)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import Driver, DriverError, Observed
from flexengine2.identity import crypto

CLOSED = "http://127.0.0.1:9"
SPI_METHODS = ("plan", "apply", "remove", "status", "list", "health", "supervise", "failed", "preflight", "commit",
               "platform", "capabilities")


class FakeDriver(Driver):
    profile = "fake"
    name = "fake"

    def __init__(self, healthy: bool = True):
        self.units: dict[str, dict] = {}
        self.healthy = healthy
        self.raise_in: dict[str, BaseException] = {}   # method -> exception to raise
        self.delay: dict[str, float] = {}             # method -> seconds to block
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def _enter(self, meth: str, dep: str = ""):
        with self.lock:
            self.calls.append((meth, dep))
        d = self.delay.get(meth)
        if d:
            time.sleep(d)
        ex = self.raise_in.get(meth)
        if ex is not None:
            raise ex

    def plan(self, current, desired):
        self._enter("plan", desired.dep_id)
        return [f"run {desired.version}"]

    def apply(self, current, desired):
        self._enter("apply", desired.dep_id)
        self.units[desired.dep_id] = {"digest": desired.digest, "version": desired.version}

    def remove(self, dep_id):
        self._enter("remove", dep_id)
        self.units.pop(dep_id, None)

    def status(self, dep_id):
        self._enter("status", dep_id)
        u = self.units.get(dep_id)
        return Observed(present=bool(u), running=bool(u), digest=(u or {}).get("digest"))

    def list(self):
        self._enter("list")
        return {k: self.status(k) for k in self.units}

    def health(self, dep_id, probe):
        self._enter("health", dep_id)
        return self.healthy and dep_id in self.units

    def supervise(self):
        self._enter("supervise")
        return []

    def failed(self, dep_id):
        self._enter("failed", dep_id)
        return None if self.healthy else "crashed"

    def preflight(self, release, dep):
        self._enter("preflight", dep["metadata"]["id"])
        return None

    def commit(self, dep_id):
        self._enter("commit", dep_id)

    def platform(self):
        self._enter("platform")
        return None

    def capabilities(self):
        self._enter("capabilities")
        return {"profile": self.profile, "driver": self.name}


def deployment(dep_id: str, version: str = "1.0.0", driver: str = "fake", digest: str | None = None) -> dict:
    return {"metadata": {"id": dep_id}, "x-flex-driver": driver,
            "spec": {"applicationId": dep_id, "parameters": {},
                     "deploymentProfile": {"components": [{"properties": {
                         "revision": version, "digest": digest or f"sha256:{dep_id}-{version}",
                         "repository": f"oci://reg/flexnodes/{dep_id}"}}]}}}


def desired(device: str, mv: int, deps: list[dict]) -> dict:
    return {"deviceId": device, "manifestVersion": mv, "deployments": deps}


def make_engine(tmp: Path, device: str = "dev-a", drivers: dict | None = None, **cfg) -> Engine:
    c = {"deviceId": device, "apiUrl": CLOSED, "dataDir": str(tmp / device), "onboardingToken": "t",
         "cloudPublicKey": crypto.public_b64(crypto.generate()), "releasePublicKeys": [], "pollInterval": 0.05,
         "backoff": [0.05, 0.1], "httpTimeout": 0.5, **cfg}
    eng = Engine(c, drivers if drivers is not None else {"fake": FakeDriver()})
    # no registry in unit tests: every release "verifies" to a payload-less release
    eng.store.verify_release = lambda repo, digest, drv, cls, platform=None: SimpleNamespace(
        payload_path="/nonexistent", payload_annotations={}, descriptor={"health": {"timeoutSeconds": 0.3,
                                                                                    "periodSeconds": 0.01}})
    return eng


def set_desired(eng: Engine, mv: int, deps: list[dict]) -> None:
    eng.state["desired"] = desired(eng.device, mv, deps)
    eng.state["manifestVersion"] = mv


def reports(eng: Engine) -> list[dict]:
    return list(eng.state["outbox"])


def write_json(p: Path, obj) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))
    return p


__all__ = ["FakeDriver", "DriverError", "SPI_METHODS", "deployment", "desired", "make_engine", "set_desired",
           "reports", "write_json", "CLOSED"]

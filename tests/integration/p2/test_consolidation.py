"""Consolidation (after P4): engine fail-fast, plan-time placement for exclusive executors, and the stub's
model check for composite releases. No Docker needed (native payloads and an in-process fake driver)."""

from __future__ import annotations

import time

import pytest

from .p2lab import MODEL
from .test_s4 import _admin, _onboard
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer, composite_members
from flexengine2.drivers.base import Driver, DriverError, Observed
from flexengine2.core.reconciler import Engine
from flexengine2.artefacts.oci import MT_PAYLOAD_NATIVE, Layout
from flexengine2.testing.release import (build_native_payload, descriptor_from_module, make_release, model_safety,
                            module_hashes, publish_release, sbom_stub, sign_release)


# ---- fail fast ------------------------------------------------------------------------------------------

class FakeDriver(Driver):
    profile = name = "fake"

    def __init__(self, apply_raises_for=(), fails_for=(), fail_after=0.3):
        self.apply_raises_for, self.fails_for, self.fail_after = set(apply_raises_for), set(fails_for), fail_after
        self.running: dict[str, str] = {}
        self.started: dict[str, float] = {}

    def plan(self, current, desired):
        return [f"run {desired.version}"]

    def apply(self, current, desired):
        self.running[desired.dep_id] = desired.version
        self.started[desired.dep_id] = time.time()
        if desired.version in self.apply_raises_for:
            raise DriverError(f"refused {desired.version}")

    def remove(self, dep_id):
        self.running.pop(dep_id, None)

    def status(self, dep_id):
        v = self.running.get(dep_id)
        return Observed(present=v is not None, running=v is not None, version=v, digest=v)

    def list(self):
        return {k: self.status(k) for k in self.running}

    def health(self, dep_id, probe):
        return self.running.get(dep_id) not in self.fails_for | self.apply_raises_for

    def failed(self, dep_id):
        v = self.running.get(dep_id)
        if v in self.fails_for and time.time() - self.started[dep_id] > self.fail_after:
            return f"{v} crashed"
        return None


class Rel:
    def __init__(self, timeout):
        self.payload_path, self.payload_annotations = "/dev/null", {}
        self.descriptor = {"health": {"periodSeconds": 0.1, "successThreshold": 2, "timeoutSeconds": timeout}}


def _dep(version):
    return {"metadata": {"id": "app"}, "spec": {"applicationId": "app", "parameters": {}, "deploymentProfile": {
        "components": [{"properties": {"revision": version, "digest": version}}]}}, "x-flex-driver": "fake"}


def _engine(tmp_path, drv):
    cfg = {"deviceId": "dev-ff", "dataDir": str(tmp_path / "eng"), "apiUrl": "http://127.0.0.1:9",
           "releasePublicKeys": [], "cloudPublicKey": crypto.public_b64(crypto.generate())}
    e = Engine(cfg, drivers={"fake": drv})
    e.install("app", _dep("1.0.0"), Rel(15))
    assert e.state["applied"]["app"]["wl"]["version"] == "1.0.0"
    return e


def _last(e):
    return [r for r in e.state["outbox"] if r["state"] == "Failed"][-1]


def test_fail_fast_on_apply_error(tmp_path):
    """A refused apply() fails the install at once: no 15 s wait in the health gate."""
    e = _engine(tmp_path, FakeDriver(apply_raises_for={"2.0.0"}))
    t0 = time.time()
    e.install("app", _dep("2.0.0"), Rel(15))
    dt = time.time() - t0
    r = _last(e)
    assert r["reason"] == "apply failed" and r["failure"].startswith("apply failed: refused 2.0.0")
    assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True and r["detectSeconds"] == 0.0
    assert dt < 2.0, dt


def test_fail_fast_on_visibly_failed_workload(tmp_path):
    """A workload the driver reports as failed for good (crash loop exhausted, App `failed`) fails the gate
    after the first failing probe instead of the timeout."""
    e = _engine(tmp_path, FakeDriver(fails_for={"3.0.0"}, fail_after=0.3))
    t0 = time.time()
    e.install("app", _dep("3.0.0"), Rel(15))
    dt = time.time() - t0
    r = _last(e)
    assert r["reason"] == "health check failed" and r["failFast"] is True and r["failure"] == "3.0.0 crashed"
    assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True
    assert r["detectSeconds"] < 1.5 and dt < 3.0, (r, dt)


def test_timeout_still_applies_without_failure_signal(tmp_path):
    drv = FakeDriver(fails_for={"4.0.0"}, fail_after=1e9)  # unhealthy, but never "failed" -> full timeout
    e = _engine(tmp_path, drv)
    e.install("app", _dep("4.0.0"), Rel(1.0))
    r = _last(e)
    assert r["failFast"] is False and r["failure"].startswith("timeout") and r["detectSeconds"] >= 1.0


# ---- plan-time placement for exclusive executors ---------------------------------------------------------

@pytest.fixture()
def cloud_native(tmp_path):
    key = crypto.generate()
    layout = Layout(tmp_path / "cloud" / "oci")
    classes = model_safety(MODEL)
    make_release(layout, tmp_path / "w", key, model_dir=MODEL, module="detection_logger", version="1.0.0",
                 profile="native", hashes=module_hashes(MODEL), classes=classes)
    srv = CloudServer(tmp_path / "cloud", 0, "t", model_dirs=[MODEL]).start()
    yield srv, layout, key, classes
    srv.stop()


def _it(dep_id, module="detection_logger", version="1.0.0", driver="native"):
    return {"id": dep_id, "module": module, "version": version, "driver": driver}


def test_plan_refuses_second_deployment_on_exclusive_driver(cloud_native):
    srv = cloud_native[0]
    _onboard(srv, "board-1")
    srv.state.s["devices"]["board-1"]["capabilities"] = {
        "x-flex": {"drivers": {"native": {"profile": "native", "exclusive": True, "maxDeployments": 1}}}}
    s, b = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "board-1", "deployments": [_it("a"), _it("b")]})
    assert s == 422 and b["title"] == "Placement refused: exclusive executor", b
    assert "runs at most 1 deployment(s), the plan places 2" in b["detail"]
    s, b = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "board-1", "deployments": [_it("a")]})
    assert s == 201, b
    # the P4 applab capability before `exclusive` existed: maxRunningApps is honoured as well
    srv.state.s["devices"]["board-1"]["capabilities"] = {
        "x-flex": {"drivers": {"native": {"profile": "applab", "maxRunningApps": 1}}}}
    s, b = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "board-1", "deployments": [_it("a"), _it("b")]})
    assert s == 422, b
    # a non-exclusive driver takes several
    srv.state.s["devices"]["board-1"]["capabilities"] = {"x-flex": {"drivers": {"native": {"profile": "native"}}}}
    s, b = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "board-1", "deployments": [_it("a"), _it("b")]})
    assert s == 201, b


# ---- composite releases: the model check covers every member ---------------------------------------------

def _composite(layout, key, classes, system, version, declared, members):
    desc = descriptor_from_module(MODEL, "detection_logger", version, profile="native", health={}, restart={},
                                  hashes=module_hashes(MODEL), classes=classes)
    desc.update(module=system, safetyClass=declared,
                composite={"mpu": {i: {"module": m, "version": "1", "interfaceHash": "x"} for i, m in members.items()}})
    m = publish_release(layout, f"flexnodes/{system}", desc,
                        [(MT_PAYLOAD_NATIVE, build_native_payload("detection_logger", version),
                          {"dev.flex.profile": "native"})], sbom_stub(system, version, "native"))
    sign_release(layout, key, f"flexnodes/{system}", m, version)


def test_composite_members_shapes():
    assert composite_members({"composite": {"mcu": {"io": {"module": "a"}}, "mpu": {"ctl": {"module": "b"}}}}) == \
        {"io": "a", "ctl": "b"}
    assert composite_members({"composite": [{"instance": "x", "module": "a"}]}) == {"x": "a"}
    assert composite_members({"module": "a"}) == {}


def test_stub_checks_every_member_of_a_composite_release(cloud_native):
    srv, layout, key, classes = cloud_native
    # perception_guard is safety-relevant in UC1, detection_logger is not -> the App is safety-relevant
    _composite(layout, key, classes, "uc1_app", "1.0.0", "none", {"guard": "perception_guard", "log": "detection_logger"})
    _composite(layout, key, classes, "uc1_app", "1.1.0", "safety-relevant",
               {"guard": "perception_guard", "log": "detection_logger"})
    _composite(layout, key, classes, "uc1_app", "1.2.0", "none", {"log": "detection_logger", "x": "no_such_module"})
    _composite(layout, key, classes, "uc1_app", "1.3.0", "none", {"log": "detection_logger"})
    _onboard(srv, "d1")

    def plan(v):
        return _admin(srv, "POST", "/api/v1/admin/plans",
                      {"deviceId": "d1", "deployments": [_it("app", "uc1_app", v)]})
    s, b = plan("1.0.0")
    assert s == 422 and "model derives 'safety-relevant'" in b["detail"] and "['guard']" in b["detail"], b
    s, b = plan("1.1.0")
    assert s == 201 and b["requiresApproval"] is True, b
    s, b = plan("1.2.0")
    assert s == 422 and b["title"] == "Composite release member not in the model" and "no_such_module" in b["detail"]
    s, b = plan("1.3.0")
    assert s == 201 and b["requiresApproval"] is False, b

"""EV-17 unit tests: the os-rauc driver's checks and capabilities, its config wiring, and the reconciler's
rolling-back journal for drivers whose apply reboots the device (`reboots_on_apply`)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from flexengine2 import contracts
from flexengine2.config import load as cl
from flexengine2.core.reconciler import Engine, build_drivers
from flexengine2.drivers.base import Observed
from flexengine2.drivers.os_rauc import ANN_COMPATIBLE, OsRaucDriver, parse_status
from flexengine2.testing import rauc as trauc

from fe_unit_fakes import FakeDriver, deployment, make_engine, reports, set_desired

CONTRACT = Path(__file__).resolve().parents[1] / "contract"
sys.path.insert(0, str(CONTRACT))
from spi_harness.base import failing_shim  # noqa: E402
from spi_harness.os_rauc import SimRauc  # noqa: E402


@pytest.fixture
def sim(tmp_path):
    return SimRauc(tmp_path / "dev").start()


def drv(sim, tmp_path, data=None, **kw) -> OsRaucDriver:
    return OsRaucDriver("d1", data or tmp_path / "data", {**sim.driver_cfg(tmp_path), **kw})


def release(sim, tmp_path, version="1.0.0", **ann):
    b = sim.bundle(tmp_path / "payloads", version)
    return SimpleNamespace(payload_path=str(b), descriptor={},
                           payload_annotations={"dev.flex.profile": "os-rauc", ANN_COMPATIBLE: "flex-test", **ann})


DEP = deployment("os", driver="os-rauc")


# ---- preflight ------------------------------------------------------------------------------------------------------
def test_preflight_accepts_a_matching_signed_bundle(sim, tmp_path):
    assert drv(sim, tmp_path).preflight(release(sim, tmp_path), DEP) is None


def test_preflight_refuses_a_data_dir_outside_the_shared_partition(sim, tmp_path):
    r = drv(sim, tmp_path, sharedDataPath=str(tmp_path / "other")).preflight(release(sim, tmp_path), DEP)
    assert r.startswith("engine data dir ") and "is not on the shared data partition" in r
    assert contracts.reason_code(r)["code"] == "FE-RAU-PRE-002"
    d = OsRaucDriver("d1", tmp_path / "data", {k: v for k, v in sim.driver_cfg(tmp_path).items()
                                               if k != "sharedDataPath"})
    assert contracts.reason_code(d.preflight(release(sim, tmp_path), DEP))["code"] == "FE-RAU-PRE-001"
    assert d.capabilities()["dataDirOnSharedPartition"] is False


def test_preflight_refuses_compatible_mismatch_and_missing_annotation(sim, tmp_path):
    d = drv(sim, tmp_path)
    rel = release(sim, tmp_path)
    rel.payload_annotations[ANN_COMPATIBLE] = "other-board"
    assert d.preflight(rel, DEP) == "bundle is for compatible other-board, this device is flex-test"
    del rel.payload_annotations[ANN_COMPATIBLE]
    assert contracts.reason_code(d.preflight(rel, DEP))["code"] == "FE-RAU-PRE-003"
    wrong = trauc.build_fake_bundle(tmp_path / "w.raucb", "1.0.0", compatible="other-board")
    rel = SimpleNamespace(payload_path=str(wrong), payload_annotations={ANN_COMPATIBLE: "flex-test"}, descriptor={})
    assert contracts.reason_code(d.preflight(rel, DEP))["code"] == "FE-RAU-PRE-007"


def test_preflight_unreachable_rauc_defers(sim, tmp_path):
    down = failing_shim(tmp_path / "down", "rauc", "Error retrieving slot status via D-Bus")
    from flexengine2.drivers.base import DriverError
    with pytest.raises(DriverError):
        drv(sim, tmp_path, rauc=str(down)).preflight(release(sim, tmp_path), DEP)


# ---- capabilities (CP-2) ----------------------------------------------------------------------------------------------
def _caps(tmp_path, d) -> dict:
    cfg = {"deviceId": "d1", "dataDir": str(tmp_path / "eng"), "apiUrl": "http://127.0.0.1:9", "releasePublicKeys": [],
           "cloudPublicKey": "", "logFormat": "text"}
    doc = Engine(cfg, {"os-rauc": d}).capabilities()
    contracts.validate("capabilities", doc)
    return doc["x-flex"]["drivers"]["os-rauc"]


def test_capabilities_reachable_unreachable_missing(sim, tmp_path):
    f = _caps(tmp_path, drv(sim, tmp_path))
    assert f["exclusive"] is True and f["maxDeployments"] == 1 and f["compatible"] == "flex-test"
    assert f["booted"] == "rootfs.0" and {s["name"] for s in f["slots"]} == {"rootfs.0", "rootfs.1"}
    assert f["dataDirOnSharedPartition"] is True
    down = failing_shim(tmp_path / "down", "rauc", "D-Bus: no service")
    f = _caps(tmp_path, drv(sim, tmp_path, rauc=str(down)))
    assert f["reachable"] is False and "compatible" not in f
    f = _caps(tmp_path, drv(sim, tmp_path, rauc=str(tmp_path / "no-such-rauc")))
    assert f["available"] is False and "not found" in f["detail"]


def test_parse_status_of_rauc_1_11_json():
    st = parse_status('{"compatible":"flex-test","variant":"","booted":"B","boot_primary":"rootfs.1","slots":['
                      '{"rootfs.1":{"class":"rootfs","bootname":"B","state":"booted","boot_status":"good",'
                      '"slot_status":{"bundle":{"compatible":"flex-test","version":"1.0.0","hash":"ab"}}}},'
                      '{"rootfs.0":{"class":"rootfs","bootname":"A","state":"inactive","boot_status":"good",'
                      '"slot_status":{"bundle":{"compatible":null}}}}]}')
    assert (st.booted, st.primary, st.other("rootfs.1"), st.bundle_hash("rootfs.1"), st.bundle_hash("rootfs.0")) == \
        ("rootfs.1", "rootfs.1", "rootfs.0", "ab", None)


# ---- config wiring ------------------------------------------------------------------------------------------------------
def test_build_drivers_and_host_wire_os_rauc(tmp_path):
    out = build_drivers({"deviceId": "d1", "drivers": {"native": False, "os-rauc": {"sharedDataPath": str(tmp_path)}}},
                        tmp_path / "d1")
    assert isinstance(out["os-rauc"], OsRaucDriver) and "native" not in out
    from flexengine2.host import _build_device
    cfg = {"cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": "A" * 43 + "=", "releasePublicKeys": []},
           "dataDir": str(tmp_path / "data"),
           "devices": [{"deviceId": "panda", "onboardingToken": "t",
                        "drivers": {"os-rauc": {"sharedDataPath": str(tmp_path), "rauc": "/nonexistent/rauc"}}}]}
    assert cl.validate(cfg) == []
    eng, _ = _build_device(cfg, cfg["devices"][0], {})
    assert isinstance(eng.drivers["os-rauc"], OsRaucDriver) and eng.drivers["os-rauc"].rauc == "/nonexistent/rauc"


def test_plan_form_keeps_os_rauc_with_shared_data_path():
    doc = {"apiVersion": "flexengine/v2alpha1", "cloud": {"apiUrl": "http://x:1", "cloudPublicKey": "A" * 43 + "="},
           "dataDir": "/data/flexengine",
           "devices": [{"deviceId": "panda", "drivers": {"os-rauc": {"sharedDataPath": "/data", "bundleDir": "/x",
                                                                    "commitTimeout": 300}}}]}
    cfg, notes = cl.from_plan_v2(doc)
    assert cfg["devices"][0]["drivers"]["os-rauc"] == {"sharedDataPath": "/data"}
    assert any(n.startswith("/devices/0/drivers/os-rauc/bundleDir dropped") for n in notes)


# ---- reconciler: rolling-back journal of a rebooting driver ------------------------------------------------------------
class RebootingFake(FakeDriver):
    """apply(failed, previous) "reboots": the process dies (KeyboardInterrupt escapes tick, like SIGKILL)."""
    profile = "os-rauc"
    name = "fake-rauc"
    reboots_on_apply = True

    def __init__(self, units=None, die_on_rollback=False):
        super().__init__()
        self.units = dict(units or {})
        self.die = die_on_rollback

    def apply(self, current, desired):
        super().apply(current, desired)
        if current is not None and desired.version == "1.0.0" and self.die:   # the rollback
            raise KeyboardInterrupt("reboot")

    def health(self, dep_id, probe):
        self._enter("health", dep_id)
        return (self.units.get(dep_id) or {}).get("version") == "1.0.0"

    def failed(self, dep_id):
        return None

    def status(self, dep_id):
        u = self.units.get(dep_id)
        return Observed(present=bool(u), running=bool(u), digest=(u or {}).get("digest"),
                        version=(u or {}).get("version"))


def _installed_v1(tmp_path, drv):
    eng = make_engine(tmp_path, drivers={"os-rauc": drv})
    set_desired(eng, 1, [deployment("os", "1.0.0", "os-rauc")])
    eng.reconcile()
    assert eng.state["applied"]["os"]["wl"]["version"] == "1.0.0"
    return eng


def test_rollback_across_a_reboot_is_reported_by_the_next_start(tmp_path):
    d1 = RebootingFake(die_on_rollback=True)
    eng = _installed_v1(tmp_path, d1)
    set_desired(eng, 2, [deployment("os", "2.0.0", "os-rauc")])
    with pytest.raises(KeyboardInterrupt):
        eng.reconcile()                                          # gate fails -> rollback apply -> "reboot"
    v1 = d1.units["os"]
    # the next start (same data dir): v1 runs again (the bootloader/rollback switched back)
    d2 = RebootingFake(units={"os": v1})
    eng2 = make_engine(tmp_path, drivers={"os-rauc": d2})
    assert eng2.state["journal"]["phase"] == "rolling-back"
    eng2.reconcile()
    failed = [r for r in reports(eng2) if r["state"] == "Failed"]
    assert len(failed) == 1, reports(eng2)
    r = failed[0]
    assert (r["version"], r["reason"], r["rolledBackTo"], r["rollbackHealthy"]) == \
        ("2.0.0", "health check failed", "1.0.0", True), r
    assert r["failure"].startswith("timeout after") and r["reasonCode"] == "FE-REC-004"
    assert eng2.state["journal"] is None and eng2.state["failed"]["os"] == r["digest"]
    assert [c for c in d2.calls if c[0] == "apply"] == []         # landed: no second apply
    eng2.reconcile()                                               # failed digest is not retried
    assert len([r for r in reports(eng2) if r["state"] == "Failed"]) == 1 and not [c for c in d2.calls if c[0] == "apply"]


def test_rollback_that_did_not_land_is_redone(tmp_path):
    d1 = RebootingFake(die_on_rollback=True)
    eng = _installed_v1(tmp_path, d1)
    set_desired(eng, 2, [deployment("os", "2.0.0", "os-rauc")])
    with pytest.raises(KeyboardInterrupt):
        eng.reconcile()
    v2 = {"digest": eng.state["journal"]["failedDigest"], "version": "2.0.0"}
    d2 = RebootingFake(units={"os": v2})                          # still on the failed image
    eng2 = make_engine(tmp_path, drivers={"os-rauc": d2})
    eng2.reconcile()
    assert ("apply", "os") in d2.calls and d2.units["os"]["version"] == "1.0.0"
    r = [r for r in reports(eng2) if r["state"] == "Failed"][-1]
    assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True


def test_non_rebooting_driver_keeps_the_old_path(tmp_path):
    d = RebootingFake()
    d.reboots_on_apply = False
    eng = _installed_v1(tmp_path, d)
    set_desired(eng, 2, [deployment("os", "2.0.0", "os-rauc")])
    eng.reconcile()
    r = [r for r in reports(eng) if r["state"] == "Failed"][-1]
    assert r["rolledBackTo"] == "1.0.0" and eng.state["journal"] is None

"""EV-17 os-rauc driver paths the contract suite does not reach: device-side health checks, a slot marked bad, removal
of a never committed OS (reboot back), the once-per-boot mark-good, and a reboot that fails or never happens.
All on the boot simulator with the fake rauc CLI (tests/contract/spi_harness)."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.drivers.base import DriverError, Workload
from flexengine2.drivers.os_rauc import ANN_COMPATIBLE, OsRaucDriver
from flexengine2.testing import rauc as trauc

CONTRACT = Path(__file__).resolve().parents[1] / "contract"
sys.path.insert(0, str(CONTRACT))
from spi_harness.base import digest_of  # noqa: E402
from spi_harness.os_rauc import PROBE, SimRauc  # noqa: E402


@pytest.fixture
def sim(tmp_path):
    return SimRauc(tmp_path / "dev").start()


def drv(sim, tmp_path, **kw) -> OsRaucDriver:
    return OsRaucDriver("d1", tmp_path / "data", sim.driver_cfg(tmp_path, **kw))


def wl(sim, tmp_path, version="1.0.0", **kw) -> Workload:
    b = sim.bundle(tmp_path / "payloads", version, **kw)
    return Workload(dep_id="os", module="os_image", version=version, digest=digest_of(b.read_bytes()),
                    payload_path=str(b), payload_annotations={"dev.flex.profile": "os-rauc",
                                                              ANN_COMPATIBLE: trauc.COMPATIBLE},
                    health=dict(PROBE))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_health_runs_the_device_side_checks_and_the_local_http_probe(sim, tmp_path):
    good, sick = wl(sim, tmp_path, "1.0.0"), wl(sim, tmp_path, "2.0.0", healthy=False)
    d = drv(sim, tmp_path, healthChecks=[sim.health_check_cmd()])
    d.apply(None, good)
    assert sim.show()["booted"] == "B"
    assert d.health("os", {}) is True
    assert d.health("os", {"port": free_port()}) is False          # nothing listens: the new OS's service is down
    assert d.health("os", {"port": "not-a-port"}) is False
    assert d.health("other-dep", {}) is False
    d.apply(good, sick)                                              # boots, but its health check exits 1
    assert d.status("os").digest == sick.digest
    assert d.health("os", {}) is False
    broken = drv(sim, tmp_path, healthChecks=[[str(tmp_path / "no-such-check")]])
    assert broken.health("os", {}) is False                          # a check that cannot run is a failed check


def test_failed_reports_a_booted_slot_marked_bad(sim, tmp_path):
    d = drv(sim, tmp_path)
    v1 = wl(sim, tmp_path)
    d.apply(None, v1)
    assert d.failed("os") is None
    d._rauc("status", "mark-bad", "booted")
    why = d.failed("os")
    assert why == "booted slot rootfs.1 is marked bad" and contracts.reason_code(why)["code"] == "FE-RAU-013"
    assert d.health("os", {}) is False
    with pytest.raises(DriverError, match="not marking it good|failed"):
        d.commit("other-dep")


def test_removing_a_never_committed_os_reboots_back_and_remembers_why(sim, tmp_path):
    d = drv(sim, tmp_path)
    v1 = wl(sim, tmp_path)
    d.apply(None, v1)                       # installed into B and booted, but never committed
    assert sim.show()["booted"] == "B"
    d.remove("os")
    assert sim.show()["booted"] == "A", sim.show()["log"]
    assert d.status("os").present is False and d.list() == {}
    with pytest.raises(DriverError, match="was removed before it was committed"):
        d.apply(None, v1)                   # the refusal is reported once, then the next apply installs again
    d.remove("never-deployed")              # unknown deployment: no-op


def test_committed_os_is_marked_good_once_per_boot(sim, tmp_path):
    d = drv(sim, tmp_path)
    v1 = wl(sim, tmp_path)
    d.apply(None, v1)
    d.commit("os")
    assert d.supervise() == []              # this boot is confirmed already
    sim.boot()                              # an ordinary reboot of the same slot: new boot id
    ev = d.supervise()
    assert ev == [{"dep": "os", "event": "boot-confirmed", "slot": "rootfs.1", "version": "1.0.0"}]
    assert d.supervise() == []
    assert any("mark-good" in line or "good" in line for line in sim.show()["log"])


def test_plan_names_adopt_activate_and_install(sim, tmp_path):
    d = drv(sim, tmp_path)
    v1, v2 = wl(sim, tmp_path, "1.0.0"), wl(sim, tmp_path, "2.0.0")
    assert d.plan(None, v1)[0].startswith("rauc install 1.0.0 into the inactive slot")
    d.apply(None, v1)
    d.commit("os")
    assert d.plan(None, v1) == ["1.0.0 already runs in slot rootfs.1: nothing to install"]
    d.apply(v1, v2)                          # 2.0.0 into A; 1.0.0 stays in B
    assert d.plan(v2, v1)[0] == "activate slot rootfs.1 (1.0.0, still installed: no transfer)"
    assert d.plan(v1, None) == ["stop managing the OS image (the booted slot keeps running)"]


def test_reboot_request_that_fails_keeps_the_booted_slot(sim, tmp_path):
    d = drv(sim, tmp_path, rebootCommand=["false"])
    with pytest.raises(DriverError, match="reboot request failed"):
        d.apply(None, wl(sim, tmp_path))
    st = sim.show()
    assert st["booted"] == "A" and d._load()["target"]["phase"] == "reboot-failed"
    assert d._status().primary == "rootfs.0"       # the switch to the new slot was taken back


def test_reboot_that_never_happens_times_out(sim, tmp_path):
    d = drv(sim, tmp_path, rebootCommand=["true"], bootTimeoutSeconds=0.5)
    with pytest.raises(DriverError, match=r"no reboot within 0.5 s"):
        d.apply(None, wl(sim, tmp_path))
    assert d._load()["target"]["phase"] == "reboot-timeout" and d._status().primary == "rootfs.0"

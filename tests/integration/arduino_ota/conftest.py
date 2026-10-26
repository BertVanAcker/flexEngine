"""Fixtures of the arduino-ota engine flow (EV-38): the reference fake cloud, a real engine process with the
arduino-ota driver, and a simulated flexNode-arduino board (flexengine2.testing.arduino_sim) behind
  * push tier:    the in-process MQTT broker + Redis (flexengine2.testing.miniwire),
  * nightly tier: eclipse-mosquitto + redis containers (`-m docker`)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from flexengine2.identity import crypto
from flexengine2.testing import arduino_sim as asim

from p2.p2lab import EngineProc, item  # noqa: E402  (P2 lab: engine process, plan items)
from rauc.rauclab import RaucCloud  # noqa: E402  (the fake cloud without the model registry)

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1] / "work" / "arduino-int"
DEP = "board-fw"
HEALTH = {"type": "arduino-ota", "periodSeconds": 0.2, "successThreshold": 3, "timeoutSeconds": 8}


@pytest.fixture(scope="session")
def keys():
    kd = WORK / "keys"
    return {"release": crypto.load_or_create(kd / "release.key"), "cloud": crypto.load_or_create(kd / "cloud.key")}


@pytest.fixture(params=[pytest.param("mini", id="mini-broker"),
                        pytest.param("real", id="mosquitto-redis", marks=[pytest.mark.docker, pytest.mark.slow])])
def lab_kind(request):
    return request.param


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m"):
        return
    for it in items:
        if "integration/arduino_ota/" in it.nodeid.replace("\\", "/") and "mosquitto-redis" in it.nodeid:
            it.add_marker(pytest.mark.skip(reason='nightly tier: select with -m "docker"'))


class BoardDevice:
    """One engine device (the LattePanda) with the arduino-ota driver and one simulated Portenta behind it."""

    def __init__(self, tmp: Path, cloud: RaucCloud, keys, lab: asim.ArduinoLab):
        self.tmp, self.cloud, self.keys, self.lab = tmp, cloud, keys, lab
        self.dev = f"panda-{uuid.uuid4().hex[:6]}"
        self.payloads = tmp / "payloads"
        drv = lab.driver_cfg(updateTimeoutSeconds=10, acceptTimeoutSeconds=4, pollSeconds=0.1)
        self.engine = EngineProc(tmp, cloud, keys, self.dev, poll=0.3,
                                 extra={"drivers": {"arduino-ota": drv, "native": False}, "logFormat": "text"})
        self.releases: dict[str, dict] = {}

    @property
    def sim(self) -> asim.BoardSim:
        return self.lab.sim()

    def release(self, version: str, **flags) -> dict:
        img = self.lab.image(self.payloads, version, **flags)
        self.releases[version] = asim.make_arduino_release(self.cloud.layout, self.keys["release"], img,
                                                           version=version, health=HEALTH)
        return self.releases[version]

    def deploy(self, version: str):
        return self.cloud.deploy(self.dev, [item("board_fw", version, "arduino-ota", DEP,
                                                 parameters={"board": "p1"})])

    def start(self) -> "BoardDevice":
        self.engine.start()
        return self

    def report(self) -> dict | None:
        return self.cloud.status(self.dev).get(DEP)

    def wait_state(self, state: str, version: str, timeout: float = 60) -> dict:
        def ok():
            r = self.report()
            return r if r and r["state"] == state and r.get("version") == version else None
        return self.cloud.wait(ok, timeout, what=f"{self.dev}: {state} {version} (last: {self.report()})")

    def actions(self) -> list[str]:
        return [a["action"] for a in self.engine.actions()]

    def stop(self):
        import signal
        self.engine.kill(signal.SIGKILL)


@pytest.fixture
def board_device(tmp_path, keys, lab_kind):
    if lab_kind == "real" and not asim.Containers.available():
        pytest.skip("docker not available")
    cloud = RaucCloud(tmp_path, keys)
    lab = asim.ArduinoLab(tmp_path / "lab", real=lab_kind == "real").start()
    d = BoardDevice(tmp_path, cloud, keys, lab)
    yield d
    d.stop()
    lab.stop()
    cloud.stop()


def wait(pred, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {what}")

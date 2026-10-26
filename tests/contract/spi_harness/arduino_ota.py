"""Harnesses: ArduinoOtaDriver (EV-38) against the flexNode-arduino board simulator (flexengine2.testing.arduino_sim),
which speaks the proposed remote-OTA protocol over
  * push tier:    the in-process MQTT broker and Redis of flexengine2.testing.miniwire (real TCP, real wire format),
  * nightly tier: eclipse-mosquitto 2.0.22 and redis 7.4 containers (marker docker).

The driver serves each image from its own HTTP server on 127.0.0.1; the simulator downloads it, checks size +
sha256 + the .ota header and restarts into the firmware described by the image.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from flexengine2.drivers.arduino_ota import ANN_BOARD, ANN_FIRMWARE, ArduinoOtaDriver
from flexengine2.drivers.base import Workload
from flexengine2.testing.arduino_sim import ArduinoLab, Containers

from .base import DEVICE, Harness, Release, deployment, digest_of, free_port

PROBE = {"type": "arduino-ota", "periodSeconds": 0.1, "successThreshold": 2, "timeoutSeconds": 10}


class _Base(Harness):
    driver_cls = ArduinoOtaDriver
    profile = "arduino-ota"
    real = False
    na = {"rollback_without_retransfer": "single application slot: the Portenta bootloader keeps no previous image, "
                                         "a rollback re-serves the previous release's .ota (one transfer)"}

    def setup(self):
        if self.real and not Containers.available():
            pytest.skip("docker not available")
        self.lab = ArduinoLab(self.tmp / "lab", real=self.real).start()
        self.payloads = self.tmp / "payloads"
        self.wls: dict = {}

    def make_driver(self):
        return ArduinoOtaDriver(DEVICE, self.data, self.lab.driver_cfg())

    def _wl(self, version: str, dep_id: str | None, **flags) -> Workload:
        key = (version, tuple(sorted(flags.items())))
        if key not in self.wls:
            self.wls[key] = self.lab.image(self.payloads, version, **flags)
        img = self.wls[key]
        return Workload(dep_id=dep_id or self.dep_id, module="board_fw", version=version,
                        digest=digest_of(img.read_bytes()), payload_path=str(img),
                        payload_annotations={"dev.flex.profile": "arduino-ota", ANN_BOARD: self.lab.fqbn,
                                             ANN_FIRMWARE: version},
                        health=dict(PROBE))

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9", dep_id, healthy=False)     # boots, reports ERROR

    def refused_release(self):
        img = self.lab.image(self.payloads, "6.6.6")       # an Opta release offered to a Portenta board
        rel = Release(str(img), {"dev.flex.profile": "arduino-ota", ANN_BOARD: "arduino:mbed_opta:opta",
                                 ANN_FIRMWARE: "6.6.6"}, {"health": PROBE})
        return rel, deployment(self.dep_id, "board_fw", "6.6.6", "arduino-ota")

    def transfers(self):
        return sum(s.downloads for s in self.lab.sims.values())

    def destroy_out_of_band(self, dep_id):
        self.lab.sim().reflash("0.0.9-usb")                 # another sketch uploaded over USB
        self.lab.sim().wait_ready()
        return True

    def cleanup(self):
        for d in self.drivers:
            d.close()
        self.lab.stop()

    @contextmanager
    def unreachable(self):
        d = ArduinoOtaDriver(DEVICE, self.data, self.lab.driver_cfg(mqtt_port=free_port(), redis_port=free_port()))
        try:
            yield d
        finally:
            d.close()


class SimArduinoHarness(_Base):
    id = "arduino-ota[sim]"


class SimArduinoContainersHarness(_Base):
    id = "arduino-ota[sim+mosquitto+redis]"
    real = True

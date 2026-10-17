"""os-rauc engine lab (EV-17): the reference fake cloud, real engine processes and a simulated A/B device.

The device is tests/contract/spi_harness's SimRauc (fake `rauc` + bootsim; push tier) or RaucLab (real rauc 1.11.3
in a privileged container; nightly) with autoBoot OFF: the driver's reboot hook only records the request, and the
test plays the machine: kill the engine (the reboot ends it), `boot()` (the bootloader picks a slot, counting
attempts and falling back), start the engine again on the "new slot" with the same data dir (the shared partition).
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

from flexengine2.artefacts.oci import Layout
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing import rauc as trauc

HERE = Path(__file__).resolve().parent
CONTRACT = HERE.parents[1] / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

from p2.p2lab import Cloud, EngineProc, item  # noqa: E402,F401  (P2 lab: cloud admin helpers, engine process)
from spi_harness.os_rauc import RaucLab, SimRauc  # noqa: E402,F401

DEP = "os"


class RaucCloud(Cloud):
    """p2lab's Cloud without the shared registry and the model: OS images are not flexModel modules."""

    def __init__(self, tmp: Path, keys):
        self.dir = tmp / "cloud"
        self.dir.mkdir(parents=True)
        self.layout = Layout(self.dir / "oci")          # the same layout the stub serves (read from disk)
        self.srv = CloudServer(self.dir, 0, "admin-secret", keys["cloud"]).start()
        self.url = self.srv.url
        self.st = self.srv.state
        self.registry = None

    def log(self, dev: str) -> list[dict]:
        return [r for r in self.st.s["statusLog"] if r["deviceId"] == dev]


class OsDevice:
    """One simulated A/B device with its engine process."""

    def __init__(self, tmp: Path, cloud: RaucCloud, keys, lab: SimRauc, dev: str, *, shared: Path | None = None,
                 driver_extra: dict | None = None):
        self.tmp, self.cloud, self.keys, self.lab, self.dev = tmp, cloud, keys, lab, dev
        self.payloads = tmp / "payloads"
        drv = lab.driver_cfg(shared or tmp, healthChecks=[lab.health_check_cmd()], bootTimeoutSeconds=120,
                             **(driver_extra or {}))
        self.engine = EngineProc(tmp, cloud, keys, dev, poll=0.3,
                                 extra={"drivers": {"os-rauc": drv, "native": False}, "logFormat": "text"})
        self.releases: dict[str, dict] = {}

    # -- releases --
    def release(self, version: str, *, bootable: bool = True, healthy: bool = True, health: dict | None = None):
        b = self.lab.bundle(self.payloads, version, bootable=bootable, healthy=healthy)
        self.releases[version] = trauc.make_rauc_release(
            self.cloud.layout, self.keys["release"], b, version=version,
            health=health or {"type": "os-rauc", "periodSeconds": 0.25, "successThreshold": 2, "timeoutSeconds": 6})
        return self.releases[version]

    def deploy(self, version: str):
        return self.cloud.deploy(self.dev, [item("os_image", version, "os-rauc", DEP)])

    # -- the machine --
    def start(self):
        self.engine.start()
        return self

    def wait_reboot_request(self, timeout: float = 60) -> None:
        self.cloud.wait(lambda: self.lab.show().get("rebootRequested", 0) > 0, timeout,
                        what=f"{self.dev}: reboot request")

    def reboot(self) -> dict:
        """The reboot: systemd stops the engine (here: SIGKILL, the harshest case), the bootloader boots."""
        self.engine.kill(signal.SIGKILL)
        st = self.lab.boot()
        self.engine.start()
        return st

    def power_cut(self) -> dict:
        self.engine.kill(signal.SIGKILL)
        self.lab.power_off()
        return self.lab.boot()

    def report(self) -> dict | None:
        return self.cloud.status(self.dev).get(DEP)

    def wait_state(self, state: str, version: str, timeout: float = 90) -> dict:
        def ok():
            r = self.report()
            return r if r and r["state"] == state and r.get("version") == version else None
        return self.cloud.wait(ok, timeout, what=f"{self.dev}: {state} {version} (last: {self.report()})")

    def engine_state(self) -> dict:
        return self.engine.state()

    def actions(self) -> list[str]:
        return [a["action"] for a in self.engine.actions()]

    def driver_state(self) -> dict:
        p = self.engine.dir / "data" / "os-rauc" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def stop(self):
        self.engine.kill(signal.SIGKILL)
        self.lab.stop()

    def install_and_reboot(self, version: str, timeout: float = 90) -> dict:
        """Deploy `version`, play the reboot, wait for Installed (the happy path)."""
        self.deploy(version)
        self.wait_reboot_request(timeout)
        self.reboot()
        return self.wait_state("Installed", version, timeout)


def wait(pred, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {what}")

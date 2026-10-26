"""P3 lab on flexEngineV2 (port of probes/p3-mcu-hotload/tests/conftest.py).

Reference cloud fake (in process) + OCI registry with MCU releases + QEMU mps2/an385 MCU (MCUboot + base image from
the P3 build, read-only) + the engine under test: ONE `flexengine run` host process with the gateway device `gw1` and
its see-thru MCU child `gw1.mcu0` (mcu-module + mcu-firmware drivers over SMP). The scenario tests share one lab and
run in file order (a -> h), like a rollout history on one device.

Stand-ins imported read-only from the probe: `flexp3.qemu` (QEMU control, power cut = kill) and, as the independent
test oracle, `flexp3.smp_link` / `flexp3.flxm`. The engine never sees them (checked by the `lab` fixture).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

from flexengine2.testing.release import PROBES

FLEXENGINE = Path(__file__).resolve().parents[3]
PROBE = PROBES / "p3-mcu-hotload"
B = PROBE / "build"
WORK = FLEXENGINE / "tests" / "work" / "mcu" / "lab"
RESULTS = Path(os.environ.get("FLEX_RESULTS_DIR") or FLEXENGINE / "results" / "latest")   # per run, gitignored
METRICS: dict = {}
GW, CHILD = "gw1", "gw1.mcu0"

import pytest  # noqa: E402

if not (PROBE / "flexp3").is_dir():   # stand-ins from the framework (probes, demo/), not vendored: they import flexmodel
    pytest.skip("needs a modular-interoperable-framework checkout with probe P3 (MCU): set FLEX_REPO",
                allow_module_level=True)
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))  # test stand-ins only (QEMU control, SMP oracle)

from flexp3 import flxm as p3flxm  # noqa: E402
from flexp3.qemu import PART, Mcu, provision  # noqa: E402
from flexp3.smp_link import SmpLink  # noqa: E402

from flexengine2.artefacts.oci import Layout  # noqa: E402
from flexengine2.identity import crypto  # noqa: E402
from flexengine2.testing.edge_lab import HostProc, admin  # noqa: E402
from flexengine2.testing.fakecloud import CloudServer  # noqa: E402
from flexengine2.testing.mcu_lab import (gen_meta, make_firmware_release, make_module_release,  # noqa: E402
                                         module_health, region_sha, tamper_payload)
from flexengine2.util import read_json  # noqa: E402

REQUIRED = ("mcuboot/zephyr/zephyr.bin", "base-1.0.0.signed.bin", "base-1.1.0.signed.bin", "base-1.2.0.signed.bin",
            "modules/servo_drive-1.0.0.flxm", "modules/gripper_station-1.0.0.flxm",
            "gen/servo_drive/gen/module.json", "mcuboot.build.log", "base-1.1.0.build.log")
MODULE_KEY = PROBE / "work" / "keys" / "module-ec-p256.pem"   # the key the base image trusts (baked in)


def metric(name: str, value):
    METRICS[name] = value


def missing_build() -> str | None:
    if not shutil.which("qemu-system-arm"):
        return "qemu-system-arm not installed"
    for p in REQUIRED:
        if not (B / p).exists():
            return f"probes/p3-mcu-hotload/build/{p} missing: run probes/p3-mcu-hotload/tools/build_all.sh"
    if not MODULE_KEY.exists():
        return f"{MODULE_KEY} missing (P3 module signing key)"
    return None


class Lab:
    def __init__(self):
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True)
        kd = WORK.parent / "keys"
        self.keys = {"release": crypto.load_or_create(kd / "release-ed25519.key"),
                     "cloud": crypto.load_or_create(kd / "cloud-ed25519.key"),
                     "rogue": crypto.load_or_create(kd / "rogue-ed25519.key")}
        self.module_key = p3flxm.load_private(MODULE_KEY)
        # ---- registry -------------------------------------------------------------------------
        self.layout = Layout(WORK / "cloud" / "oci")
        servo, servo_chg, grip = (gen_meta(B / "gen" / n) for n in ("servo_drive", "servo_drive_changed", "gripper_station"))
        self.iface = {"servo_drive": servo["interfaceHash"], "gripper_station": grip["interfaceHash"]}
        M = B / "modules"
        rk = self.keys["release"]
        self.rel = {}
        for f, meta, ver, kw in [
            ("servo_drive-1.0.0", servo, "1.0.0", {}), ("servo_drive-2.0.0", servo, "2.0.0", {}),
            ("servo_drive-3.0.0", servo, "3.0.0", {}),
            ("servo_drive-abi2", servo, "4.0.0-abi2", {}), ("servo_drive-m33hard", servo, "4.0.0-m33", {}),
            ("servo_drive-iface", servo_chg, "5.0.0-iface", {}),
            ("servo_drive-rogue", servo, "6.0.0-rogue", {}),        # OCI-signed, FLXM signed by a rogue key
            ("servo_drive-unsigned", servo, "6.0.1-unsigned", {}),  # OCI-signed, FLXM unsigned
            ("servo_drive-2.0.0", servo, "6.0.2-nosig", {"sign": False}),  # no OCI release signature
            ("servo_drive-tamper", servo, "6.0.3-tamper", {}),       # payload blob tampered after signing
            ("gripper_station-1.0.0", grip, "1.0.0", {}),
        ]:
            self.rel[(meta["module"], ver)] = make_module_release(self.layout, rk, M / f"{f}.flxm", meta, ver, **kw)
        for v in ("1.0.0", "1.1.0", "1.2.0"):
            self.rel[("flexmcu_base", v)] = make_firmware_release(self.layout, rk, B / f"base-{v}.signed.bin", v)
        self.tampered = tamper_payload(self.layout, self.rel[("servo_drive", "6.0.3-tamper")]["digest"])
        # ---- cloud --------------------------------------------------------------------------------
        self.cloud = CloudServer(WORK / "cloud", 0, "admin-secret", self.keys["cloud"]).start()
        # ---- MCU (factory programming: MCUboot + base 1.0.0) ----------------------------------------
        self.flash = WORK / "mcu" / "flash.img"
        self.flash.parent.mkdir(parents=True)
        provision(self.flash, B / "mcuboot" / "zephyr" / "zephyr.bin", B / "base-1.0.0.signed.bin")
        self.mcu = Mcu(WORK / "mcu", self.flash)
        self.boot_s = self.mcu.start()
        # ---- engine under test: one flexengine host = gateway + see-thru MCU child -------------------------------
        pub = p3flxm.raw_public(self.module_key).hex()
        self.gw_cfg = {
            "cloud": {"apiUrl": self.cloud.url, "cloudPublicKey": crypto.public_b64(self.keys["cloud"]),
                      "releasePublicKeys": [crypto.public_b64(self.keys["release"])], "pollInterval": 0.3,
                      "backoff": [0.2, 1.0], "httpTimeout": 5},
            "dataDir": str(WORK / "gw"),
            "devices": [
                {"deviceId": GW, "role": "gateway", "dataDir": str(WORK / "gw" / "data-gw"),
                 "onboardingToken": self.token(GW), "drivers": {"native": False}},
                {"deviceId": CHILD, "parent": GW, "dataDir": str(WORK / "gw" / "data-mcu0"),
                 "onboardingToken": self.token(CHILD), "link": "smp/uart (QEMU tcp)",
                 "mcu": {"smp": {"host": "127.0.0.1", "port": self.mcu.port}, "modulePublicKey": pub}}]}
        (WORK / "gw").mkdir(exist_ok=True)
        self.gw_cfg_path = WORK / "gw" / "engine.yaml"
        self.gw_cfg_path.write_text(yaml.safe_dump(self.gw_cfg, sort_keys=False))
        self.gw = HostProc(self.gw_cfg_path, WORK / "gw" / "gateway.log")
        self.start_gateway()

    # ---- processes ------------------------------------------------------------------------------
    def start_gateway(self):
        self.gw.start()

    def stop_gateway(self):
        self.gw.stop()

    def close(self):
        self.stop_gateway()
        self.mcu.stop()
        self.cloud.stop()

    # ---- cloud admin ------------------------------------------------------------------------------
    def admin(self, method: str, path: str, body=None, user: str = "carol"):
        return admin(self.cloud.port, method, path, body, user)

    def token(self, dev: str) -> str:
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def deploy(self, items: list[dict], dev: str = CHILD) -> float:
        self.wait(lambda: dev in self.device_list(), 30, what=f"{dev} onboarded")
        s, p = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items})
        assert s == 201, p
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        return time.time()

    def device_list(self) -> list[str]:
        s, b = self.admin("GET", "/api/v1/admin/devices")
        return [d["deviceId"] for d in b["devices"]]

    def device(self, dev: str = CHILD) -> dict:
        return self.admin("GET", f"/api/v1/admin/devices/{dev}")[1]

    def status(self, dev: str = CHILD) -> dict:
        return self.device(dev)["status"]

    def wait(self, pred, timeout: float = 60, period: float = 0.1, what: str = ""):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(period)
        raise AssertionError(f"timeout waiting for {what or pred}\n--- gateway log tail ---\n" + self.gw.log()[-3000:])

    def wait_state(self, dep: str, state: str, version: str | None = None, timeout: float = 90, after: float = 0):
        def ok():
            r = self.status().get(dep)
            if r and r["state"] == state and (version is None or r.get("version") == version) \
                    and r.get("receivedAt", 0) >= after:
                return r
            return None
        return self.wait(ok, timeout, what=f"{dep} -> {state} {version or ''}")

    # ---- direct device access (test oracle, bypasses the gateway) ----------------------------------
    def smp(self) -> SmpLink:
        return SmpLink("127.0.0.1", self.mcu.port, timeout=30).connect()

    def dev_status(self) -> dict:
        l = self.smp()
        try:
            return l.flexmod("status")
        finally:
            l.close()

    def dev_info(self) -> dict:
        l = self.smp()
        try:
            return l.flexmod("info")
        finally:
            l.close()

    def slot0_sha(self) -> str:
        return region_sha(self.flash, PART["slot0"], 0x60000)

    def actions(self) -> list[dict]:
        p = WORK / "gw" / "data-mcu0" / "actions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def release_health(self, module: str, version: str) -> dict:
        """Health gate of a module release, as published (timeout = the module's confirm window)."""
        name = {"3.0.0": "servo_drive-3.0.0", "2.0.0": "servo_drive-2.0.0", "1.0.0": f"{module}-1.0.0"}[version]
        return module_health(p3flxm.parse((B / "modules" / f"{name}.flxm").read_bytes()).header.test_window_ms)

    def driver_metrics(self) -> dict:
        return read_json(WORK / "gw" / "data-mcu0" / "mcu-module-driver.json", {}).get("metrics", {})

    def fw_metrics(self) -> dict:
        return read_json(WORK / "gw" / "data-mcu0" / "mcu-firmware-driver.json", {}).get("metrics", {})


def item(module: str, version: str, dep_id: str, driver: str = "mcu-module", **params) -> dict:
    return {"id": dep_id, "module": module, "version": version, "driver": driver, "parameters": params}

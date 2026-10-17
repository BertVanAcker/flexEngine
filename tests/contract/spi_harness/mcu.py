"""Harnesses: McuModuleDriver / McuFirmwareDriver over SMP -- a fake base image (push) or QEMU mps2-an385 (nightly).

Push tier: spi_harness/fakes/fakesmp.py; the modules are FLXM containers built here (a minimal ELF, signed with a
key generated per test) and the firmware images minimal MCUboot-shaped blobs.
Nightly tier: the P3 lab's build outputs (probes/p3-mcu-hotload/build, read-only): MCUboot + base 1.0.0 provisioned
into a fresh flash file, modules servo_drive 1.0.0 / 2.0.0 (healthy) and 3.0.0 (stalls -> device fail-fast revert),
base images 1.1.0 (healthy) and 1.2.0 (FAULT build); module key probes/p3-mcu-hotload/work/keys/module-ec-p256.pem.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import struct
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from flexengine2.drivers.base import Workload
from flexengine2.drivers.mcu import flxm
from flexengine2.drivers.mcu.driver import LinkPool, McuFirmwareDriver, McuModuleDriver
from flexengine2.drivers.mcu.link import SmpLink

from .base import Harness, Release, deployment, digest_of, free_port

REPO = Path(__file__).resolve().parents[4]
P3 = REPO / "probes" / "p3-mcu-hotload"
P3B = P3 / "build"
CHILD = "ct1.mcu0"
MOD_HEALTH = {"type": "mcu-module", "periodSeconds": 0.1, "successThreshold": 3, "timeoutSeconds": 6}
FW_HEALTH = {"type": "mcu-base", "periodSeconds": 0.2, "successThreshold": 3, "timeoutSeconds": 6}


def minimal_elf() -> bytes:
    """ELF32 little-endian ET_REL for ARM without sections: enough for pyelftools (no imports)."""
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + b"\0" * 8
    return ident + struct.pack("<HHIIIIIHHHHHH", 1, 40, 1, 0, 0, 0, 0x05000000, 52, 0, 0, 40, 0, 0)


def make_flxm(key, name: str, version: str, sign: bool = True) -> bytes:
    h = flxm.Header(abi_major=1, abi_minor=0, float_abi=flxm.FLOAT_ABI["soft"], arch=flxm.ARCH["armv7m"],
                    caps_required=flxm.caps_mask(["pwm"]), elf_size=0, ram_estimate=1024, step_period_ms=5,
                    name=name, version=version, iface_hash=b"\x11" * 32, elf_sha256=b"\0" * 32)
    return flxm.pack(h, minimal_elf() + version.encode(), key if sign else None)


def load_p3_qemu():
    spec = importlib.util.spec_from_file_location("flexp3_qemu_ro", P3 / "flexp3" / "qemu.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _McuBase(Harness):
    """Common parts; `self.port` is the SMP TCP port of the (fake or QEMU) MCU."""

    def setup(self):
        self.n_up = 0
        real_flexmod, real_img = SmpLink.flexmod, SmpLink.image_upload
        h = self

        def flexmod(link, cmd, timeout=None, **body):  # instrumentation only: count transfers
            if cmd == "upload" and body.get("off") == 0:
                h.n_up += 1
            return real_flexmod(link, cmd, timeout=timeout, **body)

        def image_upload(link, data, *a, **kw):
            h.n_up += 1
            return real_img(link, data, *a, **kw)
        self.mp.setattr(SmpLink, "flexmod", flexmod)
        self.mp.setattr(SmpLink, "image_upload", image_upload)

    def transfers(self):
        return self.n_up

    def links(self, port: int | None = None) -> LinkPool:
        return LinkPool("127.0.0.1", port or self.port, timeout=5.0)


class _ModuleMixin:
    profile = "mcu-module"
    driver_cls = McuModuleDriver

    def make_driver(self):
        return McuModuleDriver(self.links(), self.data, self.pub_raw, CHILD)

    def _wl(self, path: Path, version: str, dep_id, module="ct_mod") -> Workload:
        return Workload(dep_id=dep_id or self.dep_id, module=module, version=version,
                        digest=digest_of(path.read_bytes() + version.encode()), payload_path=str(path),
                        payload_annotations={"dev.flex.profile": "mcu-module", "dev.flex.arch": "armv7m",
                                             "dev.flex.floatAbi": "soft", "dev.flex.moduleAbi": "1.0"},
                        health=dict(MOD_HEALTH), parameters={"gain": 2})

    def instances(self, dep_id):
        st = SmpLink("127.0.0.1", self.port).connect(wait=5)
        try:
            s = st.flexmod("status")
        finally:
            st.close()
        return sum(1 for x in s["sockets"] if x["dep"] == dep_id and x["state"] != "empty")

    def destroy_out_of_band(self, dep_id):
        link = SmpLink("127.0.0.1", self.port).connect(wait=5)
        try:
            for s in link.flexmod("status")["sockets"]:
                if s["dep"] == dep_id and s["state"] != "empty":
                    link.flexmod("remove", sock=s["sock"])
        finally:
            link.close()
        return True

    @contextmanager
    def unreachable(self):
        yield McuModuleDriver(self.links(free_port()), self.data, self.pub_raw, CHILD)


class _FirmwareMixin:
    profile = "mcu-firmware"
    driver_cls = McuFirmwareDriver
    dep_id = "base-image"

    def make_driver(self):
        return McuFirmwareDriver(self.links(), self.data, CHILD)

    def _wl(self, path: Path, version: str, dep_id) -> Workload:
        return Workload(dep_id=dep_id or self.dep_id, module="flexmcu_base", version=version,
                        digest=digest_of(path.read_bytes()), payload_path=str(path),
                        payload_annotations={"dev.flex.profile": "mcu-firmware"}, health=dict(FW_HEALTH))

    def refused_release(self):
        p = self.tmp / "payloads" / "not-an-image.bin"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"\0" * 64)
        return Release(str(p), {"dev.flex.profile": "mcu-firmware"}, {}), deployment(self.dep_id, "flexmcu_base",
                                                                                     "0.0.1", "mcu-firmware")

    @contextmanager
    def unreachable(self):
        yield McuFirmwareDriver(self.links(free_port()), self.data, CHILD)


# ---- push tier: fake base image -------------------------------------------------------------------------------
class _FakeMcuBase(_McuBase):
    def setup(self):
        super().setup()
        from .fakes.fakesmp import FakeMcu
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub_raw = flxm.raw_public(self.key)
        self.mcu = FakeMcu(self.pub_raw, base_version="1.0.0", fault_after=self.fail_delay or 1.0).start()
        self.port = self.mcu.port

    def cleanup(self):
        self.mcu.stop()

    def break_commit(self):
        self.mcu.fail_confirm = True
        return True


class FakeMcuModuleHarness(_ModuleMixin, _FakeMcuBase):
    id = "mcu-module[fake-smp]"
    fail_delay = 1.0
    xfail = {"idempotent_apply": "bug: McuModuleDriver.apply() of the module already under test reverts it and "
                                 "uploads it again (state TESTING, no committed slot)"}

    def _mod(self, name: str, version: str, sign: bool = True) -> Path:
        p = self.tmp / "payloads" / f"{name}-{version}.flxm"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(make_flxm(self.key, name, version, sign))
        return p

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(self._mod("ct_mod", version), version, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl(self._mod("ct_mod_bad", "9.9.9"), "9.9.9-bad", dep_id)

    def refused_release(self):
        p = self._mod("ct_mod", "6.0.1-unsigned", sign=False)
        wl = self._wl(p, "6.0.1-unsigned", None)
        return Release(str(p), wl.payload_annotations, {}), deployment(self.dep_id, "ct_mod", "6.0.1", "mcu-module")


class FakeMcuFirmwareHarness(_FirmwareMixin, _FakeMcuBase):
    id = "mcu-firmware[fake-smp]"
    fail_delay = None
    xfail = {"fail_fast": "EV-20: McuFirmwareDriver.failed() is not implemented (a FAULT build only times out)"}

    def setup(self):
        super().setup()
        # a resident, committed module: a FAULT build (module runner disabled) makes it unhealthy, like P3's 1.2.0
        link = SmpLink("127.0.0.1", self.port).connect(wait=5)
        try:
            data = make_flxm(self.key, "resident", "1.0.0")
            import hashlib
            off = 0
            while off < len(data):
                kw = {"len": len(data), "sha": hashlib.sha256(data).digest()} if off == 0 else {}
                r = link.flexmod("upload", sock=1, off=off, data=data[off:off + 768], **kw)
                off = r["off"]
            link.flexmod("activate", sock=1, slot=0, dep="resident", params="", test=False)
        finally:
            link.close()

    def _img(self, version: str, build: int = 0) -> Path:
        from .fakes.fakesmp import mcuboot_image
        p = self.tmp / "payloads" / f"base-{version}-{build}.signed.bin"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(mcuboot_image(version, b"flexmcu base " + version.encode(), build))
        return p

    def workload(self, version="1.0.0", dep_id=None):
        v = {"1.0.0": "1.1.0", "2.0.0": "1.3.0"}.get(version, version)  # 1.0.0 is the factory image
        return self._wl(self._img(v), v, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl(self._img("1.9.0", build=99), "1.9.0", dep_id)


# ---- nightly tier: QEMU ---------------------------------------------------------------------------------------
def qemu_ready() -> str | None:
    if not shutil.which("qemu-system-arm"):
        return "qemu-system-arm not installed"
    for f in (P3B / "mcuboot" / "zephyr" / "zephyr.bin", P3B / "base-1.0.0.signed.bin",
              P3 / "work" / "keys" / "module-ec-p256.pem", P3B / "modules" / "servo_drive-1.0.0.flxm"):
        if not f.exists():
            return f"P3 build output missing: {f} (run probes/p3-mcu-hotload/run.sh build)"
    return None


class _QemuBase(_McuBase):
    def setup(self):
        why = qemu_ready()
        if why:
            pytest.skip(why)
        super().setup()
        q = load_p3_qemu()
        mdir = self.tmp / "mcu"
        mdir.mkdir()
        flash = mdir / "flash.img"
        q.provision(flash, P3B / "mcuboot" / "zephyr" / "zephyr.bin", P3B / "base-1.0.0.signed.bin")
        self.mcu = q.Mcu(mdir, flash)
        self.mcu.start(timeout=60)
        self.port = self.mcu.port
        self.pub_raw = flxm.raw_public(flxm.load_private(P3 / "work" / "keys" / "module-ec-p256.pem"))

    def cleanup(self):
        self.mcu.stop()


class QemuMcuModuleHarness(_ModuleMixin, _QemuBase):
    id = "mcu-module[qemu]"
    fail_delay = None
    xfail = FakeMcuModuleHarness.xfail

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(P3B / "modules" / f"servo_drive-{version}.flxm", version, dep_id, module="servo_drive")

    def bad_workload(self, dep_id=None):
        return self._wl(P3B / "modules" / "servo_drive-3.0.0.flxm", "3.0.0", dep_id, module="servo_drive")

    def refused_release(self):
        p = P3B / "modules" / "servo_drive-unsigned.flxm"
        wl = self._wl(p, "6.0.1-unsigned", None, module="servo_drive")
        return Release(str(p), wl.payload_annotations, {}), deployment(self.dep_id, "servo_drive", "6.0.1", "mcu-module")


class QemuMcuFirmwareHarness(_FirmwareMixin, _QemuBase):
    id = "mcu-firmware[qemu]"
    fail_delay = None
    xfail = FakeMcuFirmwareHarness.xfail

    def setup(self):
        super().setup()
        # a resident committed module (servo 1.0.0): the 1.2.0 FAULT build disables the module runner
        drv = McuModuleDriver(self.links(), self.tmp / "resident-data", self.pub_raw, CHILD)
        wl = Workload(dep_id="resident", module="servo_drive", version="1.0.0", digest="sha256:" + "0" * 64,
                      payload_path=str(P3B / "modules" / "servo_drive-1.0.0.flxm"), payload_annotations={})
        drv.apply(None, wl)
        drv.commit("resident")

    def workload(self, version="1.0.0", dep_id=None):
        v = {"1.0.0": "1.1.0", "2.0.0": "1.0.0"}.get(version, version)  # 1.0.0 = factory image
        return self._wl(P3B / f"base-{v}.signed.bin", v, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl(P3B / "base-1.2.0.signed.bin", "1.2.0", dep_id)


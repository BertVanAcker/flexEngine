"""flexEngine drivers for an MCU behind a gateway (P2 driver SPI), over SMP.

  McuModuleDriver   profile "mcu-module":   LLEXT modules in A/B socket slots (flexmod SMP group 64)
  McuFirmwareDriver profile "mcu-firmware": the base image itself via MCUboot (img_mgmt + os reset)

SPI additions used by the P3 engine (proposed for flexEngine, see the P3 report):
  preflight(release, deployment) -> reason | None   checks before anything is sent to the device
  commit(dep_id)                                     two-phase executors confirm after the health gate

Both drivers rebuild their view from the device (status / image state) so an engine crash or an MCU
power cycle converges without duplicate uploads.
"""

from __future__ import annotations

import hashlib
import json
import time
import zlib
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
import io

from flexengine2.util import atomic_write_json, read_json
from flexengine2.drivers.base import Driver, DriverError, Observed, Workload

from . import flxm
from .link import SmpError, SmpLink

IMPORT_ALLOW = {"memcpy", "memset", "memcmp", "strlen"}
CONFIRM_GRACE_MS = 3000   # device TESTING deadline = module confirm window + this (time for the confirm RPC)


class LinkPool:
    """SMP link to one MCU, shared by the module and firmware drivers. The pipe is opened per call (or per
    `session()`), so other SMP clients -- a field tool, the test oracle -- can take turns on the single UART;
    connection errors close the link and are retried once."""

    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.link: SmpLink | None = None
        self.hold = 0

    def get(self) -> SmpLink:
        if self.link is None:
            self.link = SmpLink(self.host, self.port, timeout=self.timeout).connect(wait=2.0)
        return self.link

    def _drop(self):
        if self.link:
            self.link.close()
        self.link = None

    def session(self):
        pool = self

        class _S:
            def __enter__(self):
                pool.hold += 1
                return pool

            def __exit__(self, *exc):
                pool.hold -= 1
                if pool.hold == 0:
                    pool._drop()
        return _S()

    def call(self, fn, retries: int = 1):
        try:
            for attempt in range(retries + 1):
                try:
                    return fn(self.get())
                except SmpError as e:
                    raise DriverError(str(e)) from e
                except (OSError, TimeoutError, ConnectionError) as e:
                    self._drop()
                    if attempt == retries:
                        raise DriverError(f"MCU unreachable: {e}") from e
                    time.sleep(0.2)
        finally:
            if self.hold == 0:
                self._drop()

    def wait_up(self, timeout: float = 30.0) -> dict:
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                return self.call(lambda l: l.flexmod("info", timeout=2), retries=0)
            except (DriverError, SmpError) as e:
                last = e
                time.sleep(0.2)
        raise DriverError(f"MCU did not come back: {last}")


def elf_imports(elf: bytes) -> set[str]:
    e = ELFFile(io.BytesIO(elf))
    out = set()
    for s in e.iter_sections():
        if isinstance(s, SymbolTableSection):
            for sym in s.iter_symbols():
                if sym["st_shndx"] == "SHN_UNDEF" and sym.name and sym["st_info"]["bind"] != "STB_LOCAL":
                    out.add(sym.name)
    return out


class McuModuleDriver(Driver):
    profile = "mcu-module"
    name = "mcumgr-llext"

    def __init__(self, links: LinkPool, data_dir: Path, module_pubkey_raw: bytes, child_id: str):
        self.links = links
        self.child = child_id
        self.path = Path(data_dir) / "mcu-module-driver.json"
        self.st = read_json(self.path, {"socks": {}, "shas": {}, "metrics": {}})
        self.pub = flxm.public_from_raw(module_pubkey_raw)
        self.last_hb: dict[str, int] = {}
        self._info: dict | None = None
        self.fault_base: dict[str, int] = {}     # fail-fast: device fault-revert counter at activation
        self.fault_at: dict[str, float] = {}      # host time at which the device saw the fault (M2)
        self._status_cache: tuple[float, dict] | None = None

    def _save(self):
        atomic_write_json(self.path, self.st)

    def info(self, refresh: bool = False) -> dict:
        if self._info is None or refresh:
            self._info = self.links.call(lambda l: l.flexmod("info"))
        return self._info

    def device_status(self) -> dict:
        st = self.links.call(lambda l: l.flexmod("status"))
        self._status_cache = (time.time(), st)
        return st

    def _sock(self, dep_id: str, status: dict | None = None, allocate: bool = False) -> int | None:
        status = status or self.device_status()
        for s in status["sockets"]:
            if s["dep"] == dep_id and s["state"] != "empty":
                self.st["socks"][dep_id] = s["sock"]
                return s["sock"]
        if dep_id in self.st["socks"]:
            return self.st["socks"][dep_id]
        if not allocate:
            return None
        used = set(self.st["socks"].values()) | {s["sock"] for s in status["sockets"] if s["state"] != "empty"}
        for s in status["sockets"]:
            if s["sock"] not in used:
                self.st["socks"][dep_id] = s["sock"]
                self._save()
                return s["sock"]
        raise DriverError(f"no free module socket on {self.child} ({len(status['sockets'])} sockets)")

    def platform(self) -> dict:
        """Layer selection for multi-arch module releases (D-PICK: one FLXM per MCU profile in one release)."""
        info = self.info()
        return {"dev.flex.arch": info["arch"], "dev.flex.floatAbi": info["float_abi"]}

    # ---- checks before anything is sent ---------------------------------------------------------
    def preflight(self, rel, dep: dict) -> str | None:
        try:
            mod = flxm.parse(Path(rel.payload_path).read_bytes())
        except ValueError as e:
            return f"not an MCU module: {e}"
        h = mod.header
        ok, why = flxm.verify(mod, self.pub)
        if not ok:
            return f"module container: {why}"
        info = self.info()
        if h.abi_major != info["abi_major"] or h.abi_minor > info["abi_minor"]:
            return (f"module ABI {h.abi_major}.{h.abi_minor} incompatible with base ABI "
                    f"{info['abi_major']}.{info['abi_minor']} on {self.child}")
        arch = {v: k for k, v in flxm.ARCH.items()}.get(h.arch, "?")
        fabi = {v: k for k, v in flxm.FLOAT_ABI.items()}.get(h.float_abi, "?")
        if arch != info["arch"] or fabi != info["float_abi"]:
            return f"module built for {arch}/{fabi}-float, {self.child} is {info['arch']}/{info['float_abi']}-float"
        missing = set(flxm.caps_names(h.caps_required)) - set(flxm.caps_names(info["caps"]))
        if missing:
            return f"module needs capabilities {sorted(missing)} that {self.child} does not offer"
        desc_hash = rel.descriptor.get("interfaceHash")
        if desc_hash and desc_hash != h.iface:
            return f"interface hash in module ({h.iface[:20]}…) differs from its release descriptor ({desc_hash[:20]}…)"
        expect = (dep["spec"].get("parameters") or {}).get("_interfaceHash")
        if expect and expect != h.iface:
            return f"interface hash mismatch: deployment expects {expect[:20]}…, module has {h.iface[:20]}…"
        bad = elf_imports(mod.elf) - IMPORT_ALLOW
        if bad:
            return f"module imports {sorted(bad)} outside the flex API allow-list"
        if len(mod.raw) > info["slot_size"]:
            return f"module of {len(mod.raw)} B exceeds the {info['slot_size']} B slot"
        if h.ram_estimate > info["llext_heap"] // 2:
            return f"module needs ~{h.ram_estimate} B of LLEXT heap; {info['llext_heap']} B heap must hold two modules"
        return None

    # ---- SPI ----------------------------------------------------------------------------------------
    def plan(self, current, desired):
        if desired is None:
            return [f"remove module socket of {current.dep_id} on {self.child}"]
        size = Path(desired.payload_path).stat().st_size
        return [f"preflight: signature, ABI, float ABI/arch, capabilities, interface hash, imports",
                f"SMP upload {desired.module} {desired.version} ({size} B) to the inactive slot of a socket on {self.child}",
                "activate (test): verify on device, llext_load, make-before-break switch",
                f"health gate: heartbeat advancing ({desired.health.get('successThreshold', 3)} probes)",
                "confirm (commit) or revert to the previous slot without re-upload"]

    @staticmethod
    def _params(wl: Workload) -> str:
        return ";".join(f"{k}={v}" for k, v in sorted(wl.parameters.items()) if not k.startswith("_"))

    def apply(self, current, desired):
        with self.links.session():
            self._apply(current, desired)

    def _apply(self, current, desired):
        mod = flxm.parse(Path(desired.payload_path).read_bytes())
        sha = mod.header.elf_sha256.hex()
        self.st["shas"][sha] = {"digest": desired.digest, "version": desired.version}
        status = self.device_status()
        sock = self._sock(desired.dep_id, status, allocate=True)
        self._save()
        s = status["sockets"][sock]
        slot_sha = [sl.get("sha") if sl.get("valid") else None for sl in s["slots"]]
        m = {"t0": time.time(), "upload_s": 0.0, "uploaded_bytes": 0, "path": ""}
        L = self.links
        params = self._params(desired)
        if s["state"] == "testing" and s["active"] >= 0 and slot_sha[s["active"]] == sha and \
                s.get("dep") == desired.dep_id:
            # the desired module is already the one under test (idempotent apply, e.g. an engine crash between
            # apply and commit): keep the test running -- never revert and re-upload it. Only new parameters
            # need a device action, and the device accepts re-activating the slot under test.
            if s.get("params_crc") == zlib.crc32(params.encode()):
                m["path"] = "already under test (no device action)"
                self._finish(desired, sock, {"rc": 0, "state": "testing"}, m)
                return
            self.fault_base.setdefault(desired.dep_id, int(s.get("fault_reverts", 0) or 0))
            win = {"win": mod.header.test_window_ms + CONFIRM_GRACE_MS} if mod.header.test_window_ms else {}
            t = time.time()
            r = L.call(lambda l: l.flexmod("activate", sock=sock, slot=s["active"], dep=desired.dep_id,
                                           params=params, test=True, timeout=30, **win), retries=0)
            m.update(path="re-activate slot under test (parameters)", rpc_ms=round((time.time() - t) * 1000, 1))
            self._finish(desired, sock, r, m)
            return
        if s["state"] == "testing":
            if s["confirmed"] >= 0 and slot_sha[s["confirmed"]] == sha:  # rollback target = committed slot
                self.fault_base.pop(desired.dep_id, None)
                t = time.time()
                r = L.call(lambda l: l.flexmod("revert", sock=sock))
                m.update(path="revert (no upload)", rpc_ms=round((time.time() - t) * 1000, 1))
                self._finish(desired, sock, r, m)
                return
            L.call(lambda l: l.flexmod("revert", sock=sock))  # leave TESTING before anything else
            status = self.device_status()
            s = status["sockets"][sock]
        active = s["active"]
        if active >= 0 and slot_sha[active] == sha and s["state"] == "confirmed" and \
                s.get("params_crc") == zlib.crc32(params.encode()) and s.get("dep") == desired.dep_id:
            # already running with these parameters, e.g. the base image reverted to it on a fault (fail-fast):
            # nothing to do on the device, the health gate decides
            self.fault_base.pop(desired.dep_id, None)
            m["path"] = "already active (no device action)"
            self._finish(desired, sock, {"rc": 0, "state": "confirmed"}, m)
            return
        if active >= 0 and slot_sha[active] == sha and s["state"] == "confirmed":
            slot, m["path"] = active, "re-activate same slot (parameters)"
        elif active >= 0 and slot_sha[1 - active] == sha:
            slot, m["path"] = 1 - active, "activate other slot (no upload)"
        else:
            data = mod.raw
            t = time.time()
            off = 0
            while off < len(data):
                kw = {"len": len(data), "sha": hashlib.sha256(data).digest()} if off == 0 else {}
                part = data[off:off + 768]
                tc = time.time()
                for attempt in range(4):  # a lost UART line loses the frame: resend (the device checks off)
                    try:
                        r = L.call(lambda l: l.flexmod("upload", sock=sock, off=off, data=part,
                                                       timeout=5 if attempt < 3 else 30, **kw), retries=0)
                        break
                    except DriverError as e:
                        m["upload_retries"] = m.get("upload_retries", 0) + 1
                        if attempt == 3:
                            raise DriverError(f"upload failed at offset {off}/{len(data)} after "
                                              f"{time.time() - tc:.1f}s: {e}") from e
                if r.get("rc", 0) != 0 and r.get("off") is not None and r["off"] != off and off != 0:
                    off = r["off"]  # device tells where to resume
                    continue
                if r.get("rc", 0) != 0:
                    raise DriverError(f"device refused the upload: {r.get('reason')}")
                off = r["off"]
            m.update(upload_s=round(time.time() - t, 3), uploaded_bytes=len(data), path="upload + activate")
            status = self.device_status()
            slot = 1 - active if active >= 0 else 0
            # the device chose the inactive slot; double-check by hash
            cand = [i for i, sl in enumerate(status["sockets"][sock]["slots"]) if sl.get("sha") == sha]
            slot = cand[0] if cand else slot
        win = {"win": mod.header.test_window_ms + CONFIRM_GRACE_MS} if mod.header.test_window_ms else {}
        self.fault_base[desired.dep_id] = int(status["sockets"][sock].get("fault_reverts", 0) or 0)
        t = time.time()
        r = L.call(lambda l: l.flexmod("activate", sock=sock, slot=slot, dep=desired.dep_id, params=params,
                                       test=True, timeout=30, **win), retries=0)
        m["windowMs"] = mod.header.test_window_ms or None
        m["rpc_ms"] = round((time.time() - t) * 1000, 1)
        self._finish(desired, sock, r, m)

    def _finish(self, desired, sock, r, m):
        self.last_hb.pop(desired.dep_id, None)
        m["apply_s"] = round(time.time() - m.pop("t0"), 3)
        m["device"] = {k: r.get(k) for k in ("switch_us", "load_us", "verify_us", "state")}
        self.st["metrics"][desired.dep_id] = {**m, "version": desired.version, "at": time.time()}
        self._save()
        if r.get("rc", 0) != 0:
            raise DriverError(f"device rejected activation: {r.get('reason')}")

    def commit(self, dep_id: str):
        sock = self._sock(dep_id)
        if sock is not None:
            r = self.links.call(lambda l: l.flexmod("confirm", sock=sock))
            if r.get("rc", 0) != 0:
                raise DriverError(f"confirm failed: {r.get('reason')}")

    def remove(self, dep_id):
        sock = self._sock(dep_id)
        if sock is not None:
            self.links.call(lambda l: l.flexmod("remove", sock=sock))
        self.st["socks"].pop(dep_id, None)
        self._save()

    def _observed(self, s: dict) -> Observed:
        if s["state"] == "empty":
            return Observed(present=False)
        sha = s["slots"][s["active"]].get("sha") if s["active"] >= 0 else None
        known = self.st["shas"].get(sha or "", {})
        return Observed(present=True, running=bool(s.get("healthy")), digest=known.get("digest"),
                        version=known.get("version") or s.get("version"), restarts=s.get("auto_reverts", 0),
                        detail=f"{s['state']} steps={s.get('steps')} hb={s.get('hb')} errors={s.get('errors')}")

    def status(self, dep_id):
        try:
            st = self.device_status()
        except DriverError:
            # MCU unreachable (power, cable): keep the last known identity so the engine does not
            # "repair" by re-uploading; health reports it as unhealthy.
            return Observed(present=True, running=False, detail="MCU unreachable")
        sock = self._sock(dep_id, st)
        if sock is None:
            return Observed(present=False)
        return self._observed(st["sockets"][sock])

    def list(self):
        try:
            st = self.device_status()
        except DriverError:
            return {}
        return {s["dep"]: self._observed(s) for s in st["sockets"] if s["state"] != "empty" and s["dep"]}

    def health(self, dep_id, probe):
        try:
            st = self.device_status()
        except DriverError:
            return False
        sock = self._sock(dep_id, st)
        if sock is None:
            return False
        s = st["sockets"][sock]
        base = self.fault_base.get(dep_id)
        if base is not None and int(s.get("fault_reverts", 0) or 0) > base:
            return False   # the base image reverted the module under test: what runs now is the old one (fail-fast)
        hb, prev = s.get("hb", 0), self.last_hb.get(dep_id)
        self.last_hb[dep_id] = hb
        advancing = prev is None or hb > prev
        return bool(s.get("healthy")) and advancing and s["state"] != "empty"

    def failed(self, dep_id: str) -> str | None:
        """Fail-fast (D1 gap 2): a definite module fault ends the health gate at once instead of after the
        window. Definite = the base image reverted the module under test on its own (step() errors, heartbeat
        lost / never started; counted in `fault_reverts`), or the module under test keeps failing its steps.
        Uses the status the health probe just read (one SMP round trip per probe)."""
        c = self._status_cache
        try:
            st = c[1] if c and time.time() - c[0] < 1.0 else self.device_status()
        except DriverError:
            return None   # unreachable is "not healthy yet", never a definite fault
        sock = self._sock(dep_id, st)
        if sock is None:
            return None
        s = st["sockets"][sock]
        base = self.fault_base.get(dep_id)
        if base is not None and int(s.get("fault_reverts", 0) or 0) > base:
            age = s.get("fault_age_ms", -1)
            if age is not None and age >= 0:
                self.fault_at[dep_id] = (c[0] if c else time.time()) - age / 1000.0
            return f"module fault on {self.child}: {s.get('last_reason') or 'reverted by the base image'}"
        if s["state"] == "testing" and int(s.get("last_rc", 0)) < 0 and int(s.get("errors", 0)) >= 3:
            return f"module under test fails its steps on {self.child} (last rc {s.get('last_rc')}, {s.get('errors')} errors)"
        return None

    def failure_detail(self, dep_id: str, healthy_at: float) -> dict:
        """For the engine's Failed report after a rollback (M2): when the fault became visible on the device (the
        base image's fail-fast revert) and how long until the previous version was healthy again."""
        fa = self.fault_at.pop(dep_id, None)
        if fa is None:
            return {}
        return {"faultVisibleAt": round(fa, 3), "faultToHealthySeconds": round(healthy_at - fa, 3)}

    def capabilities(self):
        """Never raises. With the MCU unreachable the fragment carries no MCU facts (no ABI/arch/sockets
        guesses) and says `reachable: false`; the facts come with the next capabilities document."""
        out = {"profile": self.profile, "driver": self.name, "transport": "SMP over UART (QEMU: TCP)"}
        try:
            info = self.info(refresh=True)
        except DriverError as e:
            return {**out, "reachable": False, "detail": str(e)[:300]}
        facts = {"moduleAbi": f"{info.get('abi_major')}.{info.get('abi_minor')}"
                 if info.get("abi_major") is not None and info.get("abi_minor") is not None else None,
                 "arch": info.get("arch"), "floatAbi": info.get("float_abi"),
                 "caps": flxm.caps_names(info.get("caps", 0)), "sockets": info.get("sockets"),
                 "slotBytes": info.get("slot_size"), "llextHeapBytes": info.get("llext_heap")}
        return {**out, **{k: v for k, v in facts.items() if v is not None}}


class McuFirmwareDriver(Driver):
    """Base image updates through MCUboot: upload to slot1, mark test, reset, health, confirm.
    Rollback = reset without confirm (MCUboot swaps back)."""

    profile = "mcu-firmware"
    name = "mcumgr-mcuboot"

    def __init__(self, links: LinkPool, data_dir: Path, child_id: str, module_driver: McuModuleDriver | None = None):
        self.links = links
        self.child = child_id
        self.mods = module_driver
        self.path = Path(data_dir) / "mcu-firmware-driver.json"
        self.st = read_json(self.path, {"versions": {}, "dep": None, "metrics": {}})

    def _save(self):
        atomic_write_json(self.path, self.st)

    def _images(self) -> list[dict]:
        return self.links.call(lambda l: l.image_state())

    def _info(self) -> dict:
        return self.links.call(lambda l: l.flexmod("info"))

    @staticmethod
    def _ver(v: str) -> str:
        return ".".join(str(v).split("+")[0].split(".")[:3])

    def preflight(self, rel, dep: dict) -> str | None:
        data = Path(rel.payload_path).read_bytes()
        if len(data) < 32 or int.from_bytes(data[:4], "little") != 0x96F3B83D:
            return "not an MCUboot image"
        return None

    def plan(self, current, desired):
        return [f"img_mgmt upload base image {desired.version} to slot1 of {self.child}",
                "mark test, os reset -> MCUboot swaps slot0/slot1",
                "health: base reports the new version unconfirmed, module sockets healthy",
                "confirm (img_mgmt) -- or reset without confirm: MCUboot reverts"]

    def _reset_and_wait(self) -> float:
        t = time.time()
        self.links.call(lambda l: l.reset(), retries=0)
        self.links._drop()
        time.sleep(1.0)  # do not talk to the UART while MCUboot swaps (bytes would only queue up)
        self.links.wait_up(60)
        return time.time() - t

    def apply(self, current, desired):
        with self.links.session():
            self._apply(current, desired)

    def _apply(self, current, desired):
        data = Path(desired.payload_path).read_bytes()
        self.st["versions"][desired.version] = desired.digest
        self.st["dep"] = desired.dep_id
        self.st["want"] = desired.version
        self._save()
        m = {"t0": time.time()}
        info = self._info()
        running = self._ver(info["image"])
        if running == desired.version:
            m["path"] = "already running"
        else:
            imgs = self._images()
            other = [i for i in imgs if i.get("slot") == 1 and self._ver(str(i.get("version"))) == desired.version]
            if other and not info["confirmed"]:
                # rollback: the previous image is still in slot1 and the new one is unconfirmed -> reset only
                m["path"] = "reset without confirm (MCUboot revert)"
                m["reset_s"] = round(self._reset_and_wait(), 3)
            else:
                t = time.time()
                self.links.call(lambda l: l.image_upload(data, chunk=768), retries=0)
                m["upload_s"] = round(time.time() - t, 3)
                m["uploaded_bytes"] = len(data)
                imgs = self._images()
                tgt = [i for i in imgs if i.get("slot") == 1 and self._ver(str(i.get("version"))) == desired.version]
                if not tgt:
                    raise DriverError(f"uploaded image not visible in slot1: {imgs}")
                self.links.call(lambda l: l.image_test(tgt[0]["hash"]))
                m["reset_s"] = round(self._reset_and_wait(), 3)
                m["path"] = "upload + test + reset (MCUboot swap)"
        if self.mods:
            self.mods._info = None
        m["apply_s"] = round(time.time() - m.pop("t0"), 3)
        self.st["metrics"][desired.version] = m
        self._save()

    def commit(self, dep_id: str):
        info = self._info()
        if not info["confirmed"]:
            self.links.call(lambda l: l.image_confirm())

    def remove(self, dep_id):
        self.st["dep"] = None
        self._save()

    def status(self, dep_id):
        try:
            info = self._info()
        except (DriverError, SmpError):
            return Observed(present=True, running=False, detail="MCU unreachable")
        v = self._ver(info["image"])
        return Observed(present=self.st.get("dep") == dep_id, running=True, version=v,
                        digest=self.st["versions"].get(v), detail="confirmed" if info["confirmed"] else "test")

    def list(self):
        return {self.st["dep"]: self.status(self.st["dep"])} if self.st.get("dep") else {}

    def health(self, dep_id, probe):
        try:
            info = self._info()
            st = self.links.call(lambda l: l.flexmod("status"))
        except (DriverError, SmpError):
            return False
        if dep_id is None or self.st.get("dep") != dep_id:
            return False   # not a deployment of this driver (unknown or removed)
        want = self.st.get("want")
        socks = [s for s in st["sockets"] if s["state"] != "empty"]
        return all(s.get("healthy") for s in socks) and (want is None or self._ver(info["image"]) == want)

    def capabilities(self):
        out = {"profile": self.profile, "driver": self.name, "bootloader": "MCUboot (swap with scratch, ECDSA P-256)"}
        try:
            info = self._info()
        except (DriverError, SmpError) as e:
            return {**out, "reachable": False, "detail": str(e)[:300]}
        return {**out, "baseImage": info.get("image"), "confirmed": info.get("confirmed"), "board": info.get("board")}

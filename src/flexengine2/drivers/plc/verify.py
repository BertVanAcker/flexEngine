"""flexEngine driver `plc-verify` (D-PICK IG-08/IG-09): PLC programs are loaded once by hand (decision 2026-10-16);
flexEngine only *verifies* that the program running on the PLC has the interface hash the release expects.

Release payload (from `flex release`, kind plc): a tar.gz with the generated TwinCAT/ST set or the Opta register map,
plus `flex-plc.json` = {target, via: ads|modbus|shm, ifaceHash, ...}. Nothing of it is downloaded to the PLC.

SPI mapping (P2 drivers/base.py, merged SPI):
  preflight  payload shape, the target PLC is configured on this engine, hash in flex-plc.json == release descriptor
             (and == the deployment's `_interfaceHash` parameter, if given)
  apply      records the expectation only ("no download"); never touches the PLC
  health     reads the hash from the PLC and compares:
               ads     `Hello` RPC of the generated FB_<App>Ads over ADS with a zero GP hash: the method returns
                       HASH_MISMATCH plus aRtHash and changes no FB state (xHashOk is only read inside Hello), so
                       probing never disturbs the real GP's link or epoch (D1: pyads test-server stand-in)
               modbus  the interface-hash holding registers of the interaction-only PLC (Opta; D1: pymodbus server)
               shm     RT_HDR.iface_hash of the P10 sim RT host's region (/dev/shm/<bridge>)
  failed     a PLC that answers with a *different* hash fails the gate at once (fail-fast): the deployment goes to
             Failed and the engine rolls the expectation back; an unreachable PLC is only "not healthy yet"
  commit     nothing (the PLC program was activated by hand)

Holding the GP release: a deployment whose activation requires `plc:<dep_id>` (or `plc:<dep_id>@<FLEX01 hash>`)
stays Pending until this driver has verified that deployment (at that hash): see engine.EdgeEngine._gate. So a CX
program from an older bridge layout fails `plc-verify` and the cell_controller release that needs the new layout
is held -- nothing half-switches (plan step 13).
"""

from __future__ import annotations

import ctypes
import io
import json
import struct
import tarfile
import threading
import time
from pathlib import Path

from flexengine2.util import atomic_write_json, read_json
from flexengine2.drivers.base import Driver, DriverError, Observed

ADSIGRP_SYM_HNDBYNAME = 0xF003
ADSIGRP_SYM_VALBYHND = 0xF005
STATUS = ["OK", "BUSY", "CANCELLED", "EXPIRED", "INACTIVE", "REJECTED", "NOT_LINKED", "HASH_MISMATCH", "INTERNAL",
          "LATE_COMPLETED"]  # flexmodel.bridge.STATUS
RT_MAGIC = 0x31425846  # "FXB1"
_ADS_LOCK = threading.Lock()
_ADS_ROUTES: set[tuple[str, str]] = set()


def read_spec(payload: str | Path) -> dict:
    """flex-plc.json from a PLC release payload (tar.gz)."""
    try:
        with tarfile.open(payload, "r:gz") as tf:
            m = tf.getmember("flex-plc.json")
            return json.loads(tf.extractfile(m).read())
    except (KeyError, tarfile.TarError, OSError, ValueError) as e:
        raise ValueError(f"not a PLC verify artefact (flex-plc.json): {e}") from e


def as_hash(raw: bytes) -> str:
    return "FLEX01_" + bytes(raw).hex()


class Unreachable(Exception):
    pass


# ---- hash readers -----------------------------------------------------------------------------------------------
class AdsReader:
    """`Hello` with a zero GP hash over ADS (pyads). On Linux pyads runs its own AMS router: the engine host needs
    a local AMS net id and a route to the PLC (and the PLC a static route back, SETUP s1)."""

    def __init__(self, cfg: dict, spec: dict):
        self.net_id, self.ip = cfg["amsNetId"], cfg["ip"]
        self.port = int(cfg.get("amsPort", spec.get("amsPort", 851)))
        self.local = cfg.get("localNetId")
        self.timeout_ms = int(float(cfg.get("timeout", 2.0)) * 1000)
        self.symbol = f"{spec.get('instance') or cfg.get('instance', 'MAIN.fbBridge')}#Hello"
        hello = spec.get("hello") or {}
        self.wsize = sum(f["size"] for f in hello.get("write", [])) or 36
        self.read = hello.get("read") or [{"name": "ret", "off": 0, "size": 2}, {"name": "aRtHash", "off": 2, "size": 32},
                                          {"name": "nRtEpoch", "off": 34, "size": 4}]
        self.rsize = sum(f["size"] for f in self.read)

    def _field(self, raw: bytes, name: str) -> bytes:
        f = next(x for x in self.read if x["name"] == name)
        return raw[f["off"]:f["off"] + f["size"]]

    def read_hash(self) -> tuple[str, str]:
        import pyads

        with _ADS_LOCK:
            try:
                if not _ADS_ROUTES:
                    pyads.open_port()
                    if self.local:
                        pyads.set_local_address(self.local)
                if (self.net_id, self.ip) not in _ADS_ROUTES:
                    pyads.add_route(self.net_id, self.ip)
                    _ADS_ROUTES.add((self.net_id, self.ip))
                conn = pyads.Connection(self.net_id, self.port, self.ip)
                conn.open()
                try:
                    conn.set_timeout(self.timeout_ms)
                    h = conn.get_handle(self.symbol)
                    wdata = bytes(self.wsize)  # aGpHash = 0..0 (never matches), nGpEpoch = 0
                    raw = conn.read_write(ADSIGRP_SYM_VALBYHND, h, ctypes.c_ubyte * self.rsize,
                                          (ctypes.c_ubyte * self.wsize).from_buffer_copy(wdata),
                                          ctypes.c_ubyte * self.wsize)
                    try:
                        conn.release_handle(h)
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                finally:
                    conn.close()
            except Exception as e:  # pyads raises ADSError / OSError / its own types
                raise Unreachable(f"ADS {self.net_id}:{self.port} ({self.ip}): {e}") from e
        raw = bytes(raw)
        ret = struct.unpack("<H", self._field(raw, "ret"))[0]
        st = STATUS[ret] if ret < len(STATUS) else str(ret)
        if st == "BUSY":
            raise Unreachable("PLC busy (RPC quota of this cycle used up)")
        return as_hash(self._field(raw, "aRtHash")), f"Hello -> {st}"


class ModbusReader:
    """Interface-hash holding registers (16 x uint16, big-endian) of an interaction-only PLC (Opta)."""

    def __init__(self, cfg: dict, spec: dict):
        self.host, self.port = cfg["host"], int(cfg.get("port", spec.get("port", 502)))
        self.unit = int(cfg.get("unitId", spec.get("unitId", 1)))
        reg = spec.get("registers") or {}
        self.addr, self.count = int(reg.get("address", 256)), int(reg.get("count", 16))
        self.timeout = float(cfg.get("timeout", 2.0))

    def read_hash(self) -> tuple[str, str]:
        from pymodbus.client import ModbusTcpClient

        c = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=0)
        try:
            if not c.connect():
                raise Unreachable(f"Modbus {self.host}:{self.port}: no connection")
            r = c.read_holding_registers(self.addr, count=self.count, device_id=self.unit)
            if r.isError():
                raise Unreachable(f"Modbus {self.host}:{self.port}: {r}")
            regs = list(r.registers)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001 - pymodbus connection errors
            raise Unreachable(f"Modbus {self.host}:{self.port}: {e}") from e
        finally:
            c.close()
        return as_hash(b"".join(int(x).to_bytes(2, "big") for x in regs)), f"holding {self.addr}+{self.count}"


class ShmReader:
    """RT_HDR.iface_hash of the P10 sim RT host (the rt side creates /dev/shm/<bridge>)."""

    def __init__(self, cfg: dict, spec: dict):
        self.path = Path(cfg.get("path") or f"/dev/shm/{spec.get('bridge')}")

    def read_hash(self) -> tuple[str, str]:
        try:
            with open(self.path, "rb") as f:
                hdr = f.read(96)
        except OSError as e:
            raise Unreachable(f"{self.path}: {e}") from e
        if len(hdr) < 96 or struct.unpack_from("<I", hdr, 0)[0] != RT_MAGIC:
            raise Unreachable(f"{self.path}: no bridge region (rt side not running)")
        return as_hash(hdr[64:96]), "RT_HDR"


READERS = {"ads": AdsReader, "modbus": ModbusReader, "shm": ShmReader}


# ---- driver -------------------------------------------------------------------------------------------------------
class PlcVerifyDriver(Driver):
    profile = "plc-verify"
    name = "plc-verify"

    def __init__(self, device_id: str, data_dir: Path, cfg: dict | None = None):
        cfg = cfg or {}
        self.device = device_id
        self.plcs: dict[str, dict] = cfg.get("plcs", {})  # target (e.g. "panda/cx", "opta") -> connection
        self.cache_s = float(cfg.get("cacheSeconds", 0.5))
        self.path = Path(data_dir) / "plc-verify.json"
        self.st = read_json(self.path, {"deps": {}})
        self._cache: dict[str, tuple[float, str | None, str]] = {}

    def _save(self):
        atomic_write_json(self.path, self.st)

    def _target(self, spec: dict, params: dict) -> str:
        return params.get("plc") or spec["target"]

    def _reader(self, rec: dict):
        conn = self.plcs.get(rec["target"])
        if conn is None:
            raise DriverError(f"PLC {rec['target']!r} is not configured on {self.device}")
        via = conn.get("via", rec["spec"].get("via"))
        if via not in READERS:
            raise DriverError(f"unknown verify transport {via!r}")
        return READERS[via](conn, rec["spec"])

    def read(self, dep_id: str) -> tuple[str | None, str]:
        """(hash or None if unreachable, detail); cached for cacheSeconds."""
        now = time.time()
        c = self._cache.get(dep_id)
        if c and now - c[0] < self.cache_s:
            return c[1], c[2]
        rec = self.st["deps"].get(dep_id)
        if not rec:
            return None, "not deployed"
        try:
            h, detail = self._reader(rec).read_hash()
        except Unreachable as e:
            h, detail = None, str(e)
        self._cache[dep_id] = (now, h, detail)
        rec.update(lastSeen=h, lastDetail=detail, lastReadAt=now)
        return h, detail

    # ---- SPI ------------------------------------------------------------------------------------------
    def preflight(self, rel, dep: dict) -> str | None:
        try:
            spec = read_spec(rel.payload_path)
        except ValueError as e:
            return str(e)
        target = self._target(spec, dep["spec"].get("parameters") or {})
        if target not in self.plcs:
            return f"PLC {target!r} is not configured on {self.device} (known: {sorted(self.plcs)})"
        desc = rel.descriptor.get("interfaceHash")
        if spec.get("ifaceHash") != desc:
            return f"verify spec hash {spec.get('ifaceHash')} differs from the release descriptor {desc}"
        if rel.payload_annotations.get("dev.flex.ifaceHash") not in (None, desc):
            return "payload annotation dev.flex.ifaceHash differs from the release descriptor"
        expect = (dep["spec"].get("parameters") or {}).get("_interfaceHash")
        if expect and expect != desc:
            return f"interface hash mismatch: deployment expects {expect[:24]}…, release has {desc[:24]}…"
        if (rel.descriptor.get("plc") or {}).get("download"):
            return "this engine never downloads PLC programs (decision 2026-10-16): release must say download: false"
        return None

    def plan(self, current, desired):
        spec = read_spec(desired.payload_path)
        target = self._target(spec, desired.parameters)
        return [f"no download: the {target} program was provisioned by hand",
                f"verify over {self.plcs.get(target, {}).get('via', spec.get('via'))}: running interface hash == "
                f"{spec['ifaceHash'][:24]}…",
                "Installed only when equal; otherwise Failed and the GP releases that need it stay held"]

    def apply(self, current, desired):
        try:
            spec = read_spec(desired.payload_path)
        except ValueError as e:
            raise DriverError(str(e)) from e
        self.st["deps"][desired.dep_id] = {"target": self._target(spec, desired.parameters), "spec": spec,
                                           "expect": spec["ifaceHash"], "version": desired.version,
                                           "digest": desired.digest, "appliedAt": time.time()}
        self._cache.pop(desired.dep_id, None)
        self._save()

    def remove(self, dep_id):
        self.st["deps"].pop(dep_id, None)
        self._cache.pop(dep_id, None)
        self._save()

    def status(self, dep_id):
        rec = self.st["deps"].get(dep_id)
        if not rec:
            return Observed(present=False)
        h, detail = self.read(dep_id)
        return Observed(present=True, running=h == rec["expect"], digest=rec["digest"], version=rec["version"],
                        detail=f"{rec['target']}: {detail}; running {h or '?'}")

    def list(self):
        return {d: self.status(d) for d in self.st["deps"]}

    def health(self, dep_id, probe):
        rec = self.st["deps"].get(dep_id)
        if not rec:
            return False
        h, _ = self.read(dep_id)
        return h is not None and h == rec["expect"]

    def failed(self, dep_id):
        rec = self.st["deps"].get(dep_id)
        if not rec:
            return None
        h, detail = self.read(dep_id)
        if h is not None and h != rec["expect"]:
            return (f"{rec['target']} runs interface {h[:24]}…, the release expects {rec['expect'][:24]}… "
                    f"({detail}): program not provisioned for this release")
        return None

    def verified(self, dep_id: str, want: str | None = None) -> bool:
        """For the engine's hold gate: this deployment is verified (at hash `want`, if given) right now."""
        rec = self.st["deps"].get(dep_id)
        if not rec:
            return False
        h, _ = self.read(dep_id)
        return h is not None and h == rec["expect"] and (want is None or want == h)

    def capabilities(self):
        return {"profile": self.profile, "driver": self.name, "download": False,
                "plcs": {k: v.get("via") for k, v in sorted(self.plcs.items())}}

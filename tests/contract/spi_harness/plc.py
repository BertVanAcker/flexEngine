"""Harnesses: PlcVerifyDriver over its three hash readers, against the D1 stand-ins (demo/edge/sim, read-only).

  ads     pyads test server answering the bridge FB's `Hello` (demo/edge/sim/ads_plc.py; AMS TCP port 48898)
  modbus  pymodbus server with the interface-hash holding registers (demo/edge/sim/modbus_plc.py)
  shm     a file with the P10 RT_HDR (magic + iface_hash at 64..96), like /dev/shm/<bridge>

"Fail for good" = the PLC runs a different interface hash than the release expects.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import struct
from contextlib import contextmanager
from pathlib import Path

import pytest

from flexengine2.drivers.base import Workload
from flexengine2.drivers.plc.verify import RT_MAGIC, PlcVerifyDriver

from .base import DEVICE, Harness, Release, deployment, digest_of, free_port, targz

REPO = Path(__file__).resolve().parents[4]
SIM = REPO / "demo" / "edge" / "sim"
GOOD = "FLEX01_" + "5a" * 32
OTHER = "FLEX01_" + "c3" * 32
HEALTH = {"type": "plc-verify", "periodSeconds": 0.2, "successThreshold": 2, "timeoutSeconds": 6}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"flex_contract_sim_{name}", SIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _PlcBase(Harness):
    driver_cls = PlcVerifyDriver
    profile = "plc-verify"
    via = "?"
    target = "ct/plc"
    na = {"drift_repair": "nothing runs on the PLC under the engine's control (verify only)"}

    def make_driver(self):
        return PlcVerifyDriver(DEVICE, self.data, {"plcs": {self.target: self.conn}, "cacheSeconds": 0.0})

    def _payload(self, version: str, expect: str, desc_hash: str | None = None) -> tuple[Path, dict]:
        spec = {"target": self.target, "via": self.via, "ifaceHash": expect, **self.spec_extra()}
        data = targz({"flex-plc.json": json.dumps(spec).encode(), "README": b"provisioned by hand\n"})
        p = self.tmp / "payloads" / f"plc-{version}.tar.gz"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return p, {"interfaceHash": desc_hash or expect, "plc": {"download": False}}

    def spec_extra(self) -> dict:
        return {}

    def _wl(self, version, expect, dep_id):
        p, desc = self._payload(version, expect)
        wl = Workload(dep_id=dep_id or self.dep_id, module="ct_bridge", version=version,
                      digest=digest_of(p.read_bytes() + version.encode()), payload_path=str(p),
                      payload_annotations={"dev.flex.profile": "plc-verify", "dev.flex.ifaceHash": expect},
                      health=dict(HEALTH))
        self._desc[wl.digest] = desc
        return wl

    def setup(self):
        self._desc: dict = {}

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, GOOD, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9-bad", OTHER, dep_id)

    def release(self, wl):
        return Release(wl.payload_path, wl.payload_annotations, self._desc[wl.digest])

    def refused_release(self):
        p, desc = self._payload("6.6.6-skew", GOOD, desc_hash=OTHER)  # flex-plc.json disagrees with the descriptor
        return Release(str(p), {"dev.flex.profile": "plc-verify"}, desc), deployment(self.dep_id, "ct_bridge", "6.6.6",
                                                                                      "plc-verify")

    def instances(self, dep_id):
        return None

    @contextmanager
    def unreachable(self):
        yield PlcVerifyDriver(DEVICE, self.data,
                              {"plcs": {self.target: self.down_conn()}, "cacheSeconds": 0.0})


class PlcModbusHarness(_PlcBase):
    id = "plc-verify[modbus]"
    via = "modbus"

    def setup(self):
        super().setup()
        self.port = free_port()
        self.plc = _load("modbus_plc").ModbusPlc(GOOD, port=self.port).start()
        self.conn = {"via": "modbus", "host": "127.0.0.1", "port": self.port, "timeout": 1.0}

    def down_conn(self):
        return {"via": "modbus", "host": "127.0.0.1", "port": free_port(), "timeout": 0.5}

    def cleanup(self):
        self.plc.stop()


class PlcShmHarness(_PlcBase):
    id = "plc-verify[shm]"
    via = "shm"

    def setup(self):
        super().setup()
        self.region = self.tmp / "shm" / "ct_bridge"
        self.region.parent.mkdir()
        hdr = bytearray(96)
        struct.pack_into("<I", hdr, 0, RT_MAGIC)
        hdr[64:96] = bytes.fromhex(GOOD[7:])
        self.region.write_bytes(bytes(hdr) + bytes(160))
        self.conn = {"via": "shm", "path": str(self.region)}

    def spec_extra(self):
        return {"bridge": "ct_bridge"}

    def down_conn(self):
        return {"via": "shm", "path": str(self.tmp / "shm" / "no-rt-side")}


_ADS = {}


class PlcAdsHarness(_PlcBase):
    """One pyads test server per session (AMS TCP port 48898 is fixed); each test resets its hash."""
    id = "plc-verify[ads]"
    via = "ads"

    def setup(self):
        super().setup()
        if "plc" not in _ADS:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", 48898))
            except OSError:
                pytest.skip("AMS TCP port 48898 is in use (another ADS stand-in runs); the ADS reader needs it")
            finally:
                s.close()
            _ADS["plc"] = _load("ads_plc").AdsPlc(GOOD).start()
        self.plc = _ADS["plc"]
        self.plc.hash, self.plc.online = GOOD, True
        self.conn = {"via": "ads", "amsNetId": "127.0.0.1.1.1", "ip": "127.0.0.1", "amsPort": 851,
                     "localNetId": "127.0.0.1.1.2", "timeout": 1.0}

    def down_conn(self):
        return {"via": "ads", "amsNetId": "127.0.0.9.1.1", "ip": "127.0.0.9", "amsPort": 851,
                "localNetId": "127.0.0.1.1.2", "timeout": 0.5}

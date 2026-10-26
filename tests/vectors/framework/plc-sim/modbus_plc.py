"""D1 stand-in for the Opta (interaction-only T5): a pymodbus TCP server with the register map of `flex release`
(regmap.json): the FLEX01 interface hash in 16 holding registers and the lamp coils.

    python -m sim.modbus_plc --hash FLEX01_... [--port 15020] [--address 256] [--host 127.0.0.1]

(--host 0.0.0.0 lets the cell_controller container write the lamp coils in the SIL harness.)
"""

from __future__ import annotations

import argparse
import asyncio
import threading
import time

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer


def hash_registers(iface: str) -> list[int]:
    raw = bytes.fromhex(iface.split("_", 1)[1])
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


class ModbusPlc:
    def __init__(self, iface_hash: str, port: int = 15020, address: int = 256, unit: int = 1, host: str = "127.0.0.1"):
        self.port, self.address, self.unit, self.host = port, address, unit, host
        self.hr = ModbusSequentialDataBlock(0, [0] * 1024)
        self.co = ModbusSequentialDataBlock(0, [False] * 64)
        dev = ModbusDeviceContext(hr=self.hr, co=self.co)
        self.ctx = ModbusServerContext(devices={unit: dev}, single=False)
        self.set_hash(iface_hash)
        self.loop = asyncio.new_event_loop()
        self.server = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def set_hash(self, iface_hash: str) -> None:
        # pymodbus' default (non zero-based) device context maps protocol address a to block index a + 1
        self.hr.setValues(self.address + 1, hash_registers(iface_hash))

    def lamp(self) -> list[bool]:
        return [bool(x) for x in self.co.getValues(1, 4)]

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def serve():
            self.server = ModbusTcpServer(self.ctx, address=(self.host, self.port))
            await self.server.serve_forever()

        self.loop.run_until_complete(serve())

    def start(self) -> "ModbusPlc":
        self.thread.start()
        time.sleep(0.5)
        return self

    def stop(self):
        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop).result(5)
        self.thread.join(5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", required=True)
    ap.add_argument("--port", type=int, default=15020)
    ap.add_argument("--address", type=int, default=256)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    ModbusPlc(a.hash, a.port, a.address, host=a.host).start()
    print(f"Modbus stand-in on {a.host}:{a.port}, hash registers {a.address}..{a.address + 15}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()

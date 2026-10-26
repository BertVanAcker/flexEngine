"""D1 stand-in for the CX (TwinCAT ADS): a pyads test server that answers the generated bridge FB's `Hello` RPC
(TcRpcEnable method, symbol handle `<instance>#Hello` via 0xF003, call via 0xF005) with a configurable RT interface
hash. Encoding as in P10's ADS stand-in; no task timing (pyads' test server is "for internal testing").

    python -m sim.ads_plc --hash FLEX01_... [--port 48898] [--instance MAIN.fbBridge]
"""

from __future__ import annotations

import argparse
import struct
import threading
import time

from pyads import constants
from pyads.testserver import AdsTestServer, AdvancedHandler

STATUS_OK, STATUS_HASH_MISMATCH = 0, 7


class HelloHandler(AdvancedHandler):
    def __init__(self, plc: "AdsPlc"):
        super().__init__()
        self.plc = plc
        self.handles: dict[int, str] = {}
        self.next = 0x7100

    def handle_request(self, request):
        cmd = struct.unpack("<H", request.ams_header.command_id)[0]
        if cmd == constants.ADSCOMMAND_READWRITE:
            data = request.ams_header.data
            ig, io, rlen, wlen = struct.unpack("<IIII", data[:16])
            w = data[16:16 + wlen]
            if ig == constants.ADSIGRP_SYM_HNDBYNAME:
                name = w.decode(errors="replace").strip("\x00")
                if name == f"{self.plc.instance}#Hello":
                    h = self.next
                    self.next += 1
                    self.handles[h] = name
                    return self._resp(request, struct.pack("<I", 4) + struct.pack("<I", h))
                return self._resp(request, b"", err=0x710)  # ADSERR_DEVICE_SYMBOLNOTFOUND
            if ig == constants.ADSIGRP_SYM_VALBYHND and io in self.handles:
                if not self.plc.online:
                    return self._resp(request, b"", err=0x711)
                if wlen != 36 or rlen != 38:
                    return self._resp(request, b"", err=0x706)  # ADSERR_DEVICE_INVALIDSIZE
                self.plc.calls += 1
                gp = w[:32]
                rt = bytes.fromhex(self.plc.hash.split("_", 1)[1])
                ret = STATUS_OK if gp == rt else STATUS_HASH_MISMATCH
                out = struct.pack("<H", ret) + rt + struct.pack("<I", self.plc.epoch)
                return self._resp(request, struct.pack("<I", len(out)) + out)
        if cmd == constants.ADSCOMMAND_WRITE:  # release handle
            data = request.ams_header.data
            ig = struct.unpack("<I", data[:4])[0]
            if ig == constants.ADSIGRP_SYM_RELEASEHND:
                return self._resp(request, b"")
        return super().handle_request(request)

    @staticmethod
    def _resp(request, content: bytes, err: int = 0):
        from pyads.testserver.handler import AmsResponseData

        state = struct.pack("<H", struct.unpack("<H", request.ams_header.state_flags)[0] | 1)
        return AmsResponseData(state, request.ams_header.error_code, struct.pack("<I", err) + content)


class AdsPlc:
    """In-process stand-in; `hash` can be changed to play "a CX program from an older bridge layout"."""

    def __init__(self, iface_hash: str, port: int = 48898, instance: str = "MAIN.fbBridge"):
        self.hash, self.port, self.instance = iface_hash, port, instance
        self.epoch, self.calls, self.online = 0x1234, 0, True
        self.server = AdsTestServer(handler=HelloHandler(self), logging=False, ip_address="127.0.0.1", port=port)

    def start(self) -> "AdsPlc":
        self.server.start()
        time.sleep(0.2)
        return self

    def stop(self):
        # Wait until the server thread has closed its listening socket: AMS TCP port 48898 is fixed, so the next
        # stand-in (the next test) can only bind once this one is really gone.
        self.server.stop()
        self.server.join(timeout=10)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", required=True)
    ap.add_argument("--port", type=int, default=48898)
    ap.add_argument("--instance", default="MAIN.fbBridge")
    a = ap.parse_args()
    plc = AdsPlc(a.hash, a.port, a.instance).start()
    print(f"ADS stand-in on 127.0.0.1:{a.port}, {a.instance}#Hello -> {a.hash}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        plc.stop()


if __name__ == "__main__":
    main()

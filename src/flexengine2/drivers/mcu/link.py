"""SMP client for the flexEngine-MCU: serial framing (smp.packet, the smpclient stack) over a byte pipe.

The pipe is a TCP socket here (QEMU `-serial tcp:...` for UART1); on the lab boards it is the MCU's UART
or USB-CDC (pyserial). Standard groups use the typed messages of the `smp` package (image management,
OS management); the flexmod group (64) is encoded directly (header + CBOR).
"""

from __future__ import annotations

import socket
import struct
import time

import cbor2
import smp.image_management as smpimg
import smp.os_management as smpos
from smp import packet as smppacket

FLEXMOD_GROUP = 64
CMD = {"info": 0, "status": 1, "upload": 2, "activate": 3, "confirm": 4, "revert": 5, "remove": 6,
       "inject": 7}  # 7: D-PICK gateway relay (IG-07), base images built after 2026-10-16
_HDR = struct.Struct("!BBHHBB")


class SmpError(RuntimeError):
    pass


class SmpLink:
    def __init__(self, host: str, port: int, timeout: float = 5.0, line_length: int = 127):
        self.addr = (host, port)
        self.timeout = timeout
        self.line_length = line_length
        self.sock: socket.socket | None = None
        self.seq = 0
        self.buf = b""
        self.stats = {"requests": 0, "bytes_tx": 0, "bytes_rx": 0}

    # ---- pipe -------------------------------------------------------------------------------
    def connect(self, wait: float = 10.0):
        end = time.time() + wait
        while True:
            try:
                self.sock = socket.create_connection(self.addr, timeout=self.timeout)
                self.buf = b""
                return self
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.1)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send_frame(self, frame: bytes):
        for pkt in smppacket.encode(frame, line_length=self.line_length):
            self.sock.sendall(pkt)
            self.stats["bytes_tx"] += len(pkt)

    def _read_line(self, deadline: float) -> bytes:
        while b"\n" not in self.buf:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("SMP response timeout")
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise TimeoutError("SMP response timeout") from None
            if not chunk:
                raise ConnectionError("SMP pipe closed")
            self.stats["bytes_rx"] += len(chunk)
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line + b"\n"

    def _recv_frame(self, timeout: float) -> bytes:
        deadline = time.time() + timeout
        while True:
            line = self._read_line(deadline)
            if not line.startswith(smppacket.START_DELIMITER):
                continue  # noise
            dec = smppacket.decode()
            next(dec)
            try:
                dec.send(line)
                while True:
                    dec.send(self._read_line(deadline))
            except StopIteration as e:
                return e.value

    def transact(self, frame: bytes, seq: int, timeout: float | None = None) -> bytes:
        self.stats["requests"] += 1
        self._send_frame(frame)
        while True:
            rsp = self._recv_frame(timeout or self.timeout)
            if rsp[6] == seq:
                return rsp

    # ---- flexmod group ------------------------------------------------------------------------
    def flexmod(self, cmd: str, timeout: float | None = None, **body) -> dict:
        op = 0 if cmd in ("info", "status") else 2
        self.seq = (self.seq + 1) & 0xFF
        payload = cbor2.dumps(body)
        frame = _HDR.pack((1 << 3) | op, 0, len(payload), FLEXMOD_GROUP, self.seq, CMD[cmd]) + payload
        rsp = self.transact(frame, self.seq, timeout)
        out = cbor2.loads(rsp[8:]) if len(rsp) > 8 else {}
        if "err" in out or (isinstance(out.get("rc"), int) and "reason" not in out and out.get("rc") not in (0, None)):
            raise SmpError(f"flexmod {cmd}: SMP error {out}")
        return out

    # ---- standard groups (smp typed messages) ------------------------------------------------
    def _typed(self, req, rsp_cls, timeout=None):
        rsp = self.transact(req.BYTES, req.header.sequence, timeout)
        try:
            return rsp_cls.loads(rsp)
        except Exception:
            raise SmpError(f"{type(req).__name__}: {cbor2.loads(rsp[8:])}") from None

    def image_state(self) -> list[dict]:
        r = self._typed(smpimg.ImageStatesReadRequest(), smpimg.ImageStatesReadResponse)
        return [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in r.images]

    def image_upload(self, data: bytes, chunk: int = 512, progress=None) -> float:
        import hashlib
        t0 = time.time()
        off, sha = 0, hashlib.sha256(data).digest()
        while off < len(data):
            part = data[off:off + chunk]
            kw = {"len": len(data), "sha": sha} if off == 0 else {}
            req = smpimg.ImageUploadWriteRequest(off=off, data=part, image=0, **kw)
            r = self._typed(req, smpimg.ImageUploadWriteResponse, timeout=20 if off == 0 else None)
            off = r.off
            if progress:
                progress(off)
        return time.time() - t0

    def image_test(self, hash_: bytes):
        return self._typed(smpimg.ImageStatesWriteRequest(hash=hash_, confirm=False), smpimg.ImageStatesWriteResponse)

    def image_confirm(self):
        return self._typed(smpimg.ImageStatesWriteRequest(confirm=True), smpimg.ImageStatesWriteResponse)

    def reset(self):
        try:
            self._typed(smpos.ResetWriteRequest(), smpos.ResetWriteResponse, timeout=3)
        except (TimeoutError, ConnectionError):
            pass

    def echo(self, text: str = "ping") -> str:
        r = self._typed(smpos.EchoWriteRequest(d=text), smpos.EchoWriteResponse)
        return r.r

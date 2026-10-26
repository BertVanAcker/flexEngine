"""Fake flexEngine-MCU behind SMP (serial framing over TCP, like QEMU's `-serial tcp:`) for the push tier.

It answers the requests `SmpLink` sends -- the flexmod group 64 (info, status, upload, activate, confirm, revert,
remove), img_mgmt (state read/write, upload) and os_mgmt reset -- with the base image's semantics as read from
probes/p3-mcu-hotload/zephyr/base/src/{flexmod_mgmt.c, modmgr.c}:

  * two module sockets, A/B slots each; an upload goes to the inactive slot, is SHA-256 checked and its FLXM
    signature verified on completion (a bad module is erased); TESTING blocks a new upload
  * activate(test) -> TESTING until confirm; revert goes back to the confirmed slot (or EMPTY); the deadline or a
    module fault reverts automatically (fault_reverts, last_reason "fail-fast: ...")
  * module behaviour comes from the FLXM header name: a name ending in "_bad" never produces a heartbeat and is
    reverted by the base `fault_after` seconds after activation; any other module is healthy at once
  * MCUboot: slot0/slot1, image_test + reset swaps (test boot, unconfirmed), a reset of an unconfirmed image swaps
    back; an image whose MCUboot build number is 99 is a "FAULT build" (module runner disabled: no module is
    healthy). A reset takes the MCU off the line for `reboot_s`.

Fault injection for the contract suite: `down` (unreachable: the listener is closed), `fail_confirm`.
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
import time
import zlib

import cbor2
from smp import packet as smppacket

from flexengine2.drivers.mcu import flxm

_HDR = struct.Struct("!BBHHBB")
FLEXMOD, IMG, OS = 64, 1, 0
FLEXMOD_CMDS = {0: "info", 1: "status", 2: "upload", 3: "activate", 4: "confirm", 5: "revert", 6: "remove"}


def mcuboot_image(version: str, payload: bytes = b"", build: int = 0) -> bytes:
    """A minimal MCUboot-shaped image (header magic + version) the fake boots; build 99 = FAULT build."""
    major, minor, rev = (int(x) for x in version.split("."))
    hdr = struct.pack("<IIHHII", 0x96F3B83D, 0, 32, 0, len(payload), 0) + struct.pack("<BBHI", major, minor, rev, build)
    hdr += b"\0" * (32 - len(hdr))
    return hdr + payload


def _image_version(data: bytes) -> tuple[str, int]:
    major, minor, rev, build = struct.unpack_from("<BBHI", data, 20)
    return f"{major}.{minor}.{rev}", build


class Sock:
    def __init__(self, i: int):
        self.i = i
        self.state, self.active, self.confirmed, self.dep, self.params = "empty", -1, -1, "", ""
        self.slots: list[dict | None] = [None, None]
        self.up: dict | None = None
        self.loaded: dict | None = None
        self.activated_at = 0.0
        self.deadline = 0.0
        self.fault_reverts = self.auto_reverts = self.reverts = self.uploads = 0
        self.fault_at = 0.0
        self.last_reason = ""


class FakeMcu:
    def __init__(self, module_pub_raw: bytes, base_version: str = "1.0.0", fault_after: float = 1.0,
                 reboot_s: float = 0.3, sockets: int = 2):
        self.pub = flxm.public_from_raw(module_pub_raw)
        self.fault_after, self.reboot_s = fault_after, reboot_s
        self.socks = [Sock(i) for i in range(sockets)]
        img = mcuboot_image(base_version)
        self.img = [{"data": img, "version": base_version, "build": 0, "hash": hashlib.sha256(img).digest(),
                     "confirmed": True, "pending": False}, None]
        self.img_up: bytearray | None = None
        self.boots = 1
        self.boot_at = time.time()
        self.down = False
        self.fail_confirm = False
        self.lock = threading.RLock()
        self.conns: list[socket.socket] = []
        self.srv: socket.socket | None = None
        self.port = 0
        self.requests: list[tuple[str, dict]] = []

    # ---- life cycle -------------------------------------------------------------------------------------------
    def start(self) -> "FakeMcu":
        self._listen()
        return self

    def _listen(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        self.port = s.getsockname()[1]
        s.listen(8)
        self.srv = s
        threading.Thread(target=self._accept, args=(s,), daemon=True).start()

    def _drop_line(self):
        if self.srv:
            try:
                self.srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.srv.close()
            self.srv = None
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def set_down(self, down: bool):
        self.down = down
        if down:
            self._drop_line()
        elif self.srv is None:
            self._listen()

    def stop(self):
        self._drop_line()

    def _accept(self, srv: socket.socket):
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket):
        buf = b""
        try:
            while True:
                while b"\n" not in buf:
                    chunk = c.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                lines = []
                frame = None
                dec = smppacket.decode()
                next(dec)
                while frame is None:
                    while b"\n" not in buf:
                        chunk = c.recv(4096)
                        if not chunk:
                            return
                        buf += chunk
                    line, buf = buf.split(b"\n", 1)
                    line += b"\n"
                    if not lines and not line.startswith(smppacket.START_DELIMITER):
                        continue
                    lines.append(line)
                    try:
                        dec.send(line)
                    except StopIteration as e:
                        frame = e.value
                rsp, after = self._handle(frame)
                for pkt in smppacket.encode(rsp, line_length=127):
                    c.sendall(pkt)
                if after:
                    after()
        except OSError:
            return

    # ---- protocol ------------------------------------------------------------------------------------------
    def _handle(self, frame: bytes):
        b0, flags, ln, group, seq, cmd = _HDR.unpack(frame[:8])
        op = b0 & 7
        body = cbor2.loads(frame[8:8 + ln]) if ln else {}
        after = None
        with self.lock:
            self._tick()
            if group == FLEXMOD:
                name = FLEXMOD_CMDS.get(cmd, "?")
                self.requests.append((name, {k: v for k, v in body.items() if k != "data"}))
                out = getattr(self, f"fm_{name}")(**body)
            elif group == IMG and cmd == 0:
                out = self.img_state(**body) if op == 2 else self.img_read()
            elif group == IMG and cmd == 1:
                out = self.img_upload(**body)
            elif group == OS and cmd == 5:
                out, after = {}, self._reset
            elif group == OS and cmd == 0:
                out = {"r": body.get("d", "")}
            else:
                out = {"rc": 8}
        payload = cbor2.dumps(out)
        return _HDR.pack((b0 & ~7) | (op + 1), 0, len(payload), group, seq, cmd) + payload, after

    def _tick(self):
        now = time.time()
        for s in self.socks:
            if s.state != "testing":
                continue
            if s.loaded and s.loaded["bad"] and now - s.activated_at > self.fault_after:
                s.fault_reverts += 1
                s.fault_at = now
                self._revert(s, automatic=True)
                s.last_reason = "fail-fast: no heartbeat (last rc 0) -> reverted"
            elif now > s.deadline:
                self._revert(s, automatic=True)
                s.last_reason = "not confirmed within the window -> reverted"

    # ---- flexmod ---------------------------------------------------------------------------------------------
    def fm_info(self):
        v = self.img[0]
        return {"base": v["version"], "image": f"{v['version']}+{v['build']}", "confirmed": v["confirmed"],
                "board": "fake/mps2", "abi_major": 1, "abi_minor": 0, "float_abi": "soft", "arch": "armv7m",
                "caps": flxm.caps_mask(["pwm", "localbus"]), "sockets": len(self.socks), "slot_size": 128 * 1024,
                "llext_heap": 64 * 1024, "uptime_ms": int((time.time() - self.boot_at) * 1000)}

    def _healthy(self, s: Sock) -> bool:
        return bool(s.loaded) and not s.loaded["bad"] and self.img[0]["build"] != 99

    def fm_status(self):
        now = time.time()
        out = []
        for s in self.socks:
            h = self._healthy(s)
            ticks = int((now - s.activated_at) * 200) if h else 0
            d = {"sock": s.i, "dep": s.dep, "state": s.state, "active": s.active, "confirmed": s.confirmed,
                 "slots": [{"valid": True, "name": sl["name"], "version": sl["version"], "sha": sl["sha"],
                            "size": len(sl["data"])} if sl else {"valid": False} for sl in s.slots],
                 "steps": ticks, "errors": 0, "hb": ticks, "last_rc": 0, "healthy": h,
                 "uploads": s.uploads, "reverts": s.reverts, "auto_reverts": s.auto_reverts,
                 "fault_reverts": s.fault_reverts, "fault_age_ms": int((now - s.fault_at) * 1000) if s.fault_at else -1,
                 "params_crc": zlib.crc32(s.params.encode()), "last_reason": s.last_reason}
            if s.loaded:
                d.update(running=s.loaded["name"], version=s.loaded["version"])
            out.append(d)
        return {"uptime_ms": int((now - self.boot_at) * 1000), "boots": self.boots, "sockets": out}

    def _sock(self, sock):
        if not isinstance(sock, int) or not 0 <= sock < len(self.socks):
            return None
        return self.socks[sock]

    def fm_upload(self, sock=0, off=0, data=b"", len=0, sha=b""):  # noqa: A002 - SMP field names
        s = self._sock(sock)
        if s is None:
            return {"rc": -1, "reason": f"no socket {sock}", "off": 0}
        if off == 0:
            if s.state == "testing":
                return {"rc": -1, "reason": f"socket {sock} is testing a module: confirm or revert first", "off": 0}
            slot = 1 - s.active if s.active >= 0 else 0
            s.slots[slot] = None
            s.up = {"slot": slot, "total": len, "buf": bytearray(), "sha": sha}
            s.uploads += 1
        if not s.up or off != builtins_len(s.up["buf"]):
            return {"rc": -1, "reason": "unexpected offset", "off": builtins_len(s.up["buf"]) if s.up else 0}
        s.up["buf"] += data
        if builtins_len(s.up["buf"]) == s.up["total"]:
            raw = bytes(s.up["buf"])
            up, s.up = s.up, None
            if up["sha"] and hashlib.sha256(raw).digest() != up["sha"]:
                return {"rc": -1, "reason": "upload SHA-256 mismatch", "off": builtins_len(raw)}
            try:
                mod = flxm.parse(raw)
            except ValueError as e:
                return {"rc": -1, "reason": f"not a module: {e}", "off": builtins_len(raw)}
            ok, why = flxm.verify(mod, self.pub)
            if not ok:
                s.last_reason = why
                return {"rc": -1, "reason": why, "off": builtins_len(raw)}
            h = mod.header
            s.slots[up["slot"]] = {"data": raw, "name": h.name, "version": h.version, "sha": h.elf_sha256.hex(),
                                   "bad": h.name.endswith("_bad")}
        return {"rc": 0, "off": builtins_len(s.up["buf"]) if s.up else off + builtins_len(data)}

    def _load(self, s: Sock, slot: int) -> bool:
        sl = s.slots[slot]
        if not sl:
            return False
        s.loaded = sl
        s.activated_at = time.time()
        return True

    def fm_activate(self, sock=0, slot=0, dep="", params="", test=True, win=0):
        s = self._sock(sock)
        if s is None or slot not in (0, 1):
            return {"rc": -1, "reason": "bad socket/slot", "state": "empty"}
        if s.state == "testing" and slot != s.active:
            return {"rc": -1, "reason": f"socket {sock} is testing: confirm or revert first", "state": s.state}
        if not self._load(s, slot):
            return {"rc": -1, "reason": f"slot {'AB'[slot]} holds no valid module", "state": s.state}
        s.params = params or ""
        if dep:
            s.dep = dep
        s.active = slot
        if test and s.confirmed != slot:
            s.deadline = time.time() + (win or 30000) / 1000.0
            s.state = "testing"
        else:
            s.state, s.confirmed = "confirmed", slot
        s.last_reason = ""
        return {"rc": 0, "state": s.state, "switch_us": 12, "load_us": 340, "verify_us": 900}

    def fm_confirm(self, sock=0):
        s = self._sock(sock)
        if s is None:
            return {"rc": -1, "reason": "bad socket", "state": "empty", "active": -1}
        if self.fail_confirm:
            return {"rc": -1, "reason": "flash write failed (injected)", "state": s.state, "active": s.active}
        if s.state == "testing":
            s.state, s.confirmed = "confirmed", s.active
        return {"rc": 0, "state": s.state, "active": s.active}

    def _revert(self, s: Sock, automatic: bool):
        if s.state != "testing":
            return
        s.reverts += 1
        s.auto_reverts += 1 if automatic else 0
        if s.confirmed >= 0 and self._load(s, s.confirmed):
            s.active, s.state = s.confirmed, "confirmed"
        else:
            s.loaded, s.active, s.state = None, -1, "empty"

    def fm_revert(self, sock=0):
        s = self._sock(sock)
        if s is None:
            return {"rc": -1, "reason": "bad socket", "state": "empty", "active": -1}
        self._revert(s, automatic=False)
        return {"rc": 0, "state": s.state, "active": s.active}

    def fm_remove(self, sock=0):
        s = self._sock(sock)
        if s is None:
            return {"rc": -1, "reason": "bad socket", "state": "empty", "active": -1}
        s.loaded, s.state, s.active, s.confirmed, s.dep, s.params = None, "empty", -1, -1, "", ""
        return {"rc": 0, "state": s.state, "active": s.active}

    # ---- MCUboot / img_mgmt ---------------------------------------------------------------------------------
    def _img_desc(self, i: int, im: dict) -> dict:
        return {"slot": i, "version": f"{im['version']}.{im['build']}" if im["build"] else im["version"],
                "hash": im["hash"], "bootable": True, "pending": im["pending"], "confirmed": im["confirmed"],
                "active": i == 0, "permanent": False}

    def img_read(self):
        return {"images": [self._img_desc(i, im) for i, im in enumerate(self.img) if im]}

    def img_upload(self, off=0, data=b"", image=0, len=0, sha=b"", **_):  # noqa: A002
        if off == 0:
            self.img_up = bytearray()
            self.img_total = len
            self.uploads_fw = getattr(self, "uploads_fw", 0) + 1
        if self.img_up is None or off != builtins_len(self.img_up):
            return {"rc": 3}
        self.img_up += data
        if builtins_len(self.img_up) == self.img_total:
            raw = bytes(self.img_up)
            self.img_up = None
            v, build = _image_version(raw)
            self.img[1] = {"data": raw, "version": v, "build": build, "hash": hashlib.sha256(raw).digest(),
                           "confirmed": False, "pending": False}
            return {"off": builtins_len(raw)}
        return {"off": builtins_len(self.img_up)}

    def img_state(self, hash=None, confirm=False):  # noqa: A002
        if confirm and not hash:
            if self.fail_confirm:
                return {"rc": 1}
            self.img[0]["confirmed"] = True
        elif hash and self.img[1] and self.img[1]["hash"] == hash:
            self.img[1]["pending"] = True
        else:
            return {"rc": 3}
        return self.img_read()

    def _reset(self):
        """os reset: MCUboot swaps (test) or reverts an unconfirmed test boot; the line drops for reboot_s."""
        def boot():
            with self.lock:
                self._drop_line()
                s0, s1 = self.img
                if s1 and s1["pending"]:
                    s1.update(pending=False, confirmed=False)
                    s0["confirmed"] = True
                    self.img = [s1, s0]
                elif not s0["confirmed"] and s1:
                    s1["confirmed"] = True
                    self.img = [s1, s0]
                for s in self.socks:  # TESTING at reset: revert like MCUboot (mm_init)
                    if s.state == "testing":
                        self._revert(s, automatic=True)
                    elif s.loaded:
                        s.activated_at = time.time()
                self.boots += 1
            time.sleep(self.reboot_s)
            with self.lock:
                self.boot_at = time.time()
                if not self.down:
                    self._listen()
        threading.Thread(target=boot, daemon=True).start()


builtins_len = len

"""In-process MQTT 3.1.1 broker and Redis (RESP2) server for the arduino-ota tests (EV-38), standard library only.

Real TCP servers speaking the subset the flexNode-arduino boards and the arduino-ota driver use, so the push tier
runs the real wire protocols without containers (the nightly tier runs the same tests against eclipse-mosquitto
and redis containers, marker `docker`).

  MiniBroker  CONNECT/CONNACK, SUBSCRIBE/SUBACK (exact topics, `+` and trailing `#`), PUBLISH QoS 0/1 (delivered
              at QoS 0, PUBACK to the sender), PINGREQ, DISCONNECT; `published` counts every PUBLISH it received
  MiniRedis   AUTH, SELECT (16 dbs), PING, GET, SET [EX|PX], DEL, EXISTS, EXPIRE, TTL, RPUSH, LRANGE, KEYS, FLUSHDB

`stop()` closes the listener and every client connection (an outage); `start()` again binds the same port.
"""

from __future__ import annotations

import fnmatch
import socket
import struct
import threading
import time

from flexengine2.drivers.arduino_wire import (CONNACK, CONNECT, DISCONNECT, PINGREQ, PINGRESP, PUBACK, PUBLISH,
                                              SUBACK, SUBSCRIBE, mqtt_packet, parse_publish, read_mqtt_packet)


class _TcpServer:
    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        self.host, self.port = host, port
        self.lsock: socket.socket | None = None
        self.conns: set[socket.socket] = set()
        self.lock = threading.Lock()

    def start(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(16)
        self.port = s.getsockname()[1]
        self.lsock = s
        threading.Thread(target=self._accept, args=(s,), daemon=True).start()
        return self

    def _accept(self, s: socket.socket):
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            with self.lock:
                self.conns.add(c)
            threading.Thread(target=self._serve_conn, args=(c,), daemon=True).start()

    def _serve_conn(self, c: socket.socket):
        try:
            self.serve(c)
        except (OSError, ValueError, struct.error, IndexError):
            pass
        finally:
            with self.lock:
                self.conns.discard(c)
            try:
                c.close()
            except OSError:
                pass

    def serve(self, c: socket.socket):  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self):
        s, self.lsock = self.lsock, None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)      # wakes the accept() thread; close() alone may not
            except OSError:
                pass
            s.close()
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


def topic_matches(sub: str, topic: str) -> bool:
    sp, tp = sub.split("/"), topic.split("/")
    for i, p in enumerate(sp):
        if p == "#":
            return True
        if i >= len(tp) or (p != "+" and p != tp[i]):
            return False
    return len(sp) == len(tp)


class MiniBroker(_TcpServer):
    def __init__(self, port: int = 0, host: str = "127.0.0.1"):
        super().__init__(port, host)
        self.subs: dict[socket.socket, list[str]] = {}
        self.published: list[tuple[str, bytes]] = []
        self.wlocks: dict[socket.socket, threading.Lock] = {}

    def _write(self, c: socket.socket, data: bytes):
        with self.wlocks.setdefault(c, threading.Lock()):
            c.sendall(data)

    def serve(self, c: socket.socket):
        t, _, _ = read_mqtt_packet(c)
        if t != CONNECT:
            return
        self._write(c, mqtt_packet(CONNACK, 0, b"\x00\x00"))
        try:
            while True:
                t, flags, b = read_mqtt_packet(c)
                if t == PUBLISH:
                    topic, payload, qos, pid = parse_publish(flags, b)
                    if qos == 1:
                        self._write(c, mqtt_packet(PUBACK, 0, struct.pack("!H", pid)))
                    self.published.append((topic, payload))
                    with self.lock:
                        targets = [s for s, subs in self.subs.items() if any(topic_matches(x, topic) for x in subs)]
                    pkt = mqtt_packet(PUBLISH, 0, struct.pack("!H", len(topic.encode())) + topic.encode() + payload)
                    for s in targets:
                        try:
                            self._write(s, pkt)
                        except OSError:
                            pass
                elif t == SUBSCRIBE:
                    pid = struct.unpack("!H", b[:2])[0]
                    pos, granted = 2, b""
                    while pos < len(b):
                        n = struct.unpack("!H", b[pos:pos + 2])[0]
                        topic = b[pos + 2:pos + 2 + n].decode()
                        pos += 3 + n
                        with self.lock:
                            self.subs.setdefault(c, []).append(topic)
                        granted += b"\x00"
                    self._write(c, mqtt_packet(SUBACK, 0, struct.pack("!H", pid) + granted))
                elif t == PINGREQ:
                    self._write(c, mqtt_packet(PINGRESP, 0, b""))
                elif t == DISCONNECT:
                    return
        finally:
            with self.lock:
                self.subs.pop(c, None)
            self.wlocks.pop(c, None)


class MiniRedis(_TcpServer):
    def __init__(self, port: int = 0, host: str = "127.0.0.1", password: str | None = None):
        super().__init__(port, host)
        self.password = password
        self.dbs: dict[int, dict[str, object]] = {}
        self.exp: dict[tuple[int, str], float] = {}
        self.dlock = threading.Lock()

    # -- data (also used directly by tests) --
    def db(self, n: int = 0) -> dict:
        return self.dbs.setdefault(int(n), {})

    def _live(self, n: int, k: str):
        e = self.exp.get((n, k))
        if e is not None and e <= time.time():
            self.db(n).pop(k, None)
            self.exp.pop((n, k), None)
        return self.db(n).get(k)

    def value(self, key: str, db: int = 0):
        with self.dlock:
            v = self._live(int(db), key)
        return v.decode() if isinstance(v, bytes) else v

    # -- RESP --
    @staticmethod
    def _read_cmd(c: socket.socket, buf: bytearray) -> list[bytes] | None:
        def line():
            while b"\r\n" not in buf:
                chunk = c.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf.extend(chunk)
            i = buf.index(b"\r\n")
            out = bytes(buf[:i])
            del buf[:i + 2]
            return out

        first = line()
        if not first.startswith(b"*"):
            return first.split()        # inline command
        args = []
        for _ in range(int(first[1:])):
            n = int(line()[1:])
            while len(buf) < n + 2:
                chunk = c.recv(65536)
                if not chunk:
                    raise OSError("closed")
                buf.extend(chunk)
            args.append(bytes(buf[:n]))
            del buf[:n + 2]
        return args

    @staticmethod
    def _enc(v) -> bytes:
        if v is None:
            return b"$-1\r\n"
        if isinstance(v, bool):
            return b":%d\r\n" % int(v)
        if isinstance(v, int):
            return b":%d\r\n" % v
        if isinstance(v, str):
            return b"+" + v.encode() + b"\r\n"
        if isinstance(v, Exception):
            return b"-ERR " + str(v).encode() + b"\r\n"
        if isinstance(v, list):
            return b"*%d\r\n" % len(v) + b"".join(MiniRedis._enc(x) for x in v)
        return b"$%d\r\n%s\r\n" % (len(v), v)

    def serve(self, c: socket.socket):
        buf, db, authed = bytearray(), 0, self.password is None
        while True:
            args = self._read_cmd(c, buf)
            if not args:
                continue
            name = args[0].upper().decode()
            a = [x.decode("utf-8", "replace") for x in args[1:]]
            if name == "AUTH":
                ok = self.password is None or a[-1] == self.password
                authed = authed or ok
                c.sendall(self._enc("OK" if ok else ValueError("invalid password")))
                continue
            if not authed:
                c.sendall(b"-NOAUTH Authentication required.\r\n")
                continue
            if name == "SELECT":
                db = int(a[0])
                c.sendall(self._enc("OK"))
                continue
            with self.dlock:
                r = self._exec(db, name, a, args[1:])
            c.sendall(self._enc(r))

    def _exec(self, db: int, name: str, a: list[str], raw: list[bytes]):
        d = self.db(db)
        if name == "PING":
            return "PONG"
        if name == "GET":
            v = self._live(db, a[0])
            return v if isinstance(v, (bytes, type(None))) else ValueError("WRONGTYPE")
        if name == "SET":
            d[a[0]] = raw[1]
            self.exp.pop((db, a[0]), None)
            if len(a) >= 4 and a[2].upper() in ("EX", "PX"):
                self.exp[(db, a[0])] = time.time() + float(a[3]) / (1 if a[2].upper() == "EX" else 1000)
            return "OK"
        if name == "DEL":
            n = 0
            for k in a:
                if self._live(db, k) is not None:
                    d.pop(k, None)
                    n += 1
            return n
        if name == "EXISTS":
            return sum(1 for k in a if self._live(db, k) is not None)
        if name == "EXPIRE":
            if self._live(db, a[0]) is None:
                return 0
            self.exp[(db, a[0])] = time.time() + float(a[1])
            return 1
        if name == "TTL":
            e = self.exp.get((db, a[0]))
            return -2 if self._live(db, a[0]) is None else (-1 if e is None else int(e - time.time()))
        if name == "RPUSH":
            lst = self._live(db, a[0])
            if lst is None:
                lst = d[a[0]] = []
            lst.extend(raw[1:])
            return len(lst)
        if name == "LRANGE":
            lst = self._live(db, a[0]) or []
            start, stop = int(a[1]), int(a[2])
            stop = len(lst) if stop == -1 else stop + 1
            return list(lst[start:stop])
        if name == "KEYS":
            return [k.encode() for k in list(d) if self._live(db, k) is not None and fnmatch.fnmatchcase(k, a[0])]
        if name == "FLUSHDB":
            d.clear()
            return "OK"
        return ValueError(f"unknown command '{name}'")

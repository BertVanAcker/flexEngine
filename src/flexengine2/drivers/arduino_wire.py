"""Minimal MQTT 3.1.1 and Redis (RESP2) clients for the arduino-ota driver (EV-38), standard library only.

The flexNode-arduino boards talk to the flexCloud over exactly these two protocols (PubSubClient for MQTT, the
vendored Arduino-Redis client for the flexCloud keys). The engine needs a small subset of each:

  MqttClient   CONNECT (clean session, optional username/password), PUBLISH QoS 0/1, SUBSCRIBE, PINGREQ,
               DISCONNECT; an optional reader thread delivers incoming PUBLISH packets (the board simulator uses it,
               the driver only publishes)
  RedisClient  AUTH, SELECT, GET, SET [EX], DEL, RPUSH, LRANGE, EXPIRE, PING (one connection, reconnects lazily)

Both raise `WireError` (an OSError) for every transport or protocol failure, so callers can treat "broker / Redis
unreachable" as one case. No TLS: the boards' PubSubClient and Redis client use plain TCP (P0), see
api/cp6-children.md §5.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

__all__ = ["WireError", "MqttClient", "RedisClient"]


class WireError(OSError):
    pass


# ---- MQTT 3.1.1 -------------------------------------------------------------------------------------------------
CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 8, 9, 12, 13, 14
CONNACK_TEXT = {1: "unacceptable protocol version", 2: "client id rejected", 3: "server unavailable",
                4: "bad user name or password", 5: "not authorized"}


def _mqtt_str(s: str | bytes) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def _remaining(n: int) -> bytes:
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (0x80 if n else 0))
        if not n:
            return bytes(out)


def mqtt_packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + _remaining(len(body)) + body


def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WireError("connection closed by peer")
        buf += chunk
    return bytes(buf)


def read_mqtt_packet(sock: socket.socket) -> tuple[int, int, bytes]:
    """(type, flags, body) of the next packet."""
    h = read_exact(sock, 1)[0]
    mult, n = 1, 0
    for _ in range(4):
        d = read_exact(sock, 1)[0]
        n += (d & 0x7F) * mult
        if not d & 0x80:
            break
        mult *= 128
    else:
        raise WireError("malformed MQTT remaining length")
    return h >> 4, h & 0x0F, read_exact(sock, n) if n else b""


def parse_publish(flags: int, body: bytes) -> tuple[str, bytes, int, int | None]:
    """(topic, payload, qos, packet id) of a PUBLISH body."""
    tl = struct.unpack("!H", body[:2])[0]
    topic = body[2:2 + tl].decode("utf-8", "replace")
    pos, qos, pid = 2 + tl, (flags >> 1) & 3, None
    if qos:
        pid = struct.unpack("!H", body[pos:pos + 2])[0]
        pos += 2
    return topic, body[pos:], qos, pid


class MqttClient:
    def __init__(self, host: str, port: int = 1883, client_id: str = "flexengine", username: str | None = None,
                 password: str | None = None, keepalive: int = 30, timeout: float = 3.0):
        self.host, self.port, self.client_id = host, int(port), client_id
        self.username, self.password = username or None, password or None
        self.keepalive, self.timeout = int(keepalive), float(timeout)
        self.sock: socket.socket | None = None
        self._pid = 0
        self._lock = threading.Lock()
        self._acks: dict[int, threading.Event] = {}
        self._reader: threading.Thread | None = None
        self._on_message = None
        self.closed = threading.Event()

    # -- connection --
    def connect(self) -> "MqttClient":
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise WireError(f"MQTT broker {self.host}:{self.port} unreachable: {e}") from e
        flags = 0x02 | (0x80 if self.username else 0) | (0x40 if self.password else 0)
        body = _mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + _mqtt_str(self.client_id)
        if self.username:
            body += _mqtt_str(self.username)
        if self.password:
            body += _mqtt_str(self.password)
        try:
            s.sendall(mqtt_packet(CONNECT, 0, body))
            t, _, b = read_mqtt_packet(s)
        except (OSError, struct.error) as e:
            s.close()
            raise WireError(f"MQTT broker {self.host}:{self.port}: no CONNACK ({e})") from e
        if t != CONNACK or len(b) < 2:
            s.close()
            raise WireError(f"MQTT broker {self.host}:{self.port}: unexpected packet {t} instead of CONNACK")
        if b[1] != 0:
            s.close()
            raise WireError(f"MQTT broker {self.host}:{self.port} refused the connection: "
                            f"{CONNACK_TEXT.get(b[1], b[1])}")
        self.sock = s
        self.closed.clear()
        return self

    def _next_pid(self) -> int:
        self._pid = self._pid % 65535 + 1
        return self._pid

    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise WireError("MQTT client not connected")
        try:
            with self._lock:
                self.sock.sendall(data)
        except OSError as e:
            raise WireError(f"MQTT send failed: {e}") from e

    def _wait_ack(self, ptype: int, pid: int) -> None:
        """Wait for PUBACK/SUBACK `pid`: through the reader thread when it runs, else read inline."""
        if self._reader is not None:
            ev = self._acks.setdefault(pid, threading.Event())
            if not ev.wait(self.timeout):
                raise WireError(f"MQTT broker sent no acknowledgement within {self.timeout:g} s")
            self._acks.pop(pid, None)
            return
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            try:
                t, flags, b = read_mqtt_packet(self.sock)
            except (OSError, struct.error) as e:
                raise WireError(f"MQTT broker sent no acknowledgement: {e}") from e
            if t == ptype and len(b) >= 2 and struct.unpack("!H", b[:2])[0] == pid:
                if t == SUBACK and len(b) > 2 and b[2] == 0x80:
                    raise WireError("MQTT broker refused the subscription")
                return
        raise WireError(f"MQTT broker sent no acknowledgement within {self.timeout:g} s")

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        body = _mqtt_str(topic)
        pid = None
        if qos:
            pid = self._next_pid()
            body += struct.pack("!H", pid)
        self._send(mqtt_packet(PUBLISH, (qos << 1) | int(retain), body + data))
        if qos:
            self._wait_ack(PUBACK, pid)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        pid = self._next_pid()
        self._send(mqtt_packet(SUBSCRIBE, 2, struct.pack("!H", pid) + _mqtt_str(topic) + bytes([qos])))
        self._wait_ack(SUBACK, pid)

    def ping(self) -> None:
        self._send(mqtt_packet(PINGREQ, 0, b""))

    def start(self, on_message) -> None:
        """Deliver incoming PUBLISH packets to on_message(topic, payload) from a reader thread (pings itself)."""
        self._on_message = on_message
        self.sock.settimeout(max(1.0, self.keepalive / 2))
        self._reader = threading.Thread(target=self._read_loop, name=f"mqtt-{self.client_id}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    t, flags, b = read_mqtt_packet(self.sock)
                except socket.timeout:
                    self.ping()
                    continue
                if t == PUBLISH:
                    topic, payload, qos, pid = parse_publish(flags, b)
                    if qos == 1:
                        self._send(mqtt_packet(PUBACK, 0, struct.pack("!H", pid)))
                    try:
                        self._on_message(topic, payload)
                    except Exception:  # noqa: BLE001 - a callback error must not end the connection
                        pass
                elif t in (PUBACK, SUBACK) and len(b) >= 2:
                    self._acks.setdefault(struct.unpack("!H", b[:2])[0], threading.Event()).set()
        except (OSError, struct.error, ValueError):
            pass
        finally:
            self.closed.set()

    def close(self) -> None:
        s, self.sock = self.sock, None
        self.closed.set()
        if s is None:
            return
        try:
            with self._lock:
                s.sendall(mqtt_packet(DISCONNECT, 0, b""))
        except OSError:
            pass
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


# ---- Redis RESP2 ------------------------------------------------------------------------------------------------
class RedisReplyError(WireError):
    """The server answered with an error reply (-ERR ...)."""


class RedisClient:
    def __init__(self, host: str, port: int = 6379, password: str | None = None, db: int | str = 0,
                 timeout: float = 2.0):
        self.host, self.port, self.password, self.db = host, int(port), password or None, str(db)
        self.timeout = float(timeout)
        self.sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    def _connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            self.sock = None
            raise WireError(f"Redis {self.host}:{self.port} unreachable: {e}") from e
        self._buf = b""
        if self.password:
            self._call("AUTH", self.password)
        if self.db not in ("", "0"):
            self._call("SELECT", self.db)

    def close(self) -> None:
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def _line(self) -> bytes:
        while b"\r\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WireError("Redis closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _bulk(self, n: int) -> bytes:
        while len(self._buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WireError("Redis closed the connection")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n + 2:]
        return data

    def _reply(self):
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RedisReplyError(f"Redis error: {rest.decode(errors='replace')}")
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._bulk(n)
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise WireError(f"Redis: unexpected reply {line[:40]!r}")

    def _call(self, *args):
        parts = [a if isinstance(a, bytes) else str(a).encode() for a in args]
        req = b"*%d\r\n" % len(parts) + b"".join(b"$%d\r\n%s\r\n" % (len(p), p) for p in parts)
        self.sock.sendall(req)
        return self._reply()

    def call(self, *args):
        with self._lock:
            if self.sock is None:
                self._connect()
            try:
                return self._call(*args)
            except RedisReplyError:
                raise
            except (OSError, ValueError) as e:
                self.close()
                raise WireError(f"Redis {self.host}:{self.port}: {e}") from e

    # -- commands --
    def ping(self) -> bool:
        return self.call("PING") == "PONG"

    def get(self, key: str) -> str | None:
        v = self.call("GET", key)
        return None if v is None else v.decode("utf-8", "replace")

    def set(self, key: str, value: str | bytes, ex: int | None = None) -> None:
        if ex:
            self.call("SET", key, value, "EX", int(ex))
        else:
            self.call("SET", key, value)

    def delete(self, *keys: str) -> int:
        return self.call("DEL", *keys) if keys else 0

    def expire(self, key: str, seconds: int) -> None:
        self.call("EXPIRE", key, int(seconds))

    def rpush(self, key: str, value: str) -> int:
        return self.call("RPUSH", key, value)

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        return [v.decode("utf-8", "replace") for v in self.call("LRANGE", key, start, stop) or []]

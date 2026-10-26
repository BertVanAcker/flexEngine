"""Minimal synchronous MessagePack-RPC client for arduino-router (moved from probe P4, flexp4/rpc.py).

Used by the applab driver's health check (it asks both sides of the running app for their contract status
over the router socket) and by the contract tests to send raw, deliberately ill-typed calls that bypass the
generated Python stubs.
"""

from __future__ import annotations

import socket
import time

import msgpack  # not a flexmodel dependency: needed only to talk to a router (pip install msgpack)


class RpcError(Exception):
    def __init__(self, method: str, code, message):
        super().__init__(f"{method}: {message} ({code})")
        self.method, self.code, self.message = method, code, message


class RawClient:
    def __init__(self, address: str, timeout: float = 5.0):
        path = address.removeprefix("unix://")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.unpacker = msgpack.Unpacker(raw=False)
        self.msgid = 0

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def notify(self, method: str, *params):
        self.sock.sendall(msgpack.packb([2, method, list(params)]))

    def call(self, method: str, *params, timeout: float = 5.0):
        self.msgid += 1
        mid = self.msgid
        self.sock.sendall(msgpack.packb([0, mid, method, list(params)]))
        deadline = time.monotonic() + timeout
        while True:
            for msg in self.unpacker:
                if isinstance(msg, list) and len(msg) == 4 and msg[0] == 1 and msg[1] == mid:
                    if msg[2]:
                        err = msg[2]
                        raise RpcError(method, err[0] if isinstance(err, list) else None,
                                       err[1] if isinstance(err, list) and len(err) > 1 else err)
                    return msg[3]
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method}: no response")
            self.sock.settimeout(left)
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("router closed the connection")
            self.unpacker.feed(data)


def call_once(address: str, method: str, *params, timeout: float = 3.0):
    with RawClient(address, timeout) as c:
        return c.call(method, *params, timeout=timeout)

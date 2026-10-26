"""A toxiproxy-like TCP shim between the engine and flexCloud, for network-cut tests.

Each engine request uses its own TCP connection, so the shim can peek at the HTTP request line and
apply rules:
  * cut()/restore()             drop every connection (the link is "down")
  * cut_on(pred, ...)            when a request line matches pred, cut the link from that point on
                                 (optionally let that request reach the server but drop the response,
                                 to simulate "request delivered, ack lost")
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable


class NetShim:
    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1", listen_port: int = 0):
        self.up = (upstream_host, upstream_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", listen_port))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.down = False
        self.rule: tuple[Callable[[str], bool], bool] | None = None
        self.log: list[tuple[float, str, str]] = []  # (t, request line, verdict)
        self.cut_at: float | None = None
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- control --------------------------------------------------------------------------
    def cut(self):
        with self._lock:
            self.down = True
            self.cut_at = time.time()

    def restore(self):
        with self._lock:
            self.down = False
            self.rule = None

    def cut_on(self, pred: Callable[[str], bool], deliver_request: bool = False):
        with self._lock:
            self.rule = (pred, deliver_request)

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- data path ------------------------------------------------------------------------
    def _accept(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c: socket.socket):
        try:
            c.settimeout(10)
            first = c.recv(65536)
            line = first.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            deliver_then_cut = False
            with self._lock:
                if self.down:
                    self.log.append((time.time(), line, "dropped"))
                    c.close()
                    return
                if self.rule and self.rule[0](line):
                    pred, deliver = self.rule
                    self.rule = None
                    if deliver:
                        deliver_then_cut = True
                    else:
                        self.down, self.cut_at = True, time.time()
                        self.log.append((time.time(), line, "cut"))
                        c.close()
                        return
                self.log.append((time.time(), line, "deliver-then-cut" if deliver_then_cut else "pass"))
            u = socket.create_connection(self.up, timeout=10)
            u.sendall(first)
            if deliver_then_cut:
                # forward the full request body, wait for the server to process it, then drop the reply
                c.settimeout(0.3)
                try:
                    while True:
                        more = c.recv(65536)
                        if not more:
                            break
                        u.sendall(more)
                except socket.timeout:
                    pass
                u.settimeout(5)
                try:
                    u.recv(65536)  # server has answered -> request was processed
                except OSError:
                    pass
                with self._lock:
                    self.down, self.cut_at = True, time.time()
                u.close()
                c.close()
                return
            self._pipe(c, u)
        except OSError:
            try:
                c.close()
            except OSError:
                pass

    def _pipe(self, a: socket.socket, b: socket.socket):
        def fwd(src, dst):
            try:
                while True:
                    if self.down:
                        break
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=fwd, args=(b, a), daemon=True)
        t.start()
        fwd(a, b)
        t.join(timeout=15)
        a.close()
        b.close()

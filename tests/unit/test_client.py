"""CloudClient over plain HTTP: the public interface stays as before EV-06 (request/get_json/post_json,
bytes_in, requests, Offline/Rejected)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flexengine2.cloud.client import CloudClient, Offline, Rejected, TlsConfigError
from flexengine2.identity import crypto

KEY = crypto.generate()


class H(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        H.seen.append((self.command, self.path, dict(self.headers), body))
        code = {"/404": 404, "/500": 500, "/503": 503, "/429": 429}.get(self.path, 200)
        out = json.dumps({"ok": code == 200}).encode()
        self.send_response(code)
        if code in (503, 429):
            self.send_header("Retry-After", "7")
        self.send_header("ETag", '"e1"')
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _go


@pytest.fixture(scope="module")
def url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_signed_get(url):
    c = CloudClient(url, "dev-a", KEY)
    assert c.get_json("/api") == {"ok": True}
    m, p, h, b = H.seen[-1]
    assert (m, p, b) == ("GET", "/api", b"")
    s = crypto.request_string("GET", "/api", h["X-Flex-Date"], h["X-Flex-Nonce"], b"")
    assert h["X-Flex-Device"] == "dev-a" and crypto.verify(crypto.public_b64(KEY), s, h["X-Flex-Signature"])
    assert c.requests == 1 and c.bytes_in == len(b'{"ok": true}')


def test_post_put_unsigned_and_headers(url):
    c = CloudClient(url, "dev-a", KEY)
    st, hdrs, data = c.post_json("/x", {"a": 1}, method="PUT", signed=False)
    m, _, h, b = H.seen[-1]
    assert st == 200 and hdrs["etag"] == '"e1"' and m == "PUT" and json.loads(b) == {"a": 1}
    assert "X-Flex-Signature" not in h and h["Content-Type"] == "application/json"
    st, _, _ = c.request("GET", "/y", headers={"If-None-Match": '"e0"'})
    assert H.seen[-1][2]["If-None-Match"] == '"e0"'


def test_errors(url):
    c = CloudClient(url, "dev-a", KEY)
    with pytest.raises(Rejected) as r:
        c.get_json("/404")
    assert r.value.status == 404
    with pytest.raises(Offline) as o:
        c.get_json("/503")
    assert o.value.retry_after == 7.0
    with pytest.raises(Offline):
        c.get_json("/429")
    with pytest.raises(Offline) as o:
        c.get_json("/500")
    assert o.value.retry_after is None


def test_unreachable_is_offline():
    with pytest.raises(Offline):
        CloudClient("http://127.0.0.1:1", "d", KEY, timeout=0.5).get_json("/")


def test_ports_and_positional_timeout():
    c = CloudClient("http://h", "d", KEY, 2.5)
    assert (c.host, c.port, c.timeout, c.ssl_context) == ("h", 80, 2.5, None)


def test_bad_scheme():
    with pytest.raises(TlsConfigError):
        CloudClient("ftp://h", "d", KEY)

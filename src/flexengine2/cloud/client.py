"""Device-side HTTP client: signed requests, one TCP connection per request, typed failures."""

from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit

from flexengine2.identity import crypto


class Offline(Exception):
    """Cloud not reachable / 5xx / 429. Retry later; never a reason to change local state."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Rejected(Exception):
    """Cloud answered with a definite 4xx."""

    def __init__(self, status: int, body: str):
        super().__init__(f"{status}: {body[:200]}")
        self.status = status


class CloudClient:
    def __init__(self, base_url: str, device_id: str, key, timeout: float = 5.0):
        u = urlsplit(base_url)
        self.host, self.port = u.hostname, u.port or 80
        self.device_id, self.key, self.timeout = device_id, key, timeout
        self.bytes_in = 0
        self.requests = 0

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None,
                signed: bool = True) -> tuple[int, dict, bytes]:
        h = {"Connection": "close", "Content-Length": str(len(body))}
        if body:
            h["Content-Type"] = "application/json"
        if signed:
            h.update(crypto.sign_request(self.key, self.device_id, method, path, body, int(time.time() * 1000)))
        h.update(headers or {})
        self.requests += 1
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            c.close()
        except (OSError, http.client.HTTPException) as e:
            raise Offline(f"{method} {path}: {type(e).__name__}: {e}") from None
        self.bytes_in += len(data)
        if r.status in (429, 502, 503, 504):
            ra = hdrs.get("retry-after")
            raise Offline(f"{method} {path}: HTTP {r.status}", float(ra) if ra else None)
        if r.status >= 500:
            raise Offline(f"{method} {path}: HTTP {r.status}")
        if r.status >= 400:
            raise Rejected(r.status, data.decode("utf-8", "replace"))
        return r.status, hdrs, data

    def get_json(self, path: str):
        return json.loads(self.request("GET", path)[2])

    def post_json(self, path: str, obj, method: str = "POST", signed: bool = True):
        return self.request(method, path, json.dumps(obj).encode(), signed=signed)

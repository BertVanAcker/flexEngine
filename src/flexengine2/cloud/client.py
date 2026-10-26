"""Device-side HTTP(S) client: signed requests, one TCP connection per request, typed failures.

Transport (EV-06, DL-4 alpha): ``https://`` URLs use TLS >= 1.2 with a context that trusts ONLY the configured
lab CA file (``ca_file`` / cfg ``caFile``) and verifies the hostname (``tls_server_name`` / cfg ``tlsServerName``
overrides the name checked for IP-addressed lab hosts). Without a CA file an https client is refused at construction
unless ``allow_system_trust`` (cfg ``allowSystemTrust: true``) is given explicitly. ``http://`` still works
(fake cloud in tests, loopback TLS proxy) but logs one warning per client. Ed25519 request signatures are unchanged.
"""

from __future__ import annotations

import http.client
import os
import json
import logging
import ssl
import time
from urllib.parse import urlsplit

from flexengine2.identity import crypto

log = logging.getLogger("flexengine2.cloud.client")


class Offline(Exception):
    """Cloud not reachable / 5xx / 429. Retry later; never a reason to change local state."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class TlsError(Offline):
    """TLS handshake/verification failed (unknown CA, wrong hostname, expired cert, protocol). Still an Offline so
    the engine keeps retrying, but distinct so status and logs show the reason."""


class Rejected(Exception):
    """Cloud answered with a definite 4xx."""

    def __init__(self, status: int, body: str):
        super().__init__(f"{status}: {body[:200]}")
        self.status = status


class TlsConfigError(ValueError):
    """Unsafe or incomplete TLS configuration (e.g. https without a pinned CA)."""


def make_ssl_context(ca_file: str | None, allow_system_trust: bool = False) -> ssl.SSLContext:
    """Client context: TLS >= 1.2, CERT_REQUIRED, hostname check; trusts only `ca_file` when given."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # check_hostname=True, CERT_REQUIRED, no CAs loaded
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca_file:
        ctx.load_verify_locations(cafile=str(ca_file))
    elif allow_system_trust:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    else:
        raise TlsConfigError("https requires a pinned CA (caFile); set allowSystemTrust: true to use the system store")
    return ctx


class _HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose certificate is checked against `server_hostname` instead of the connect host."""

    def __init__(self, host, port, *, timeout, context, server_hostname=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._server_hostname = server_hostname

    def connect(self):
        if not self._server_hostname:
            return super().connect()
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


PLAIN_HTTP_ENV = "FLEX_ALLOW_PLAIN_HTTP"


def plain_http_allowed(cfg: dict | None = None) -> bool:
    """Plain http:// to the control plane only with an explicit lab flag (project lead, 2026-10-17): config
    `allowPlainHttp: true`, or FLEX_ALLOW_PLAIN_HTTP=1 in the engine's environment (test suites, simulation)."""
    return bool((cfg or {}).get("allowPlainHttp")) or os.environ.get(PLAIN_HTTP_ENV) == "1"


def from_cfg_kwargs(cfg: dict) -> dict:
    """CloudClient keyword arguments taken from an engine cfg (caFile, tlsServerName, allowSystemTrust,
    allowPlainHttp)."""
    return {"ca_file": cfg.get("caFile"), "tls_server_name": cfg.get("tlsServerName"),
            "allow_system_trust": bool(cfg.get("allowSystemTrust", False)),
            "allow_plain_http": plain_http_allowed(cfg)}


class CloudClient:
    def __init__(self, base_url: str, device_id: str, key, timeout: float = 5.0, *, ca_file: str | None = None,
                 tls_server_name: str | None = None, allow_system_trust: bool = False,
                 allow_plain_http: bool | None = None):
        u = urlsplit(base_url)
        self.scheme = (u.scheme or "http").lower()
        if self.scheme not in ("http", "https"):
            raise TlsConfigError(f"unsupported URL scheme {u.scheme!r}")
        self.host, self.port = u.hostname, u.port or (443 if self.scheme == "https" else 80)
        self.device_id, self.key, self.timeout = device_id, key, timeout
        self.tls_server_name = tls_server_name
        self.ssl_context: ssl.SSLContext | None = None
        if self.scheme == "https":
            self.ssl_context = make_ssl_context(ca_file, allow_system_trust)
        else:
            if not (plain_http_allowed() if allow_plain_http is None else allow_plain_http):
                raise TlsConfigError(f"plain HTTP refused for {base_url}: use https:// with caFile, or set "
                                     f"allowPlainHttp: true (lab/test only)")
            log.warning("plain HTTP: lab/test only (%s)", base_url)
        self.bytes_in = 0
        self.requests = 0

    def _connection(self) -> http.client.HTTPConnection:
        if self.ssl_context is None:
            return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        return _HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.ssl_context,
                                server_hostname=self.tls_server_name)

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
            c = self._connection()
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            c.close()
        except ssl.SSLCertVerificationError as e:
            raise TlsError(f"{method} {path}: TLS verification failed: {e.verify_message or e}") from None
        except ssl.SSLError as e:
            raise TlsError(f"{method} {path}: TLS error: {e}") from None
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

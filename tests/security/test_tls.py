"""EV-12 TLS subset: the device client trusts only the pinned lab CA and checks the hostname (DL-4 alpha)."""

from __future__ import annotations

import logging

import pytest

from flexengine2.cloud.client import CloudClient, Offline, TlsConfigError, TlsError
from flexengine2.identity import crypto

KEY = crypto.generate()


def client(port: int, ca, host: str = "127.0.0.1", **kw) -> CloudClient:
    return CloudClient(f"https://{host}:{port}", "dev-a", KEY, timeout=3, ca_file=str(ca), **kw)


def test_valid_ca_ok_and_signed(pki, tls_server):
    c = client(tls_server("good"), pki.ca)
    got = c.get_json("/api/v1/ping")
    assert got == {"method": "GET", "path": "/api/v1/ping", "body": "", "signed": True}
    st, _, _ = c.post_json("/x", {"a": 1}, signed=False)
    assert st == 200 and c.requests == 2 and c.bytes_in > 0


def test_dns_name_via_localhost(pki, tls_server):
    assert client(tls_server("good"), pki.ca, host="localhost").get_json("/")["path"] == "/"


def test_wrong_ca_refused(pki, tls_server):
    port = tls_server("rogue")
    with pytest.raises(TlsError, match="verif"):
        client(port, pki.ca).get_json("/")
    # and the good server is refused when the client pins the other CA
    with pytest.raises(TlsError):
        client(tls_server("good"), pki.other_ca).get_json("/")


def test_wrong_hostname_refused(pki, tls_server):
    with pytest.raises(TlsError, match="(?i)hostname|match"):
        client(tls_server("wronghost"), pki.ca).get_json("/")


def test_server_name_override(pki, tls_server):
    port = tls_server("wronghost")
    assert client(port, pki.ca, tls_server_name="elsewhere.lab").get_json("/")["path"] == "/"
    with pytest.raises(TlsError):
        client(tls_server("good"), pki.ca, tls_server_name="elsewhere.lab").get_json("/")


def test_expired_refused(pki, tls_server):
    with pytest.raises(TlsError, match="(?i)expired"):
        client(tls_server("expired"), pki.ca).get_json("/")


def test_https_without_ca_refused_at_construction():
    with pytest.raises(TlsConfigError, match="pinned CA"):
        CloudClient("https://cloud.lab", "dev-a", KEY)


def test_system_trust_only_when_explicit(pki, tls_server):
    c = CloudClient(f"https://127.0.0.1:{tls_server('good')}", "dev-a", KEY, timeout=3, allow_system_trust=True)
    assert c.port and c.ssl_context.minimum_version.name == "TLSv1_2"
    with pytest.raises(TlsError):  # lab CA is not in the system store
        c.get_json("/")


def test_context_pins_only_ca(pki):
    c = client(443, pki.ca)
    ctx = c.ssl_context
    assert ctx.check_hostname and ctx.verify_mode.name == "CERT_REQUIRED"
    assert [x["subject"] for x in ctx.get_ca_certs()] == [((("commonName", "flexengine test CA"),),)]


def test_tls_error_is_offline():
    assert issubclass(TlsError, Offline)


def test_tls_error_caught_as_offline(pki, tls_server):
    try:
        client(tls_server("rogue"), pki.ca).get_json("/")
    except Offline as e:  # the engine's retry path
        assert isinstance(e, TlsError) and "TLS" in str(e)
    else:
        pytest.fail("no error")


def test_plain_http_warns_once_per_client(caplog):
    with caplog.at_level(logging.WARNING, logger="flexengine2.cloud.client"):
        c = CloudClient("http://127.0.0.1:1", "dev-a", KEY, timeout=0.5)
        for _ in range(2):
            with pytest.raises(Offline) as ei:
                c.get_json("/")
            assert not isinstance(ei.value, TlsError)
    msgs = [r.getMessage() for r in caplog.records if "plain HTTP" in r.getMessage()]
    assert len(msgs) == 1 and "lab/test only" in msgs[0]

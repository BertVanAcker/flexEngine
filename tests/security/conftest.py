"""Layer-4 security fixtures: a throwaway PKI generated with `cryptography` at test time and a local TLS server.

Certificates: `ca` (trusted), `other_ca` (not trusted), and leaves signed by `ca` for localhost/127.0.0.1
(`good`), for another name only (`wronghost`), and one already expired (`expired`); `rogue` is a valid-looking
localhost leaf signed by `other_ca`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = dt.datetime.now(dt.timezone.utc)


def _pem_key(k) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())


def _ca(cn: str):
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
            .sign(k, hashes.SHA256()))
    return cert, k


def _leaf(ca, cak, dns: list[str], ips: list[str], *, expired: bool = False):
    k = ec.generate_private_key(ec.SECP256R1())
    nb, na = ((NOW - dt.timedelta(days=10), NOW - dt.timedelta(days=1)) if expired
              else (NOW - dt.timedelta(days=1), NOW + dt.timedelta(days=30)))
    sans = [x509.DNSName(n) for n in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (dns or ips)[0])]))
            .issuer_name(ca.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(nb).not_valid_after(na)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(cak.public_key()), critical=False)
            .sign(cak, hashes.SHA256()))
    return cert, k


@dataclass
class Pki:
    dir: Path
    ca: Path
    other_ca: Path

    def pair(self, name: str) -> tuple[Path, Path]:
        return self.dir / f"{name}.crt", self.dir / f"{name}.key"


@pytest.fixture(scope="session")
def pki(tmp_path_factory) -> Pki:
    d = tmp_path_factory.mktemp("pki")
    ca, cak = _ca("flexengine test CA")
    oca, ocak = _ca("some other CA")
    for name, (c, k) in {"ca": (ca, cak), "other_ca": (oca, ocak)}.items():
        (d / f"{name}.crt").write_bytes(c.public_bytes(serialization.Encoding.PEM))
    leaves = {
        "good": _leaf(ca, cak, ["localhost", "cloud.lab"], ["127.0.0.1"]),
        "wronghost": _leaf(ca, cak, ["elsewhere.lab"], []),
        "expired": _leaf(ca, cak, ["localhost"], ["127.0.0.1"], expired=True),
        "rogue": _leaf(oca, ocak, ["localhost"], ["127.0.0.1"]),
    }
    for name, (c, k) in leaves.items():
        (d / f"{name}.crt").write_bytes(c.public_bytes(serialization.Encoding.PEM))
        (d / f"{name}.key").write_bytes(_pem_key(k))
    return Pki(d, d / "ca.crt", d / "other_ca.crt")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        out = json.dumps({"method": self.command, "path": self.path, "body": body.decode(),
                          "signed": bool(self.headers.get("X-Flex-Signature"))}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _reply


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # handshake failures are expected in these tests
        pass


@pytest.fixture
def tls_server(pki):
    """Factory: tls_server("good") -> port of a local HTTPS server presenting that leaf certificate."""
    servers = []

    def start(leaf: str) -> int:
        crt, key = pki.pair(leaf)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(crt, key)
        srv = _Server(("127.0.0.1", 0), _Handler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()

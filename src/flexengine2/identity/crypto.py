"""Ed25519 keys, detached signatures and signed HTTP requests.

Three key roles (all Ed25519):
  * release key   - CI/release pipeline signs flexNode release manifests (trust anchor on every device)
  * cloud key     - flexCloud signs each desired-state manifest it serves (trust anchor on every device)
  * device key    - generated on the device at first start; the public half is registered at onboarding
                    and every device request carries a signature over method, path, time, nonce, body hash
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from flexengine2.util import atomic_write_bytes


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def generate() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def public_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def key_id(pub_b64: str) -> str:
    return "ed25519:" + hashlib.sha256(unb64(pub_b64)).hexdigest()[:16]


def load_or_create(path: str | Path) -> Ed25519PrivateKey:
    path = Path(path)
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(unb64(path.read_text().strip()))
    key = generate()
    atomic_write_bytes(path, b64(private_bytes(key)).encode(), mode=0o600)
    return key


def load_private(path: str | Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(unb64(Path(path).read_text().strip()))


def load_public(text_b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(unb64(text_b64))


def sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return b64(key.sign(data))


def verify(pub: Ed25519PublicKey | str, data: bytes, sig_b64: str) -> bool:
    if isinstance(pub, str):
        pub = load_public(pub)
    try:
        pub.verify(unb64(sig_b64), data)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---- signed device requests ---------------------------------------------------------------------

def request_string(method: str, path: str, date_ms: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, date_ms, nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign_request(key: Ed25519PrivateKey, device_id: str, method: str, path: str, body: bytes,
                 date_ms: int) -> dict[str, str]:
    nonce = os.urandom(12).hex()
    sig = sign(key, request_string(method, path, str(date_ms), nonce, body))
    return {"X-Flex-Device": device_id, "X-Flex-Date": str(date_ms), "X-Flex-Nonce": nonce,
            "X-Flex-Signature": sig}

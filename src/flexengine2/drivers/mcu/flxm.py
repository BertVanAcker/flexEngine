"""FLXM v1 container for flexEngine-MCU modules (see zephyr/base/include/flex_mcu_abi.h).

    [flexm_hdr 160 B][LLEXT ELF][sig TLV: type u16, len u16][ECDSA-P256 r||s over SHA-256(hdr||ELF)]
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.exceptions import InvalidSignature

MAGIC = 0x4D584C46
HDR_SIZE = 160
FMT_VERSION = 1
SIG_ECDSA_P256_SHA256 = 1
_HDR = struct.Struct("<IHHHHBBHIIII24s16s32s32sI20s")
assert _HDR.size == HDR_SIZE

FLOAT_ABI = {"soft": 1, "hard": 2}
ARCH = {"armv7m": 1, "armv8m.main": 2}
CAPS = {"pwm": 1 << 0, "localbus": 1 << 1, "heap": 1 << 2, "fpu": 1 << 3, "zenoh": 1 << 4}


def caps_mask(names) -> int:
    m = 0
    for n in names:
        if n not in CAPS:
            raise ValueError(f"unknown MCU capability {n!r}")
        m |= CAPS[n]
    return m


def caps_names(mask: int) -> list[str]:
    return [k for k, v in CAPS.items() if mask & v]


def iface_bytes(h: str) -> bytes:
    """FLEX01_<64 hex> -> 32 bytes."""
    if not h.startswith("FLEX01_") or len(h) != 71:
        raise ValueError(f"not a FLEX01 hash: {h!r}")
    return bytes.fromhex(h[7:])


@dataclass
class Header:
    abi_major: int
    abi_minor: int
    float_abi: int
    arch: int
    caps_required: int
    elf_size: int
    ram_estimate: int
    step_period_ms: int
    name: str
    version: str
    iface_hash: bytes
    elf_sha256: bytes
    flags: int = 0
    test_window_ms: int = 0     # confirm window (model extensions.mcu.test_window_ms); 0 = base default
    magic: int = MAGIC
    hdr_size: int = HDR_SIZE
    fmt_version: int = FMT_VERSION

    def pack(self) -> bytes:
        return _HDR.pack(self.magic, self.hdr_size, self.fmt_version, self.abi_major, self.abi_minor,
                         self.float_abi, self.arch, self.flags, self.caps_required, self.elf_size,
                         self.ram_estimate, self.step_period_ms, self.name.encode(), self.version.encode(),
                         self.iface_hash, self.elf_sha256, self.test_window_ms, b"\0" * 20)

    @classmethod
    def unpack(cls, b: bytes) -> "Header":
        (magic, hs, fv, amaj, amin, fa, arch, flags, caps, esz, ram, per, name, ver, ih, sha, win,
         _) = _HDR.unpack(b[:HDR_SIZE])
        return cls(amaj, amin, fa, arch, caps, esz, ram, per, name.rstrip(b"\0").decode(), ver.rstrip(b"\0").decode(),
                   ih, sha, flags, win, magic, hs, fv)

    @property
    def iface(self) -> str:
        return "FLEX01_" + self.iface_hash.hex()

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "abi": f"{self.abi_major}.{self.abi_minor}",
                "floatAbi": {v: k for k, v in FLOAT_ABI.items()}.get(self.float_abi, "?"),
                "arch": {v: k for k, v in ARCH.items()}.get(self.arch, "?"), "caps": caps_names(self.caps_required),
                "elfSize": self.elf_size, "ramEstimate": self.ram_estimate, "stepPeriodMs": self.step_period_ms,
                "testWindowMs": self.test_window_ms,
                "interfaceHash": self.iface, "elfSha256": self.elf_sha256.hex()}


@dataclass
class Module:
    header: Header
    elf: bytes
    sig: bytes | None
    raw: bytes = field(repr=False, default=b"")


def load_private(pem: Path):
    return serialization.load_pem_private_key(Path(pem).read_bytes(), password=None)


def raw_public(key) -> bytes:
    pub = key.public_key() if hasattr(key, "public_key") else key
    n = pub.public_numbers()
    return n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")


def public_from_raw(raw: bytes):
    return ec.EllipticCurvePublicNumbers(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"),
                                         ec.SECP256R1()).public_key()


def pack(header: Header, elf: bytes, key=None) -> bytes:
    header.elf_size = len(elf)
    header.elf_sha256 = hashlib.sha256(elf).digest()
    body = header.pack() + elf
    if key is None:
        return body
    der = key.sign(body, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return body + struct.pack("<HH", SIG_ECDSA_P256_SHA256, 64) + r.to_bytes(32, "big") + s.to_bytes(32, "big")


def parse(data: bytes) -> Module:
    if len(data) < HDR_SIZE:
        raise ValueError("shorter than an FLXM header")
    h = Header.unpack(data)
    if h.magic != MAGIC or h.hdr_size != HDR_SIZE or h.fmt_version != FMT_VERSION:
        raise ValueError("not an FLXM v1 container")
    end = HDR_SIZE + h.elf_size
    if end > len(data):
        raise ValueError("truncated ELF")
    elf = data[HDR_SIZE:end]
    sig = None
    if len(data) >= end + 4 + 64:
        t, ln = struct.unpack_from("<HH", data, end)
        if t == SIG_ECDSA_P256_SHA256 and ln == 64:
            sig = data[end + 4:end + 68]
    return Module(h, elf, sig, data)


def verify(mod: Module, pub) -> tuple[bool, str]:
    if hashlib.sha256(mod.elf).digest() != mod.header.elf_sha256:
        return False, "ELF SHA-256 mismatch (tampered)"
    if mod.sig is None:
        return False, "module not signed"
    der = encode_dss_signature(int.from_bytes(mod.sig[:32], "big"), int.from_bytes(mod.sig[32:], "big"))
    try:
        pub.verify(der, mod.raw[:HDR_SIZE + mod.header.elf_size], ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False, "bad module signature"
    return True, ""


def c_pubkey_header(pem: Path, out: Path) -> None:
    raw = raw_public(load_private(pem))
    body = ", ".join(f"0x{b:02x}" for b in raw)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("/* generated by flexp3.flxm: module signing public key (P-256, X||Y) */\n#pragma once\n"
                   "#include <stdint.h>\nstatic const uint8_t flex_module_pubkey[64] = {" + body + "};\n")


if __name__ == "__main__":
    import sys
    c_pubkey_header(Path(sys.argv[1]), Path(sys.argv[2]))

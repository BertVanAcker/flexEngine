"""The host ports the e2e packages publish lie outside the host's ephemeral port range (no Docker needed).

A published port inside that range can be held at any time by an unrelated socket as its local port; `docker run -p`
then fails with "address already in use", the engine fails the release (a failed release is never retried) and every
deployment that depends on it stays held: the e2e and ordering tests then time out with nothing to show for it."""

from __future__ import annotations

import configparser
import re
import shlex
from pathlib import Path

import yaml

from .flexialab import HOST_PORTS, PACKAGES


def ephemeral_range() -> tuple[int, int]:
    try:
        lo, hi = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
        return min(int(lo), 49152), 65535   # also never the IANA range other hosts (macOS) allocate from
    except (OSError, ValueError):
        return 32768, 65535                 # Linux default low end and the IANA high end


def published_ports() -> dict[str, int]:
    out = {}
    ini = configparser.ConfigParser(inline_comment_prefixes=("#",))
    ini.read(PACKAGES / "flcpy-docker" / "flexIA.ini")
    args = shlex.split(shlex.split(ini["run:edge1"]["arguments"])[0])
    for i, a in enumerate(args):
        if a == "-p":
            out["flcpy-docker -p"] = int(args[i + 1].split(":")[-2])
    compose = yaml.safe_load((PACKAGES / "compose-two" / "compose.yaml").read_text())
    for svc, spec in compose["services"].items():
        for p in spec.get("ports") or []:
            out[f"compose-two {svc}"] = int(re.split(r":", str(p))[-2])
    return out


def test_published_ports_are_outside_the_ephemeral_range():
    lo, hi = ephemeral_range()
    ports = published_ports()
    assert sorted(ports.values()) == sorted(HOST_PORTS), (ports, HOST_PORTS)   # the allow-list names exactly these
    inside = {k: p for k, p in ports.items() if lo <= p <= hi}
    assert not inside, f"published host ports inside the ephemeral range {lo}-{hi}: {inside}"

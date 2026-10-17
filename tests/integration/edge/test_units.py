"""Unit checks of the edge pieces that need no cloud, on flexEngineV2 (port of demo/edge/tests/test_units.py): relay
port map, plc-verify preflight, one-process build."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from flexengine2.drivers.base import SPI_VERSION
from flexengine2.drivers.plc.verify import PlcVerifyDriver
from flexengine2.gateway.relay import find_port, mcu_ports
from flexengine2.host import build

from .edgelab import DP
from flexmodel.codegen.nodes.mcu_module import topic_id
from flexmodel.ihash import all_hashes
from flexmodel.workspace import Workspace


def test_relay_ports_match_the_generated_module_topics():
    ws = Workspace.load(DP)
    ports = mcu_ports(ws, "d_pick_lab", "portenta")
    by = {(p.instance, p.port): p for p in ports}
    th = all_hashes(ws)["types"]
    assert by[("gripper", "state")].topic == topic_id(th["uc2/GripperState"])
    assert by[("gripper", "command")].pattern == "rpc" and by[("gripper", "command")].response == "uc2/GripAck"
    assert by[("gripper", "command.response")].topic == topic_id(th["uc2/GripAck"])
    assert by[("servo", "joint")].key.startswith("0/servo/joint/uc2::msg::dds_::JointSample_/RIHS01_")
    assert find_port(ports, "gripper", "joint").direction == "in"
    with pytest.raises(KeyError):
        find_port(ports, "gripper", "state")
    assert {p.instance for p in ports} == {"gripper", "servo"}   # only the instances allocated to the MCU


def plc_payload(tmp: Path, spec: dict) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = json.dumps(spec).encode()
        ti = tarfile.TarInfo("flex-plc.json")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    p = tmp / "plc.tgz"
    p.write_bytes(buf.getvalue())
    return p


def test_plc_verify_preflight_refusals(tmp_path):
    h = "FLEX01_" + "11" * 32
    drv = PlcVerifyDriver("panda", tmp_path, {"plcs": {"panda/cx": {"via": "ads"}}})
    dep = {"spec": {"parameters": {}}}

    def rel(spec, desc_hash=h, plc=None, ann=None):
        return SimpleNamespace(payload_path=str(plc_payload(tmp_path, spec)),
                               descriptor={"interfaceHash": desc_hash, "plc": plc or {"download": False}},
                               payload_annotations=ann or {"dev.flex.ifaceHash": desc_hash})

    assert drv.preflight(rel({"target": "panda/cx", "via": "ads", "ifaceHash": h}), dep) is None
    assert "not configured" in drv.preflight(rel({"target": "opta", "via": "modbus", "ifaceHash": h}), dep)
    assert "differs from the release descriptor" in drv.preflight(
        rel({"target": "panda/cx", "via": "ads", "ifaceHash": "FLEX01_" + "22" * 32}), dep)
    assert "never downloads" in drv.preflight(rel({"target": "panda/cx", "via": "ads", "ifaceHash": h},
                                                  plc={"download": True}), dep)
    assert "deployment expects" in drv.preflight(rel({"target": "panda/cx", "via": "ads", "ifaceHash": h}),
                                                 {"spec": {"parameters": {"_interfaceHash": "FLEX01_" + "33" * 32}}})
    bad = SimpleNamespace(payload_path=str(tmp_path / "missing.tgz"), descriptor={}, payload_annotations={})
    assert "not a PLC verify artefact" in drv.preflight(bad, dep)


def test_one_process_hosts_every_driver_kind(tmp_path):
    """The LattePanda engine (container + plc-verify) and the UNO Q engine (applab, here with a CLI that is absent)
    are built from one config; the applab executor is exclusive (one App per board, plan-time placement)."""
    cfg = {"cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": "", "releasePublicKeys": []},
           "dataDir": str(tmp_path), "model": str(DP), "deployment": "d_pick_lab",
           "devices": [{"deviceId": "panda", "role": "gateway", "onboardingToken": "t",
                        "drivers": {"container": {"runtime": "podman"}, "plc-verify": {"plcs": {}}}},
                       {"deviceId": "unoq", "onboardingToken": "t",
                        "drivers": {"applab": {"board": "unoq", "cli": "/bin/false"}}}]}
    engines, relays = build(cfg)
    assert [e.device for e in engines] == ["panda", "unoq"] and relays == []
    assert set(engines[0].drivers) == {"container", "plc-verify"}
    assert set(engines[1].drivers) == {"applab"}
    caps = engines[1].capabilities()
    assert caps["x-flex"]["drivers"]["applab"]["exclusive"] is True
    # adapted: V2 advertises the frozen SPI version (EV-03) instead of the list of probe SPI extensions
    assert caps["x-flex"]["engine"]["spi"] == SPI_VERSION


def test_config_resolves_cloud_stack_info_and_keys(tmp_path, monkeypatch):
    from flexengine2.host import load_config
    from flexengine2.identity import crypto

    key = tmp_path / "release.key"
    crypto.load_or_create(key)
    (tmp_path / "stack.json").write_text(json.dumps({"internal": {"p2": "http://127.0.0.1:8700"},
                                                     "urls": {"p2": "https://lattepanda.lab:9700"},
                                                     "cloudPublicKey": "CLOUDKEY"}))
    (tmp_path / "tok").write_text("tok-123\n")
    monkeypatch.setenv("T", str(tmp_path))
    (tmp_path / "edge.yaml").write_text(
        "cloud: {stackInfo: ${T}/stack.json, releaseKeyFiles: [${T}/release.key]}\n"
        "dataDir: ${T}/data\ndevices:\n  - {deviceId: panda, onboardingTokenFile: ${T}/tok}\n")
    cfg = load_config(tmp_path / "edge.yaml")
    assert cfg["cloud"]["apiUrl"] == "http://127.0.0.1:8700" and cfg["cloud"]["cloudPublicKey"] == "CLOUDKEY"
    assert cfg["cloud"]["releasePublicKeys"] == [crypto.public_b64(crypto.load_private(key))]
    assert cfg["devices"][0]["onboardingToken"] == "tok-123"


def test_shm_reader_reads_the_sim_rt_host_header(tmp_path):
    """P10 sim RT host: RT_HDR magic at 0, iface_hash bytes at 64..96 of /dev/shm/<bridge>."""
    import struct

    from flexengine2.drivers.plc.verify import RT_MAGIC, ShmReader, Unreachable

    region = tmp_path / "pick_cell_bridge"
    hdr = bytearray(192)
    struct.pack_into("<I", hdr, 0, RT_MAGIC)
    hdr[64:96] = bytes(range(32))
    region.write_bytes(bytes(hdr))
    assert ShmReader({"path": str(region)}, {}).read_hash()[0] == "FLEX01_" + bytes(range(32)).hex()
    region.write_bytes(bytes(192))
    with pytest.raises(Unreachable):
        ShmReader({"path": str(region)}, {}).read_hash()


def test_otel_hook_is_off_by_default_and_instruments_with_flex_otel(tmp_path, monkeypatch):
    """IG-13 merged into the engine (demo/obs/patches/edge-engine-otel.patch): FLEX_OTEL=1 instruments every device
    engine (install/report wrapped), unset = untouched."""
    from flexengine2.host import _otel_hook

    class E:
        device, dir, state = "panda", str(tmp_path), {}
        drivers: dict = {}

        class client:
            @staticmethod
            def request(*a, **k):
                return 200, {}, b"{}"

        def install(self, *a):
            return None

        def health_gate(self, *a, **kw):
            return True, 0.0

        def report(self, *a, **k):
            return None

    monkeypatch.delenv("FLEX_OTEL", raising=False)
    e = E()
    orig = e.install
    assert _otel_hook([e]) is None and e.install == orig
    monkeypatch.setenv("FLEX_OTEL", "1")
    monkeypatch.setenv("FLEX_OTEL_FILE", str(tmp_path / "spans.jsonl"))
    otel = _otel_hook([e])
    assert otel is not None and e.install != orig

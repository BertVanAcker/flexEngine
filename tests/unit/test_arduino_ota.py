"""EV-38 unit tests: the arduino-ota protocol (signed OTA_UPDATE in flexCommunicator's command format, .ota header,
one-time image server), the wire clients against the in-process broker / Redis (and the real containers, marker
docker), and the driver's failure paths against the board simulator."""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from flexengine2 import contracts
from flexengine2.config import load as cl
from flexengine2.core.reconciler import Engine, build_drivers
from flexengine2.drivers import arduino_ota as ao
from flexengine2.drivers.arduino_ota import ANN_BOARD, ANN_FIRMWARE, ArduinoOtaDriver, OtaServer
from flexengine2.drivers.arduino_wire import MqttClient, RedisClient, WireError
from flexengine2.drivers.base import DriverError, Workload, wait_until
from flexengine2.testing import arduino_sim as asim
from flexengine2.testing.miniwire import MiniBroker, MiniRedis, topic_matches

from fe_unit_fakes import deployment

PORTENTA, OPTA = asim.PORTENTA, asim.OPTA


# ---- protocol -------------------------------------------------------------------------------------------------------
def flcpy_signature(key: bytes, c: dict) -> str:
    """flexCommunicator flcpy/security/security.py compute_signature(), with the node key as HMAC key."""
    data = (c["id"] + c["cmd"] + str(c["priority"]) + str(c["expiry"]) + json.dumps(c["params"], sort_keys=True) +
            str(c["timestamp"]) + c["nonce"])
    return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


def test_command_has_the_flexcommunicator_shape_and_signature():
    key = bytes(range(32))
    params = {"board": PORTENTA, "sha256": "ab" * 32, "size": 1234, "url": "http://10.0.0.1:8470/ota/t.ota",
              "version": "0.2.0"}
    c = ao.compose_command(key, params, expiry_seconds=60, now=1_700_000_000)
    assert set(c) == {"id", "cmd", "priority", "expiry", "params", "timestamp", "nonce", "signature"}
    assert c["cmd"] == "OTA_UPDATE" and c["expiry"] == 1_700_000_060 and c["timestamp"] == 1_700_000_000
    assert c["signature"] == flcpy_signature(key, c)
    assert ao.verify(key, c)
    # the canonical params string the patched library rebuilds (json.dumps(sort_keys=True) separators)
    assert json.dumps(params, sort_keys=True) == (
        '{"board": "arduino:mbed_portenta:envie_m7", "sha256": "' + "ab" * 32 + '", "size": 1234, '
        '"url": "http://10.0.0.1:8470/ota/t.ota", "version": "0.2.0"}')
    for k, v in [("params", {**params, "url": "http://evil/x.ota"}), ("expiry", c["expiry"] + 1), ("nonce", "0")]:
        assert not ao.verify(key, {**c, k: v})
    assert not ao.verify(bytes(32), c)
    assert not ao.verify(key, {"cmd": "OTA_UPDATE"})


def test_topic_and_keys():
    assert ao.cmd_topic("flexIA@demo-1fd420", "OTA_01") == "flexcloud/flexIA@demo-1fd420/OTA_01/cmd"
    assert ao.keys("c", "a") == {"status": "c:applications:a:status", "version": "c:applications:a:version",
                                 "logs": "c:applications:a:logs", "ota": "c:applications:a:ota",
                                 "heartbeat": "c:applications:a:heartbeat"}


def test_key_file_forms(tmp_path):
    key = bytes(range(0xae, 0xae + 32))
    (tmp_path / "hex").write_text(key.hex() + "\n")
    (tmp_path / "c").write_text("static const uint8_t K[] = {\n" + ", ".join(f"0x{b:02x}" for b in key) + "\n};\n")
    (tmp_path / "short").write_text("abcd")
    assert ao.read_key_file(tmp_path / "hex") == key == ao.read_key_file(tmp_path / "c")
    with pytest.raises(ValueError, match="at least 16"):
        ao.read_key_file(tmp_path / "short")


def test_ota_header_checks(tmp_path):
    img = asim.write_image(tmp_path / "ok.ota", "1.0.0")
    h = ao.ota_header(img, PORTENTA)
    assert h["magic"] == ao.OTA_MAGIC[PORTENTA] and h["size"] == img.stat().st_size
    with pytest.raises(ValueError, match="OTA image is for arduino:mbed_portenta:envie_m7, the board is arduino:mbed_opta:opta"):
        ao.ota_header(img, OPTA)
    data = bytearray(img.read_bytes())
    data[-1] ^= 1
    (tmp_path / "crc.ota").write_bytes(bytes(data))
    with pytest.raises(ValueError, match="CRC-32 mismatch"):
        ao.ota_header(tmp_path / "crc.ota", PORTENTA)
    (tmp_path / "len.ota").write_bytes(img.read_bytes() + b"x")
    with pytest.raises(ValueError, match="header length"):
        ao.ota_header(tmp_path / "len.ota")
    (tmp_path / "tiny.ota").write_bytes(b"x" * 5)
    with pytest.raises(ValueError, match="5 bytes"):
        ao.ota_header(tmp_path / "tiny.ota")
    with pytest.raises(ValueError, match="no OTA support known"):
        ao.ota_header(img, "esp32:esp32:m5stack_atoms3")


def test_image_matches_make_ota_container_layout(tmp_path):
    """Same container as flexNode-arduino tools/hwtest/make_ota.py: [len][crc32][magic][0 x7, 0x40][body]."""
    body = b"\x01\x02\x03"
    img = asim.ota_image(body, OPTA)
    length, crc, magic = struct.unpack("<III", img[:12])
    assert length == len(img) - 8 and magic == 0x23410064 and img[12:20] == bytes(7) + b"\x40" and img[20:] == body


# ---- one-time image server ------------------------------------------------------------------------------------------
def test_server_serves_each_token_once_and_only_to_the_board(tmp_path):
    img = asim.write_image(tmp_path / "a.ota", "1.0.0")
    srv = OtaServer("127.0.0.1", 0, None)
    try:
        tok, url = srv.offer(str(img), img.stat().st_size, 60)
        assert url == f"http://127.0.0.1:{srv.port}/ota/{tok}.ota" and len(tok) >= 32
        assert urllib.request.urlopen(url, timeout=5).read() == img.read_bytes()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url, timeout=5)
        assert e.value.code == 404 and srv.served == 1
        for bad in (f"http://127.0.0.1:{srv.port}/ota/guess.ota", f"http://127.0.0.1:{srv.port}/"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(bad, timeout=5)
        _, url2 = srv.offer(str(img), img.stat().st_size, 60, address="10.9.9.9")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url2, timeout=5)              # not the board's address
        tok3, url3 = srv.offer(str(img), img.stat().st_size, -1)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url3, timeout=5)              # expired
        tok4, url4 = srv.offer(str(img), img.stat().st_size, 60)
        srv.revoke(tok4)
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url4, timeout=5)
    finally:
        srv.close()


def test_server_public_url_placeholder(tmp_path):
    srv = OtaServer("127.0.0.1", 0, "http://panda.lan:{port}/")
    try:
        assert srv.public == f"http://panda.lan:{srv.port}"
    finally:
        srv.close()


# ---- wire clients ---------------------------------------------------------------------------------------------------
def _wire_roundtrip(mqtt_port: int, redis_port: int, db: int = 3):
    r = RedisClient("127.0.0.1", redis_port, db=db)
    r.set("k", "v")
    assert r.get("k") == "v" and r.get("missing") is None
    r.set("t", "x", ex=1)
    r.rpush("l", "a")
    r.rpush("l", "b")
    assert r.lrange("l") == ["a", "b"] and r.delete("k", "l") == 2 and r.ping()
    got = []
    sub = MqttClient("127.0.0.1", mqtt_port, "sub-1").connect()
    sub.subscribe("flexcloud/c/a/cmd")
    sub.start(lambda t, p: got.append((t, p)))
    pub = MqttClient("127.0.0.1", mqtt_port, "pub-1").connect()
    pub.publish("flexcloud/c/a/cmd", b'{"x":1}', qos=1)
    pub.publish("other/topic", b"no", qos=0)
    pub.close()
    end = time.time() + 5
    while not got and time.time() < end:
        time.sleep(0.02)
    sub.close()
    assert got == [("flexcloud/c/a/cmd", b'{"x":1}')]


def test_wire_clients_against_the_in_process_servers():
    b, r = MiniBroker().start(), MiniRedis().start()
    try:
        _wire_roundtrip(b.port, r.port)
        assert r.value("t", 3) == "x"
    finally:
        b.stop()
        r.stop()


@pytest.mark.docker
@pytest.mark.slow
def test_wire_clients_against_mosquitto_and_redis_containers():
    if not asim.Containers.available():
        pytest.skip("docker not available")
    c = asim.Containers("unit").start()
    try:
        _wire_roundtrip(c.mqtt_port, c.redis_port)
    finally:
        c.stop()


def test_wire_errors_are_wire_errors():
    import socket
    so = socket.socket()
    so.bind(("127.0.0.1", 0))
    p = so.getsockname()[1]
    so.close()
    with pytest.raises(WireError, match="unreachable"):
        MqttClient("127.0.0.1", p, "x", timeout=0.5).connect()
    with pytest.raises(WireError, match="unreachable"):
        RedisClient("127.0.0.1", p, timeout=0.5).get("k")
    r = MiniRedis(password="pw").start()
    try:
        with pytest.raises(WireError):
            RedisClient("127.0.0.1", r.port, password="nope").get("k")
        assert RedisClient("127.0.0.1", r.port, password="pw").get("k") is None
    finally:
        r.stop()


def test_topic_matching():
    assert topic_matches("a/+/c", "a/b/c") and topic_matches("a/#", "a/b/c") and not topic_matches("a/b", "a/b/c")


# ---- driver against the simulator -----------------------------------------------------------------------------------
@pytest.fixture
def lab(tmp_path):
    lab = asim.ArduinoLab(tmp_path / "lab").start()
    yield lab
    lab.stop()


@pytest.fixture
def mk(lab, tmp_path):
    made = []

    def make(**kw) -> ArduinoOtaDriver:
        d = ArduinoOtaDriver("dev1", tmp_path / "data", lab.driver_cfg(**kw))
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def wl(lab, tmp_path, version="1.0.0", dep="fw", fw=None, **flags) -> Workload:
    img = lab.image(tmp_path / "payloads", version, **flags)
    return Workload(dep, "board_fw", version, "sha256:" + hashlib.sha256(img.read_bytes()).hexdigest(), str(img),
                    {"dev.flex.profile": "arduino-ota", ANN_BOARD: lab.fqbn, ANN_FIRMWARE: fw or version})


@pytest.fixture
def engine_cfg(tmp_path):
    def make(**kw):
        return {"deviceId": "vec-dev", "dataDir": str(tmp_path / "engine"), "apiUrl": "http://127.0.0.1:9",
                "releasePublicKeys": [], "cloudPublicKey": "", "httpTimeout": 0.5, **kw}
    return make


def code(text: str) -> str:
    return contracts.reason_code(text)["code"]


def test_install_then_update_keeps_one_command_per_update(lab, mk, tmp_path):
    d = mk()
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    d.apply(None, wl(lab, tmp_path, "1.0.0"))                    # idempotent: nothing sent
    d.apply(wl(lab, tmp_path, "1.0.0"), wl(lab, tmp_path, "2.0.0"))
    sim = lab.sim()
    assert sim.firmware["version"] == "2.0.0" and sim.downloads == 2 and len(sim.commands) == 2
    o = d.status("fw")
    assert o.present and o.running and o.version == "2.0.0", o
    ota = json.loads(lab.redis.value(sim.k["ota"], 15))
    assert ota["state"] == "booted" and ota["running"] == "2.0.0" and ota["ref"] == sim.commands[-1]["id"]


@pytest.mark.parametrize("fault,expect", [("download", "download failed"), ("flip", "sha256 mismatch")])
def test_failed_transfer_keeps_the_old_firmware(lab, mk, tmp_path, fault, expect):
    d = mk()
    v1 = wl(lab, tmp_path, "1.0.0")
    d.apply(None, v1)
    lab.sim().fault = fault
    with pytest.raises(DriverError) as e:
        d.apply(v1, wl(lab, tmp_path, "2.0.0"))
    assert expect in str(e.value) and "it keeps firmware 1.0.0" in str(e.value) and code(str(e.value)) == "FE-AOTA-015"
    assert lab.sim().firmware["version"] == "1.0.0"
    lab.sim().fault = None
    d.apply(wl(lab, tmp_path, "2.0.0"), v1)                     # the engine's rollback: old firmware still runs
    assert lab.sim().downloads == (1 if fault == "download" else 2)
    o = d.status("fw")
    assert o.running and o.digest == v1.digest and d.failed("fw") is None


def test_corrupt_image_fails_in_decompress(lab, mk, tmp_path):
    d = mk()
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    bad = wl(lab, tmp_path, "3.0.0", corrupt=True)
    assert d.preflight(SimpleNamespace(payload_path=bad.payload_path, payload_annotations=bad.payload_annotations),
                       deployment("fw", driver="arduino-ota")) is None      # header fine: only the board finds out
    with pytest.raises(DriverError, match=r"decompress\(\) failed"):
        d.apply(None, bad)
    assert lab.sim().firmware["version"] == "1.0.0"


def test_board_that_never_comes_back_times_out(lab, mk, tmp_path):
    d = mk(updateTimeoutSeconds=2)
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    with pytest.raises(DriverError) as e:
        d.apply(None, wl(lab, tmp_path, "4.0.0", boots=False))
    assert "the board did not come back" in str(e.value) and "last state restarting" in str(e.value)
    assert code(str(e.value)) == "FE-AOTA-012"
    assert d.health("fw", {}) is False                          # heartbeat expired, status OTA_IN_PROGRESS


def test_unpatched_library_and_wrong_key_never_accept(lab, mk, tmp_path):
    d = mk(acceptTimeoutSeconds=0.6)
    d.apply(None, wl(lab, tmp_path, "1.0.0", remote_ota=False))   # installs 0.1.0-style firmware
    with pytest.raises(DriverError) as e:
        d.apply(None, wl(lab, tmp_path, "2.0.0"))
    assert "did not accept the OTA command" in str(e.value) and code(str(e.value)) == "FE-AOTA-013"
    lab.sim().reflash("1.0.0")
    lab.sim().wait_ready()
    (Path(lab.root) / "p1.key").write_text((b"\x01" * 32).hex())
    with pytest.raises(DriverError, match="did not accept"):
        mk(acceptTimeoutSeconds=0.6).apply(None, wl(lab, tmp_path, "2.0.0"))
    # The node may log the rejection after the driver has stopped waiting for an accept (under load): wait for it.
    def rejected() -> bool:
        return any(b"OTA_UPDATE rejected: bad signature" in x for x in lab.redis.db(15).get(lab.sim().k["logs"]) or [])
    assert wait_until(rejected, 3.0, 0.05)


def test_node_side_rejection_is_reported(lab, mk, tmp_path):
    """The node checks the board itself (a driver config naming the wrong FQBN for this node)."""
    d = mk()
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    lab.sim().fqbn = OPTA
    with pytest.raises(DriverError) as e:
        d.apply(None, wl(lab, tmp_path, "2.0.0"))
    assert str(e.value) == ("OTA_01 rejected the OTA command: image is for arduino:mbed_portenta:envie_m7, this node "
                            "is arduino:mbed_opta:opta") and code(str(e.value)) == "FE-AOTA-014"
    assert lab.sim().downloads == 1 and d.status("fw").version == "1.0.0"


def test_stalled_download_times_out(lab, mk, tmp_path):
    d = mk(updateTimeoutSeconds=1)
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    lab.sim().fault = "stall"                                    # accepted, never finishes the download
    with pytest.raises(DriverError, match="last state downloading.*did not come back"):
        d.apply(None, wl(lab, tmp_path, "2.0.0"))


def test_offline_board_fails_before_sending(lab, mk, tmp_path):
    d = mk()
    lab.sim().power_off()
    time.sleep(1.2)                                              # heartbeat TTL (3 x 0.3 s) runs out
    with pytest.raises(DriverError, match="is offline: no heartbeat key") as e:
        d.apply(None, wl(lab, tmp_path, "1.0.0"))
    assert code(str(e.value)) == "FE-AOTA-011" and lab.broker.published == []
    assert mk(requireHeartbeat=False).status("fw").present is False   # nothing of ours installed


def test_unhealthy_firmware_fails_fast_and_rollback_reserves(lab, mk, tmp_path):
    d = mk()
    v2, v3 = wl(lab, tmp_path, "2.0.0"), wl(lab, tmp_path, "3.0.0", healthy=False)
    d.apply(None, v2)
    d.apply(v2, v3)                                              # boots: apply succeeds
    assert d.health("fw", {}) is False
    why = d.failed("fw")
    assert why == "OTA_01 reports status ERROR on firmware 3.0.0" and code(why) == "FE-AOTA-019"
    d.apply(v3, v2)                                              # rollback = re-serve 2.0.0
    assert lab.sim().downloads == 3 and d.health("fw", {}) and d.failed("fw") is None


def test_preflight_refusals(lab, mk, tmp_path):
    d = mk()
    good = wl(lab, tmp_path, "1.0.0")
    rel = lambda **ann: SimpleNamespace(payload_path=good.payload_path,  # noqa: E731
                                        payload_annotations={**good.payload_annotations, **ann})
    dep = deployment("fw", driver="arduino-ota")
    assert d.preflight(rel(), dep) is None
    cases = {
        "FE-AOTA-PRE-003": d.preflight(rel(**{ANN_BOARD: OPTA}), dep),
        "FE-AOTA-PRE-002": d.preflight(SimpleNamespace(payload_path=good.payload_path, payload_annotations={
            "dev.flex.profile": "arduino-ota", ANN_FIRMWARE: "1"}), dep),
        "FE-AOTA-PRE-004": d.preflight(SimpleNamespace(payload_path=good.payload_path, payload_annotations={
            "dev.flex.profile": "arduino-ota", ANN_BOARD: PORTENTA}), dep),
    }
    other = deployment("fw", driver="arduino-ota")
    other["spec"]["parameters"] = {"board": "nope"}
    cases["FE-AOTA-PRE-001"] = d.preflight(rel(), other)
    opta_img = asim.write_image(tmp_path / "opta.ota", "1.0.0", fqbn=OPTA)
    cases["FE-AOTA-PRE-005"] = d.preflight(SimpleNamespace(payload_path=str(opta_img),
                                                           payload_annotations=good.payload_annotations), dep)
    for want, r in cases.items():
        assert r and code(r) == want, (want, r)
    (Path(lab.root) / "p1.key").write_text("zz")
    r = d.preflight(rel(), dep)
    assert r.startswith("arduino-ota: board p1: unusable command key file") and code(r) == "FE-AOTA-003"


def test_serve_config_is_checked(lab, tmp_path):
    w = wl(lab, tmp_path, "1.0.0")
    d = ArduinoOtaDriver("dev1", tmp_path / "d1", lab.driver_cfg(serve={"host": "127.0.0.1", "port": 0}))
    with pytest.raises(DriverError) as e:
        d.apply(None, w)
    assert code(str(e.value)) == "FE-AOTA-005"
    d = ArduinoOtaDriver("dev1", tmp_path / "d2", lab.driver_cfg(serve={"allowPlainHttp": True}))
    with pytest.raises(DriverError) as e:
        d.apply(None, w)
    assert code(str(e.value)) == "FE-AOTA-006"


def test_two_boards_parameter_board_and_placement(tmp_path):
    lab = asim.ArduinoLab(tmp_path / "lab", boards=("left", "right")).start()
    d = ArduinoOtaDriver("dev1", tmp_path / "data", lab.driver_cfg())
    try:
        assert d.max_deployments == 2 and d.capabilities()["maxDeployments"] == 2
        assert d.platform() == {ANN_BOARD: PORTENTA}
        a, b = wl(lab, tmp_path, "1.0.0", dep="fw-l"), wl(lab, tmp_path, "2.0.0", dep="fw-r")
        a.parameters, b.parameters = {"board": "left"}, {"board": "right"}
        with pytest.raises(DriverError) as e:
            d.apply(None, wl(lab, tmp_path, "1.0.0", dep="x"))  # two boards: the parameter is needed
        assert code(str(e.value)) == "FE-AOTA-004"
        d.apply(None, a)
        d.apply(None, b)
        assert lab.sim("left").firmware["version"] == "1.0.0" and lab.sim("right").firmware["version"] == "2.0.0"
        c = wl(lab, tmp_path, "2.0.0", dep="fw-x")
        c.parameters = {"board": "left"}
        with pytest.raises(DriverError) as e:
            d.apply(None, c)
        assert code(str(e.value)) == "FE-AOTA-009"
        assert set(d.list()) == {"fw-l", "fw-r"}
    finally:
        d.close()
        lab.stop()


def test_platform_none_for_mixed_boards(tmp_path):
    cfg = {"boards": {"a": {"fqbn": PORTENTA}, "b": {"fqbn": OPTA}}}
    assert ArduinoOtaDriver("d", tmp_path, cfg).platform() is None


def test_remove_forgets_the_board_and_drift_is_detected(lab, mk, tmp_path):
    d = mk()
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    lab.sim().reflash("0.0.9-usb")
    lab.sim().wait_ready()
    o = d.status("fw")
    assert o.present is False and "0.0.9-usb" in o.detail
    d.remove("fw")
    d.remove("fw")
    assert d.list() == {} and lab.sim().firmware["version"] == "0.0.9-usb"


def test_capabilities_fragment_validates(lab, mk, tmp_path, engine_cfg):
    d = mk()
    d.apply(None, wl(lab, tmp_path, "1.0.0"))
    doc = Engine(engine_cfg(), {"arduino-ota": d}).capabilities()
    contracts.validate("capabilities", doc)
    frag = doc["x-flex"]["drivers"]["arduino-ota"]
    assert frag["boards"]["p1"] == {"fqbn": PORTENTA, "node": "OTA_01", "reachable": True, "version": "1.0.0",
                                    "status": "RUNNING", "online": True}
    lab.redis.stop()
    frag = ArduinoOtaDriver("dev1", tmp_path / "data", lab.driver_cfg()).capabilities()
    assert frag["boards"]["p1"]["reachable"] is False
    contracts.validate("capabilities", Engine(engine_cfg(), {"arduino-ota": ArduinoOtaDriver(
        "dev1", tmp_path / "data", lab.driver_cfg())}).capabilities())


def test_config_validates_and_builds_the_driver(tmp_path):
    drv = {"boards": {"p1": {"fqbn": PORTENTA, "connectorId": "c", "applicationId": "OTA_01",
                             "commandKeyFile": "/k"}}, "serve": {"allowPlainHttp": True, "publicUrl": "http://h:1"}}
    cfg = {"cloud": {"apiUrl": "http://127.0.0.1:1", "cloudPublicKey": "A" * 43 + "=", "releasePublicKeys": []},
           "dataDir": str(tmp_path), "devices": [{"deviceId": "panda", "onboardingToken": "t",
                                                  "drivers": {"arduino-ota": drv}}]}
    assert cl.validate(cfg) == []
    out = build_drivers({"deviceId": "panda", "drivers": {"arduino-ota": drv, "native": False}}, tmp_path)
    assert isinstance(out["arduino-ota"], ArduinoOtaDriver) and set(out) == {"arduino-ota"}

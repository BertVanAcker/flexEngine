"""Every flex step type against a stand-in of what it talks to on the desk: an HTTP(S) server, commands, the CP-7
local API (status/history documents valid against cli-status.v1), a Zenoh peer, the fake SMP MCU of the contract
suite, the pyads `Hello` and pymodbus stand-ins of demo/edge/sim, a fake ssh/journalctl, TCP ports going down/up."""

from __future__ import annotations

import json
import os
import socket
import ssl
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import flexsteps as f

from conftest import Ctx

PASS, FAIL, SKIP = f.PASS, f.FAIL, f.SKIP
FLEXENGINE = Path(__file__).resolve().parents[2]
REPO = FLEXENGINE.parent
A = f.ACTIONS


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- an HTTP stand-in: fixed documents per path, bearer token check --------------------------------------------------
class Api:
    def __init__(self, docs: dict, token: str | None = None, tls: dict | None = None):
        self.docs, self.token, self.hits = docs, token, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, method):
                path = self.path.split("?", 1)[0]
                outer.hits.append((method, self.path, self.headers.get("Authorization")))
                if outer.token and self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"title": "unauthorized"})
                doc = outer.docs.get((method, path), outer.docs.get(path))
                if callable(doc):
                    doc = doc(self.path)
                if doc is None:
                    return self._send(404, {"title": "not found"})
                code, body = doc if isinstance(doc, tuple) else (200, doc)
                self._send(code, body)

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply("GET")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.body = self.rfile.read(n)
                self._reply("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        scheme = "http"
        if tls:
            c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            c.load_cert_chain(str(tls["cert"]), str(tls["key"]))
            self.httpd.socket = c.wrap_socket(self.httpd.socket, server_side=True)
            scheme = "https"
        self.url = f"{scheme}://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api():
    made = []

    def make(docs, token=None, tls=None):
        a = Api(docs, token, tls)
        made.append(a)
        return a
    yield make
    for a in made:
        a.close()


def tokfile(tmp_path, value="t0k"):
    p = tmp_path / "token"
    p.write_text(value + "\n")
    return str(p)


# ---- http_expect ----------------------------------------------------------------------------------------------------
def test_http_expect_json_match_token_and_save(runner, ctx, api, tmp_path):
    a = api({"/v1/health": {"ok": True, "ticks": 7}}, token="t0k")
    st, d = A["http_expect"](ctx, {"url": f"{a.url}/v1/health", "token_file": tokfile(tmp_path),
                                   "json_match": {"/ok": True, "/ticks": {"gt": 0}}, "save": {"ticks": "/ticks"}})
    assert st == PASS and "saved ticks=7" in d and ctx.vars["ticks"] == "7"
    assert a.hits[-1][2] == "Bearer t0k"
    st, d = A["http_expect"](ctx, {"url": f"{a.url}/v1/health", "status": 401, "timeout": 1})
    assert st == PASS
    st, d = A["http_expect"](ctx, {"url": f"{a.url}/v1/health", "token_file": tokfile(tmp_path),
                                   "json_match": {"/ok": False}, "timeout": 0.5})
    assert st == FAIL and "/ok: true != false" in d


def test_http_expect_post_body_and_status_list(runner, ctx, api):
    a = api({("POST", "/v1/reconcile"): (403, {"title": "forbidden"})})
    st, d = A["http_expect"](ctx, {"url": f"{a.url}/v1/reconcile", "method": "POST", "body": {"x": 1},
                                   "status": [403, 405]})
    assert st == PASS and json.loads(a.body) == {"x": 1}


def test_http_expect_https_pins_the_ca(runner, ctx, api, tmp_path):
    import simdesk

    pki = simdesk.make_pki(tmp_path / "pki")
    a = api({"/x": {"y": 1}}, tls=pki)
    assert A["http_expect"](ctx, {"url": f"{a.url}/x", "ca": str(pki["ca"]), "json_match": {"/y": 1}})[0] == PASS
    st, d = A["http_expect"](ctx, {"url": f"{a.url}/x", "ca": str(pki["rogue_ca"]), "timeout": 0.5})
    assert st == FAIL and "CERTIFICATE_VERIFY_FAILED" in d or "certificate verify failed" in d


def test_http_expect_unreachable(runner, ctx):
    st, d = A["http_expect"](ctx, {"url": f"http://127.0.0.1:{free_port()}/", "timeout": 0.3})
    assert st == FAIL and "Connection refused" in d or "URLError" in d


# ---- cmd_json_expect ------------------------------------------------------------------------------------------------
def test_cmd_json_expect_match_retry_and_lines(runner, ctx, tmp_path):
    st, d = A["cmd_json_expect"](ctx, {"cmd": "echo '{\"valid\": true, \"errors\": []}'",
                                       "match": {"/valid": True, "/errors": {"len": 0}}})
    assert st == PASS
    cnt = tmp_path / "n"
    cmd = (f"n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}; "
           "echo \"{\\\"n\\\": $n}\"")
    st, d = A["cmd_json_expect"](ctx, {"cmd": cmd, "match": {"/n": {"gte": 3}}, "timeout": 10, "every": 0.1})
    assert st == PASS and int(cnt.read_text()) == 3
    lines = "printf '%s\\n' '{\"action\": \"apply\"}' 'not json' '{\"action\": \"reject\"}'"
    st, d = A["cmd_json_expect"](ctx, {"cmd": lines, "json_lines": True,
                                       "match": {"": {"where": {"/action": "apply"}, "count": 1}}})
    assert st == PASS


def test_cmd_json_expect_exit_code_and_no_json(runner, ctx):
    st, d = A["cmd_json_expect"](ctx, {"cmd": "echo '{}'; exit 3", "match": {}})
    assert st == FAIL and "exit 3 (want 0)" in d
    assert A["cmd_json_expect"](ctx, {"cmd": "echo '{\"a\": 1}'; exit 1", "expect_rc": "", "match": {"/a": 1}})[0] == PASS
    st, d = A["cmd_json_expect"](ctx, {"cmd": "echo hello", "match": {"/a": 1}})
    assert st == FAIL and "no JSON" in d


# ---- fe_status_expect / fe_history_* against CP-7 documents ---------------------------------------------------------------
def status_doc(state="Installed", version="1.1.0", **dep):
    return {"apiVersion": "flexengine.local/v1", "kind": "EngineStatus",
            "engine": {"version": "2.0.0a1", "spi": "1.0", "uptimeSeconds": 3.0, "mode": "running"},
            "devices": [{"deviceId": "panda", "role": "standalone-device", "onboarded": True, "manifestVersion": 4,
                         "online": True, "outboxLength": 0, "drivers": ["container"],
                         "deployments": [{"id": "pg", "driver": "container", "state": state, "version": version,
                                          **dep}]}]}


def test_status_documents_are_cp7_valid():
    from flexengine2 import contracts

    contracts.validate("cli-status", status_doc(health="healthy"))
    contracts.validate("cli-status", status_doc("Failed", "1.2.0", rolledBackTo="1.1.0", failFast=True,
                                                reason="health check failed", reasonCode="FE-REC-004"))


def test_fe_status_expect_over_the_api(runner, ctx, api, tmp_path):
    seq = [status_doc("Installing", "1.1.0"), status_doc("Installed", "1.1.0", health="healthy")]
    a = api({"/v1/status": lambda p: seq.pop(0) if len(seq) > 1 else seq[0]}, token="t0k")
    ctx.vars.update(fe_api=a.url, fe_api_token_file=tokfile(tmp_path))
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "deployment": "pg", "state": "Installed",
                                        "version": "1.1.0", "health": "healthy", "timeout": 5, "every": 0.1})
    assert st == PASS and "Installed 1.1.0" in d
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "device_fields": {"onboarded": True, "/manifestVersion": {"gte": 4}},
                                        "save": {"mv": "/manifestVersion"}})
    assert st == PASS and ctx.vars["mv"] == "4"
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "deployment": "pg", "state": "Failed", "timeout": 0.3})
    assert st == FAIL and "is Installed 1.1.0" in d
    st, d = A["fe_status_expect"](ctx, {"device": "nope", "timeout": 0.2})
    assert st == FAIL and "not in the status" in d
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "deployment": "pg", "absent": True, "timeout": 0.2})
    assert st == FAIL and "still Installed" in d
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "deployment": "xx", "absent": True})
    assert st == PASS


def test_fe_status_expect_failed_fields_and_cli(runner, ctx, tmp_path):
    doc = status_doc("Failed", "1.2.0", rolledBackTo="1.1.0", failFast=True, reasonCode="FE-REC-004",
                     reason="health check failed")
    fe = tmp_path / "fe"
    fe.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'status --json' ] && cat <<'EOF'\n{json.dumps(doc)}\nEOF\n")
    fe.chmod(0o755)
    ctx.vars.update(fe=str(fe), fe_host="local")
    st, d = A["fe_status_expect"](ctx, {"device": "panda", "deployment": "pg", "state": "Failed", "version": "1.2.0",
                                        "fields": {"rolledBackTo": "1.1.0", "failFast": True,
                                                   "reasonCode": {"in": ["FE-REC-004"]}}, "via": "cmd"})
    assert st == PASS and "rolledBackTo" in d and "FE-REC-004" in d


def history(records):
    return [{"deviceId": "panda", "delivered": True, "record": r} for r in records]


def test_fe_history_mark_and_expect(runner, ctx, api, tmp_path):
    t0 = 1_000.0
    recs = [{"dep": "pg", "seq": 1, "state": "Installed", "version": "1.0.0", "deviceTime": t0}]
    a = api({"/v1/history": lambda p: history([r for r in recs if r["deviceTime"] > float(p.split("since=")[1].split("&")[0])])},
            token="t0k")
    ctx.vars.update(fe_api=a.url, fe_api_token_file=tokfile(tmp_path))
    assert A["fe_history_mark"](ctx, {"name": "upd", "device": "panda"})[0] == PASS
    assert ctx.marks["fe:upd"] == t0
    recs += [{"dep": "pg", "seq": 2, "state": "Installing", "version": "1.1.0", "deviceTime": t0 + 1},
             {"dep": "dl", "seq": 3, "state": "Installed", "version": "1.0.0", "deviceTime": t0 + 2},
             {"dep": "pg", "seq": 4, "state": "Installed", "version": "1.1.0", "deviceTime": t0 + 4.5}]
    pat = [{"dep": "pg", "state": "Installing", "version": "1.1.0"}, {"dep": "pg", "state": "Installed", "version": "1.1.0"}]
    st, d = A["fe_history_expect"](ctx, {"since": "upd", "device": "panda", "match": pat, "max_duration": 10,
                                         "save_duration": "dur"})
    assert st == PASS and "3.50 s" in d and ctx.vars["dur"] == "3.500"
    st, d = A["fe_history_expect"](ctx, {"since": "upd", "match": pat, "max_duration": 1})
    assert st == FAIL and "> max 1" in d
    st, d = A["fe_history_expect"](ctx, {"since": "upd", "match": [{"dep": "pg", "state": "Failed"}], "timeout": 0.3})
    assert st == FAIL and "matched 0/1" in d
    assert A["fe_history_expect"](ctx, {"since": "nomark", "match": pat})[0] == FAIL


# ---- zenoh_expect -----------------------------------------------------------------------------------------------------
def test_zenoh_expect_json_and_miss(runner, ctx):
    zenoh = pytest.importorskip("zenoh")
    port = free_port()
    pub = zenoh.open(zenoh.Config.from_json5(json.dumps({"mode": "peer", "listen": {"endpoints": [f"tcp/127.0.0.1:{port}"]},
                                                          "scouting": {"multicast": {"enabled": False}}})))
    stop = threading.Event()

    def loop():
        n = 0
        while not stop.is_set():
            n += 1
            pub.put("0/gripper_station/state/T/H", json.dumps({"seq": n, "phase": "open"}))
            time.sleep(0.1)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    try:
        st, d = A["zenoh_expect"](ctx, {"connect": f"tcp/127.0.0.1:{port}", "mode": "peer",
                                        "key": "0/gripper_station/state/*/*", "match": {"/seq": {"gte": 3}}, "timeout": 15})
        assert st == PASS and "0/gripper_station/state/T/H" in d
        st, d = A["zenoh_expect"](ctx, {"connect": f"tcp/127.0.0.1:{port}", "mode": "peer", "key": "0/other/**",
                                        "timeout": 1.5})
        assert st == FAIL and "no sample" in d
    finally:
        stop.set()
        th.join(2)
        pub.close()


# ---- smp_probe (fake flexEngine-MCU of the contract suite) -------------------------------------------------------------
@pytest.fixture
def fake_mcu():
    sys.path.insert(0, str(FLEXENGINE / "tests" / "contract"))
    from spi_harness.fakes.fakesmp import FakeMcu

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(Encoding.X962,
                                                                           PublicFormat.UncompressedPoint)[1:]
    m = FakeMcu(raw, base_version="1.1.0").start()
    yield m
    m.stop()


def test_smp_probe_commands(runner, ctx, fake_mcu):
    p = {"host": "127.0.0.1", "port": fake_mcu.port, "timeout": 5}
    st, d = A["smp_probe"](ctx, {**p, "cmd": "image_list", "match": {"/images[slot=0]/version": {"regex": "^1\\.1\\.0"},
                                                                     "/images[slot=0]/confirmed": True}})
    assert st == PASS, d
    st, d = A["smp_probe"](ctx, {**p, "cmd": "flexmod_status", "match": {"/sockets": {"len": 2}}})
    assert st == PASS, d
    st, d = A["smp_probe"](ctx, {**p, "cmd": "echo", "text": "hi", "match": {"/echo": "hi"}})
    assert st == PASS, d
    st, d = A["smp_probe"](ctx, {**p, "cmd": "image_list", "match": {"/images[slot=0]/version": "9.9.9"}, "timeout": 1})
    assert st == FAIL and "answer:" in d


def test_smp_probe_unreachable(runner, ctx):
    st, d = A["smp_probe"](ctx, {"host": "127.0.0.1", "port": free_port(), "cmd": "echo", "timeout": 0.5,
                                 "connect_wait": 0.2})
    assert st == FAIL and "SMP echo" in d


# ---- PLC stand-ins (demo/edge/sim, read-only) -------------------------------------------------------------------------------
@pytest.fixture
def plc_sim():
    sys.path.insert(0, str(REPO / "demo" / "edge"))
    import sim.ads_plc as ads_plc
    import sim.modbus_plc as modbus_plc
    return ads_plc, modbus_plc


HASH = "FLEX01_" + "5a" * 32
OTHER = "FLEX01_" + "c3" * 32


def test_modbus_expect_hash_registers_and_coils(runner, ctx, plc_sim):
    port = free_port()
    plc = plc_sim[1].ModbusPlc(HASH, port=port).start()
    try:
        p = {"host": "127.0.0.1", "port": port, "unit": 1, "timeout": 3}
        assert A["modbus_expect"](ctx, {**p, "holding": [256, 16], "equals": HASH})[0] == PASS
        st, d = A["modbus_expect"](ctx, {**p, "holding": [256, 16], "equals": OTHER, "timeout": 0.5})
        assert st == FAIL and "holding [256, 16]" in d
        assert A["modbus_expect"](ctx, {**p, "coils": [0, 4], "equals": [False, False, False, False]})[0] == PASS
        plc.set_hash(OTHER)
        assert A["modbus_expect"](ctx, {**p, "holding": [256, 16], "equals": OTHER})[0] == PASS
    finally:
        plc.stop()
    st, d = A["modbus_expect"](ctx, {"host": "127.0.0.1", "port": port, "holding": [256, 16], "equals": HASH,
                                     "timeout": 0.3})
    assert st == FAIL


def test_ads_probe_hello_and_symbol(runner, ctx, plc_sim):
    from pyads import constants
    from pyads.testserver.advanced_handler import PLCVariable

    import struct

    from pyads.testserver import AdvancedHandler

    ads_plc = plc_sim[0]

    class HelloAndSymbols(ads_plc.HelloHandler):
        """The stand-in's Hello RPC plus ordinary symbols (the door variable of the CX), as the real PLC has both."""

        def handle_request(self, request):
            cmd = struct.unpack("<H", request.ams_header.command_id)[0]
            data = request.ams_header.data
            if cmd == constants.ADSCOMMAND_READWRITE and struct.unpack("<I", data[:4])[0] == constants.ADSIGRP_SYM_HNDBYNAME:
                wlen = struct.unpack("<I", data[12:16])[0]
                if data[16:16 + wlen].decode(errors="replace").strip("\x00") != f"{self.plc.instance}#Hello":
                    return AdvancedHandler.handle_request(self, request)
            return super().handle_request(request)

    class Plc(ads_plc.AdsPlc):
        def __init__(self, h):
            super().__init__(h)
            from pyads.testserver import AdsTestServer
            self.handler = HelloAndSymbols(self)
            self.server = AdsTestServer(handler=self.handler, logging=False, ip_address="127.0.0.1", port=self.port)

    try:
        plc = Plc(HASH).start()          # AMS TCP 48898 on 127.0.0.1
    except OSError as e:
        pytest.skip(f"AMS port 48898 busy ({e})")
    try:
        plc.handler.add_variable(PLCVariable("MAIN.xDoorOpen", True, ads_type=constants.ADST_BIT, symbol_type="BOOL"))
        p = {"net_id": "127.0.0.1.1.1", "ip": "127.0.0.1", "port": 851, "local_net_id": "127.0.0.1.1.9", "timeout": 5}
        st, d = A["ads_probe"](ctx, {**p, "hello_hash": HASH})
        assert st == PASS and "Hello FLEX01_5a5a" in d
        st, d = A["ads_probe"](ctx, {**p, "hello_hash": OTHER, "timeout": 0.5})
        assert st == FAIL and "!=" in d
        st, d = A["ads_probe"](ctx, {**p, "symbol": "MAIN.xDoorOpen", "equals": True})
        assert st == PASS and "MAIN.xDoorOpen = true" in d
    finally:
        plc.stop()


# ---- ssh_run / journal ---------------------------------------------------------------------------------------------------
@pytest.fixture
def fake_ssh(env_path):
    s = env_path / "fake-ssh"   # `ssh [-o opt]... host cmd`: runs cmd locally, records the host
    s.write_text("#!/bin/sh\nwhile [ $# -gt 2 ]; do case \"$1\" in -o) shift 2 ;; *) shift ;; esac; done\n"
                 f"echo \"$1\" >> {env_path / 'ssh-hosts'}\nshift\nexec sh -c \"$1\"\n")
    s.chmod(0o755)
    return s


def test_ssh_run_local_remote_save(runner, ctx, fake_ssh, env_path):
    st, d = A["ssh_run"](ctx, {"host": "local", "cmd": "echo started=2026-10-17T10:00:00Z", "match": "started=(\\S+)",
                               "save": {"started": 1}})
    assert st == PASS and ctx.vars["started"] == "2026-10-17T10:00:00Z"
    ctx.vars["ssh"] = str(fake_ssh)
    st, d = A["ssh_run"](ctx, {"host": "lab@lattepanda.lab", "cmd": "echo hi; exit 0", "match": "^hi$"})
    assert st == PASS and (env_path / "ssh-hosts").read_text().strip() == "lab@lattepanda.lab"
    st, d = A["ssh_run"](ctx, {"host": "local", "cmd": "exit 4"})
    assert st == FAIL and "exit 4 (want 0)" in d
    assert A["ssh_run"](ctx, {"host": "local", "cmd": "exit 4", "expect_rc": 4})[0] == PASS
    st, d = A["ssh_run"](ctx, {"host": "local", "cmd": "echo nope", "match": "yes"})
    assert st == FAIL and "no line matching" in d


def test_journal_file_source(runner, ctx, tmp_path):
    log = tmp_path / "engine.log"
    log.write_text(json.dumps({"event": "old", "device": "panda"}) + "\n")
    src = f"file:{log}"
    assert A["journal_mark"](ctx, {"name": "m", "source": src})[0] == PASS

    def later():
        time.sleep(0.4)
        with open(log, "a") as fh:
            fh.write(json.dumps({"ts": "2026-10-17T10:00:00.000Z", "event": "onboarded", "device": "panda"}) + "\n")
    threading.Thread(target=later, daemon=True).start()
    st, d = A["journal_expect"](ctx, {"since": "m", "fields": {"event": "onboarded", "device": "panda", "ts": {"regex": "Z$"}},
                                      "timeout": 5})
    assert st == PASS and "onboarded" in d
    st, d = A["journal_expect"](ctx, {"since": "m", "pattern": "\"event\": \"old\"", "timeout": 0.3})
    assert st == FAIL                          # before the mark
    assert A["journal_expect"](ctx, {"since": "m", "pattern": "panic", "absent": True, "timeout": 0.3})[0] == PASS
    assert A["journal_expect"](ctx, {"since": "m", "pattern": "onboarded", "absent": True, "timeout": 0.3})[0] == FAIL
    assert A["journal_expect"](ctx, {"since": "m", "pattern": "onboarded", "count": 2, "timeout": 0.3})[0] == FAIL


def test_journal_journald_source(runner, ctx, env_path):
    jc = env_path / "journalctl"
    jc.write_text("#!/bin/sh\necho \"$@\" > " + str(env_path / "jc-args") + "\n"
                  "echo '{\"event\": \"action.apply\", \"dep\": \"pg\", \"version\": \"1.1.0\"}'\n")
    jc.chmod(0o755)
    assert A["journal_mark"](ctx, {"name": "j", "unit": "flexengine", "host": "local"})[0] == PASS
    st, d = A["journal_expect"](ctx, {"since": "j", "fields": {"event": "action.apply", "version": "1.1.0"}, "timeout": 2})
    assert st == PASS
    args = (env_path / "jc-args").read_text()
    assert "-u flexengine" in args and "--since @" in args and "-o cat" in args


# ---- write_file / manual / power_cycle ----------------------------------------------------------------------------------
def test_write_file_variants(runner, ctx, tmp_path, fake_ssh):
    st, d = A["write_file"](ctx, {"path": str(tmp_path / "a" / "plain.txt"), "content": "x"})
    assert st == PASS and (tmp_path / "a" / "plain.txt").read_text() == "x"       # the runner's own write_file
    p = tmp_path / "safe.json"
    st, d = A["write_file"](ctx, {"path": str(p), "json": {"machine_parked": False}, "mode": "600"})
    assert st == PASS and json.loads(p.read_text()) == {"machine_parked": False}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    ctx.vars["ssh"] = str(fake_ssh)
    r = tmp_path / "remote" / "f.json"
    st, d = A["write_file"](ctx, {"host": "lab@lp", "path": str(r), "json": {"a": 1}})
    assert st == PASS and json.loads(r.read_text()) == {"a": 1} and "lab@lp:" in d


def test_manual_dry_run_and_desk(runner, ctx, tmp_path):
    dry = Ctx({"dry_run": "1"})
    st, d = A["manual"](dry, {"instruction": "unplug", "confirm": "unplugged?", "sim": f"touch {tmp_path / 'done'}"})
    assert st == PASS and (tmp_path / "done").exists() and "answered yes" in d
    assert A["manual"](dry, {"instruction": "x", "confirm": "y?", "sim_answer": "n"})[0] == FAIL
    st, d = A["manual"](dry, {"instruction": "x", "sim": "exit 2"})
    assert st == FAIL and "exit 2" in d
    desk = Ctx({}, unattended=True)            # the runner's own manual step: unattended = skipped
    assert A["manual"](desk, {"instruction": "look", "sim": "touch /nonexistent/x"})[0] == SKIP


class Port:
    def __init__(self):
        self.port = free_port()
        self.sock = None

    def up(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(8)
        self.sock = s

    def down(self):
        if self.sock:
            self.sock.close()
            self.sock = None


def test_power_cycle_dry_run(runner, tmp_path):
    p = Port()
    dry = Ctx({"dry_run": "1"})
    script = tmp_path / "cut.py"
    script.write_text(f"import socket,time,os\ntime.sleep(0.3)\nopen({str(tmp_path / 'cut')!r},'w').close()\n")
    p.up()
    try:
        st, d = A["power_cycle"](dry, {"instruction": "pull the plug", "simulate": f"{sys.executable} {script}",
                                       "wait_up": {"host": "127.0.0.1", "port": p.port}, "timeout": 10})
        assert st == PASS and "simulated" in d and (tmp_path / "cut").exists()
    finally:
        p.down()
    st, d = A["power_cycle"](dry, {"instruction": "x"})
    assert st == FAIL and "no `simulate`" in d


def test_power_cycle_desk_waits_down_then_up(runner, monkeypatch):
    p = Port()
    p.up()
    desk = Ctx({})

    def cut():
        time.sleep(0.5)
        p.down()
        time.sleep(1.0)
        p.up()
    monkeypatch.setattr(runner, "ask", lambda *a, **k: "")
    threading.Thread(target=cut, daemon=True).start()
    try:
        st, d = A["power_cycle"](desk, {"instruction": "pull it", "wait_up": {"host": "127.0.0.1", "port": p.port},
                                        "timeout": 20})
        assert st == PASS and "by hand" in d
    finally:
        p.down()


def test_power_cycle_desk_never_down(runner, monkeypatch):
    p = Port()
    p.up()
    monkeypatch.setattr(runner, "ask", lambda *a, **k: "")
    monkeypatch.setattr(f, "_tcp_up", lambda h, port: True)
    try:
        st, d = A["power_cycle"](Ctx({}), {"instruction": "x", "wait_up": {"host": "127.0.0.1", "port": p.port},
                                           "timeout": 1})
        assert st == FAIL and "never went down" in d
    finally:
        p.down()


# ---- variable keys + validation ------------------------------------------------------------------------------------------
def test_matcher_keys_take_variables(runner, ctx):
    ctx.vars["dep"] = "pg"
    st, d = A["cmd_json_expect"](ctx, {"cmd": "echo '{\"actual\": {\"pg\": {\"version\": \"2.0.0\"}}}'",
                                       "match": {"/actual/${dep}/version": "2.0.0"}})
    assert st == PASS


def test_validate_reports_parameters_vars_and_marks(runner):
    rb = {"id": "X", "title": "t", "vars": {"a": {"default": "1", "prompt": "a", "oops": 1}},
          "steps": [{"id": "s1", "fe_history_expect": {"since": "m", "match": [{"dep": "d", "state": "Installed"}]}},
                    {"id": "s2", "smp_probe": {"cmd": "echo"}},
                    {"id": "s3", "cmd_json_expect": {"cmd": "x", "save": {"v": "/v"}}},
                    {"id": "s4", "run": {"cmd": "echo ${v} ${a} ${b}"}},
                    {"id": "s5", "journal_expect": {"since": "j"}},
                    {"id": "s6", "manual": {"instruction": "x"}, "extra": 1}]}
    errs = f.validate(rb, runner.ACTIONS)
    text = "\n".join(errs)
    assert "var a: unknown keys ['oops']" in text
    assert "s1: history mark 'm' is not set" in text
    assert "s2: smp_probe needs tty or host + port" in text
    assert "s4: ${b} is not a variable" in text and "${v}" not in text and "${a}" not in text
    assert "s5: journal mark 'j'" in text and "s5: journal_expect needs pattern or fields" in text
    assert "s6: unknown step keys ['extra']" in text

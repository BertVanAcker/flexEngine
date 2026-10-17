"""EV-07 local API (local/api.py): handlers against a real device engine and offline from its data dir, the Unix
socket and the read-only TCP bind."""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
import threading
import time
import urllib.request

import pytest

from flexengine2 import contracts
from flexengine2.local.api import (ApiClient, EngineProvider, LocalApi, OfflineProvider, config_devices,
                                   local_settings)
from flexengine2.util import canonical_json

from _local_fixtures import BUNDLE, DEVICE, TAMPERED, FakeDriver, cloud_key, installed_engine, make_engine, \
    write_config


@pytest.fixture
def setup(tmp_path):
    cfg, path = write_config(tmp_path)
    eng = installed_engine(cfg)
    return cfg, path, eng


def online(eng, cfg=None, **kw):
    return LocalApi(EngineProvider([eng], cfg, config_file="engine.yaml"), socket_path=None, log=lambda m: None, **kw)


def offline(cfg):
    return LocalApi(OfflineProvider(cfg, "engine.yaml"), socket_path=None, log=lambda m: None)


def get(api, target):
    r = api.handle("GET", target)
    return r.status, (json.loads(r.body) if not r.streaming and r.body else r.body)


# ---- status -----------------------------------------------------------------------------------------------------
def test_status_online_validates_and_shows_installed(setup):
    cfg, _, eng = setup
    st, doc = get(online(eng, cfg), "/v1/status")
    assert st == 200
    contracts.validate("cli-status", doc)
    assert doc["engine"]["mode"] == "running" and doc["engine"]["configFile"] == "engine.yaml"
    (d,) = doc["devices"]
    assert d["deviceId"] == DEVICE and d["role"] == "standalone-device" and d["manifestVersion"] == 3
    assert d["drivers"] == ["native"] and d["outboxLength"] == len(eng.state["outbox"]) and d["online"] is False
    deps = {x["id"]: x for x in d["deployments"]}
    assert set(deps) == {"native-app", "safety-app"}
    assert all(x["state"] == "Installed" and x["health"] == "healthy" and x["version"] == "1.0.0"
               and x["previousVersion"] is None and x["since"] > 0 for x in deps.values())


def test_status_offline_equals_online_but_mode(setup):
    cfg, _, eng = setup
    on = get(online(eng, cfg), "/v1/status")[1]
    off = get(offline(cfg), "/v1/status")[1]
    contracts.validate("cli-status", off)
    assert off["engine"]["mode"] == "offline" and off["engine"]["uptimeSeconds"] == 0
    for d in (on, off):
        d.pop("engine")
    assert on == off


def test_status_offline_without_data_dir(tmp_path):
    cfg, _ = write_config(tmp_path)
    doc = get(offline(cfg), "/v1/status")[1]
    contracts.validate("cli-status", doc)
    assert doc["devices"][0]["deployments"] == [] and doc["devices"][0]["onboarded"] is False


def test_pending_failed_and_removing_states(tmp_path):
    cfg, _ = write_config(tmp_path, parked=False)
    drv = FakeDriver()
    eng = make_engine(cfg, drv)
    eng.import_bundle(str(BUNDLE))
    eng.tick()
    api = online(eng, cfg)
    deps = {x["id"]: x for x in get(api, "/v1/status")[1]["devices"][0]["deployments"]}
    assert deps["safety-app"]["state"] == "Pending" and deps["safety-app"]["reason"]
    assert deps["safety-app"]["reasonCode"].startswith("FE-")
    # unblock, install, then a v4 that removes native-app and fails safety-app (rolled back to 1.0.0)
    (tmp_path / "safe.json").write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    eng.tick()
    doc = copy.deepcopy(eng.state["desired"])
    doc["manifestVersion"] = 4
    doc["deployments"] = [d for d in doc["deployments"] if d["metadata"]["id"] == "safety-app"]
    doc["deployments"][0]["spec"]["parameters"] = {"mode": "fast"}
    body = canonical_json(doc)
    from flexengine2.identity import crypto

    assert eng.accept_manifest(body, crypto.sign(cloud_key(), body), None)
    deps = {x["id"]: x for x in get(api, "/v1/status")[1]["devices"][0]["deployments"]}
    assert deps["native-app"]["state"] == "Removing"
    drv.healthy, drv.fail_reason = False, "process exited (Restart=never)"
    eng.tick()
    st, doc = get(api, "/v1/status")
    contracts.validate("cli-status", doc)
    deps = {x["id"]: x for x in doc["devices"][0]["deployments"]}
    assert "native-app" not in deps
    f = deps["safety-app"]
    assert f["state"] == "Failed" and f["rolledBackTo"] == "1.0.0" and f["failFast"] is True
    assert f["reason"] == "health check failed" and f["reasonCode"] == "FE-REC-004" and f["previousVersion"] is None
    # delivered (outbox empty): rolledBackTo / failFast still come from the history ring buffer (EV-15 dry run)
    eng.state["outbox"] = []
    eng.save()
    f = {x["id"]: x for x in get(api, "/v1/status")[1]["devices"][0]["deployments"]}["safety-app"]
    assert f["state"] == "Failed" and f["rolledBackTo"] == "1.0.0" and f["failFast"] is True
    f = {x["id"]: x for x in get(offline(cfg), "/v1/status")[1]["devices"][0]["deployments"]}["safety-app"]
    assert f["rolledBackTo"] == "1.0.0" and f["failFast"] is True


def test_deployment_endpoint_and_404(setup):
    cfg, _, eng = setup
    api = online(eng, cfg)
    st, dep = get(api, f"/v1/devices/{DEVICE}/deployments/native-app")
    assert st == 200 and dep["id"] == "native-app"
    contracts.validate("cli-status#Deployment", dep)
    assert get(api, f"/v1/devices/{DEVICE}/deployments/nope")[0] == 404
    st, prob = get(api, "/v1/devices/other/deployments/native-app")
    assert st == 404 and prob["title"] == "unknown device"
    assert api.handle("GET", "/v2/status").status == 404
    assert api.handle("POST", "/v1/status").status == 405


# ---- history / logs ---------------------------------------------------------------------------------------------
def test_history_records_validate_and_filter(setup):
    cfg, _, eng = setup
    api = online(eng, cfg)
    st, hist = get(api, "/v1/history?since=0")
    assert st == 200 and len(hist) == len(eng.state["outbox"]) >= 4
    for e in hist:
        contracts.validate("status", e["record"])
        assert e["deviceId"] == DEVICE and e["delivered"] is False
    times = [e["record"]["deviceTime"] for e in hist]
    assert times == sorted(times)
    assert get(api, f"/v1/history?since={times[-1] + 1}")[1] == []
    assert len(get(api, f"/v1/history?device={DEVICE}")[1]) == len(hist)  # default: last hour
    assert get(api, "/v1/history?device=nope")[0] == 404
    assert get(api, "/v1/history?since=yesterday")[0] == 400
    # delivered reports leave the outbox; the history (obs/history.py) still has them
    eng.state["outbox"] = eng.state["outbox"][-1:]
    eng.save()
    hist2 = get(offline(cfg), "/v1/history?since=0")[1]
    assert [e["delivered"] for e in hist2] == [True] * (len(hist2) - 1) + [False]
    assert [e["record"]["seq"] for e in hist2] == [e["record"]["seq"] for e in hist]


def test_logs_tail_and_fields(setup):
    cfg, _, eng = setup
    api = online(eng, cfg)
    r = api.handle("GET", "/v1/logs")
    assert r.status == 200 and r.ctype == "application/x-ndjson"
    lines = [json.loads(x) for x in r.body.splitlines()]
    assert [x["action"] for x in lines] == ["apply", "apply"]
    assert all(x["device"] == DEVICE and x["source"] == "actions" and x["event"] == "action.apply" and x["ts"]
               for x in lines)
    assert len(api.handle("GET", "/v1/logs?tail=1").body.splitlines()) == 1
    assert api.handle("GET", "/v1/logs?follow=2").status == 400
    assert api.handle("GET", "/v1/logs?device=nope").status == 404


def test_logs_extra_log_file_from_provider(setup, tmp_path):
    cfg, _, eng = setup
    logf = tmp_path / "engine.jsonl"
    logf.write_text(json.dumps({"ts": "2026-10-17T10:00:00.000Z", "level": "info", "msg": "hello"}) + "\nplain text\n")
    api = LocalApi(EngineProvider([eng], cfg, log_files=lambda dev, dd: [logf]), socket_path=None, log=lambda m: None)
    lines = [json.loads(x) for x in api.handle("GET", "/v1/logs").body.splitlines()]
    msgs = [x["msg"] for x in lines]
    assert "hello" in msgs and "plain text" in msgs
    hello = next(x for x in lines if x["msg"] == "hello")
    assert hello["source"] == "log" and hello["t"] == pytest.approx(1792231200.0)


# ---- capabilities / health / metrics ----------------------------------------------------------------------------
def test_capabilities_online_and_503_offline(setup):
    cfg, _, eng = setup
    st, caps = get(online(eng, cfg), f"/v1/capabilities/{DEVICE}")
    assert st == 200
    contracts.validate("capabilities", caps)
    assert get(offline(cfg), f"/v1/capabilities/{DEVICE}")[0] == 503
    assert get(online(eng, cfg), "/v1/capabilities/nope")[0] == 404


def test_health_ok_stale_and_offline(setup):
    cfg, _, eng = setup
    now = [time.time()]
    api = online(eng, cfg, clock=lambda: now[0])
    st, h = get(api, "/v1/health")
    assert st == 200 and h["ok"] is True and h["ticks"] == 1
    contracts.validate("cli-status", get(api, "/v1/status")[1])
    now[0] += 3600
    st, h = get(api, "/v1/health")
    assert st == 503 and h["ok"] is False and h["lastTickAgeSeconds"] >= 3600
    st, h = get(offline(cfg), "/v1/health")
    assert st == 503 and h == {"ok": False, "ticks": 0}


def test_metrics(setup):
    cfg, _, eng = setup
    st, m = get(online(eng, cfg), "/v1/metrics")
    assert st == 200 and m[DEVICE]["ticks"] == 1
    assert get(offline(cfg), "/v1/metrics")[0] == 503
    api = LocalApi(EngineProvider([eng], metrics=lambda: {"x": 1}), socket_path=None)
    assert get(api, "/v1/metrics")[1] == {"x": 1}


# ---- writes -----------------------------------------------------------------------------------------------------
def test_bundle_import_offline_then_engine_installs(tmp_path):
    cfg, _ = write_config(tmp_path)
    api = offline(cfg)
    r = api.handle("POST", "/v1/bundle", BUNDLE.read_bytes())
    assert r.status == 202 and r.json() == {"deviceId": DEVICE, "manifestVersion": 3, "accepted": True, "changed": True}
    r = api.handle("POST", f"/v1/bundle?device={DEVICE}", BUNDLE.read_bytes())
    assert r.status == 202 and r.json()["changed"] is False          # idempotent
    deps = get(api, "/v1/status")[1]["devices"][0]["deployments"]
    assert {d["state"] for d in deps} == {"Pending"} and get(api, "/v1/status")[1]["devices"][0]["manifestVersion"] == 3
    eng = make_engine(cfg)
    assert eng.state["manifestVersion"] == 3
    eng.tick()
    assert sorted(eng.state["applied"]) == ["native-app", "safety-app"]


def test_bundle_refusals(setup):
    cfg, _, eng = setup
    api = online(eng, cfg)
    assert api.handle("POST", "/v1/bundle", TAMPERED.read_bytes()).status == 422
    assert api.handle("POST", "/v1/bundle", b"not a tar").status == 422
    assert api.handle("POST", "/v1/bundle", b"").status == 400
    r = api.handle("POST", "/v1/bundle?device=other", BUNDLE.read_bytes())
    assert r.status == 422 and "not 'other'" in r.json()["detail"]
    # an older manifestVersion than the accepted one is a downgrade
    from flexengine2.identity import crypto

    doc = copy.deepcopy(eng.state["desired"])
    doc["manifestVersion"] = 9
    body = canonical_json(doc)
    assert eng.accept_manifest(body, crypto.sign(cloud_key(), body), None)
    r = api.handle("POST", "/v1/bundle", BUNDLE.read_bytes())
    assert r.status == 422 and "downgrade" in r.json()["detail"]
    assert eng.state["manifestVersion"] == 9


def test_bundle_import_runs_on_the_engine_thread(setup):
    cfg, _, eng = setup
    seen = []
    orig = eng.accept_manifest

    def spy(*a, **kw):
        seen.append(threading.current_thread().name)
        return orig(*a, **kw)

    eng.accept_manifest = spy
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            eng.run_pending()
            stop.wait(0.01)

    t = threading.Thread(target=worker, name="engine-worker")
    eng.worker = t
    t.start()
    try:
        assert online(eng, cfg).handle("POST", "/v1/bundle", BUNDLE.read_bytes()).status == 202
    finally:
        stop.set()
        t.join()
    assert seen == ["engine-worker"]


def test_reconcile_nudge_and_offline(setup):
    cfg, _, eng = setup
    hold = threading.Event()
    eng.worker = threading.Thread(target=hold.wait)    # the host's worker thread, sleeping between ticks
    eng.worker.start()
    try:
        eng._wake.clear()
        r = online(eng, cfg).handle("POST", "/v1/reconcile")
        assert r.status == 202 and r.json() == {"queued": [DEVICE]}
        assert eng._wake.is_set()                       # its sleep() returns: the next tick pulls and reconciles
    finally:
        hold.set()
        eng.worker.join()
    assert offline(cfg).handle("POST", "/v1/reconcile").status == 503


def test_no_operation_changes_desired_state(setup):
    cfg, _, eng = setup
    before = copy.deepcopy(eng.state["desired"])
    api = online(eng, cfg)
    for t in ("/v1/health", "/v1/status", "/v1/history?since=0", "/v1/logs", f"/v1/capabilities/{DEVICE}",
              f"/v1/devices/{DEVICE}/deployments/native-app", "/v1/metrics"):
        assert api.handle("GET", t).status == 200, t
    api.handle("POST", "/v1/reconcile")
    api.handle("POST", "/v1/bundle", BUNDLE.read_bytes())   # same signed desired state: no change
    for m in ("PUT", "DELETE", "PATCH"):
        assert api.handle(m, "/v1/status").status == 405
    assert eng.state["desired"] == before


# ---- socket / TCP -----------------------------------------------------------------------------------------------
def test_unix_socket_server_mode_and_client(setup, sock_dir):
    cfg, _, eng = setup
    sock = sock_dir / "run" / "api.sock"
    api = LocalApi(EngineProvider([eng], cfg), socket_path=str(sock), log=lambda m: None).start()
    try:
        assert stat.S_ISSOCK(os.lstat(sock).st_mode) and stat.S_IMODE(os.lstat(sock).st_mode) == 0o660
        c = ApiClient(str(sock))
        assert c.reachable()
        r = c.request("GET", "/v1/status")
        assert r.status == 200
        contracts.validate("cli-status", r.json())
        r = c.request("POST", "/v1/bundle", BUNDLE.read_bytes(), "application/x-tar")
        assert r.status == 202 and r.json()["accepted"] is True
        with pytest.raises(RuntimeError):  # a second server on a live socket
            LocalApi(EngineProvider([eng]), socket_path=str(sock), log=lambda m: None).start()
    finally:
        api.stop()
    assert not sock.exists()


def test_stale_socket_replaced_and_regular_file_refused(setup, sock_dir):
    cfg, _, eng = setup
    sock = sock_dir / "api.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    s.close()                                   # stale socket file of a crashed engine
    api = LocalApi(EngineProvider([eng]), socket_path=str(sock), log=lambda m: None).start()
    assert ApiClient(str(sock)).reachable()
    api.stop()
    f = sock_dir / "file.sock"
    f.write_text("x")
    with pytest.raises(FileExistsError):
        LocalApi(EngineProvider([eng]), socket_path=str(f), log=lambda m: None).start()


def test_logs_follow_streams_over_socket(setup, sock_dir):
    cfg, _, eng = setup
    sock = sock_dir / "api.sock"
    api = LocalApi(EngineProvider([eng], cfg), socket_path=str(sock), log=lambda m: None).start()
    try:
        r = ApiClient(str(sock), timeout=10).request("GET", "/v1/logs?follow=1&tail=0")
        assert r.status == 200 and r.streaming
        eng.action("repair", "native-app", version="1.0.0")
        line = json.loads(next(iter(r.body)))
        assert line["action"] == "repair" and line["device"] == DEVICE
        r.body.close()
    finally:
        api.stop()


def _tcp(port, method, path, token=None, data=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=data,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_tcp_bind_read_only_with_token(setup, tmp_path):
    cfg, _, eng = setup
    tok = tmp_path / "api.token"
    tok.write_text("s3cret\n")
    os.chmod(tok, 0o600)
    api = LocalApi(EngineProvider([eng], cfg), socket_path=None, tcp_bind="127.0.0.1:0", token_file=str(tok),
                   log=lambda m: None).start()
    try:
        port = api.tcp_address[1]
        st, doc = _tcp(port, "GET", "/v1/status", "s3cret")
        assert st == 200
        contracts.validate("cli-status", doc)
        assert _tcp(port, "GET", "/v1/status")[0] == 401
        assert _tcp(port, "GET", "/v1/status", "wrong")[0] == 401
        assert _tcp(port, "POST", "/v1/reconcile", "s3cret", b"")[0] == 403
        assert _tcp(port, "POST", "/v1/bundle", "s3cret", BUNDLE.read_bytes())[0] == 403
    finally:
        api.stop()


def test_tcp_token_file_must_be_private(setup, tmp_path):
    from flexengine2.identity.secrets import SecretPermissionError

    cfg, _, eng = setup
    tok = tmp_path / "api.token"
    tok.write_text("s3cret\n")
    os.chmod(tok, 0o644)
    with pytest.raises(SecretPermissionError):
        LocalApi(EngineProvider([eng]), socket_path=None, tcp_bind="127.0.0.1:0", token_file=str(tok)).start()


def test_socket_path_too_long_is_a_registered_error_and_a_config_warning(tmp_path, sock_dir):
    """sun_path holds 108 bytes on Linux, 104 on macOS: a longer local.socket (or <dataDir>/api.sock) is refused with
    a clear, registered text (FE-LOC-001; the host logs it and runs on without the API), and `config check` warns."""
    import yaml

    from flexengine2.config.load import check
    from flexengine2.local.api import SUN_PATH_MAX

    long = sock_dir / ("d" * (SUN_PATH_MAX - len(str(sock_dir)))) / "api.sock"
    with pytest.raises(ValueError) as ei:
        LocalApi(OfflineProvider({}), socket_path=str(long), log=lambda m: None).start()
    assert contracts.reason_code(str(ei.value), "log")["code"] == "FE-LOC-001" and str(long) in str(ei.value)
    assert not long.parent.exists()                     # nothing created
    cfg, path = write_config(tmp_path)
    cfg["local"]["socket"] = str(long)
    path.write_text(yaml.safe_dump(cfg))
    r = check(path)
    assert r["valid"] and [w for w in r["warnings"] if w.startswith("/local/socket: ") and "set local.socket" in w]
    cfg["local"]["socket"] = str(sock_dir / "api.sock")
    path.write_text(yaml.safe_dump(cfg))
    assert check(path)["warnings"] == []


def test_settings_from_config(tmp_path):
    cfg, _ = write_config(tmp_path, group="flex", tcp={"bind": "127.0.0.1:8781", "tokenFile": "/t"})
    from flexengine2.config.load import validate

    assert validate(cfg) == []
    assert local_settings(cfg) == {"socket_path": str(tmp_path / "api.sock"), "group": "flex",
                                   "tcp_bind": "127.0.0.1:8781", "token_file": "/t"}
    assert local_settings({})["socket_path"] == "/run/flexengine/api.sock"
    api = LocalApi.from_config(cfg, OfflineProvider(cfg))
    assert api.tcp_bind == "127.0.0.1:8781" and api.group == "flex"


def test_config_devices_roles_and_profiles():
    cfg = {"dataDir": "/d", "devices": [
        {"deviceId": "gw", "drivers": {"container": {"runtime": "docker"}, "plc-verify": {}}},
        {"deviceId": "gw.mcu", "parent": "gw", "mcu": {}},
        {"deviceId": "u", "dataDir": "/x", "drivers": {"applab": {}, "native": True}}]}
    assert config_devices(cfg) == [
        {"deviceId": "gw", "dataDir": "/d/gw", "role": "gateway", "parent": None, "drivers": ["container", "plc-verify"]},
        {"deviceId": "gw.mcu", "dataDir": "/d/gw.mcu", "role": "child-device", "parent": "gw",
         "drivers": ["mcu-module", "mcu-firmware"]},
        {"deviceId": "u", "dataDir": "/x", "role": "standalone-device", "parent": None, "drivers": ["applab", "native"]}]


def test_serve_host_hook_with_refused_device(tmp_path, sock_dir):
    """host.run(cfg, on_ready=serve_host): running engines live, a refused device read from its data dir; the API
    stops with the host."""
    import yaml

    from flexengine2.local.api import serve_host

    cfg, path = write_config(tmp_path, sock_dir=sock_dir)
    eng = installed_engine(cfg)
    cfg["devices"].append({"deviceId": "broken", "drivers": {"native": True}})
    path.write_text(yaml.safe_dump(cfg))

    class FakeHost:
        def __init__(self):
            self.cfg, self.engines, self.started_at = cfg, [eng], time.time() - 5
            self.stop_event = threading.Event()

    host = FakeHost()
    api = serve_host(host, config_file=str(path), log=lambda m: None)
    sock = cfg["local"]["socket"]
    doc = ApiClient(sock).request("GET", "/v1/status").json()
    contracts.validate("cli-status", doc)
    assert [d["deviceId"] for d in doc["devices"]] == [DEVICE, "broken"]
    assert doc["engine"]["uptimeSeconds"] >= 5 and doc["engine"]["mode"] == "running"
    host.stop_event.set()
    for _ in range(100):
        if not os.path.exists(sock):
            break
        time.sleep(0.02)
    assert not os.path.exists(sock) and api._servers == []

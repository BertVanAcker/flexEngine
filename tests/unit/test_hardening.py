"""EV-05 hardening: typed per-phase errors, per-device worker threads, state schemaVersion / corruption, bounded
outbox, SIGTERM shutdown, sd_notify, onboarding token file, the host hook for the local API."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fe_unit_fakes import SPI_METHODS, FakeDriver, deployment, make_engine, reports, set_desired
from flexengine2 import host as hostmod
from flexengine2.core import outbox
from flexengine2.core import state as state_file
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import DriverError
from flexengine2.identity import crypto
from flexengine2.obs import logging as flog


@pytest.fixture(autouse=True)
def _log_to_buffer():
    buf = io.StringIO()
    flog.configure("json", stream=buf)
    yield buf
    flog.reset()


def records(buf) -> list[dict]:
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]


# ---- typed errors per phase -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("meth", SPI_METHODS)
def test_driver_raising_in_any_spi_method_never_escapes_tick(tmp_path, meth, _log_to_buffer):
    drv = FakeDriver()
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d1")])
    eng.tick()                                            # installs d1 (healthy)
    assert "d1" in eng.state["applied"]
    drv.raise_in[meth] = RuntimeError(f"boom in {meth}")
    if meth == "failed":
        drv.healthy = False                               # failed() is only asked while the gate sees unhealthy
    # a new manifest forces plan/apply/health/commit/... again; for remove: absence = removal
    set_desired(eng, 2, [] if meth == "remove" else [deployment("d1", "2.0.0")])
    for _ in range(3):
        delay = eng.tick()
        assert isinstance(delay, float) and delay > 0
    eng.state["capsSent"] = False                         # capabilities() is only called by the sync phase
    eng.tick()
    m = eng.metrics()
    if meth not in ("list", "capabilities"):               # list(): not used by the reconciler; caps: sync is Offline first
        assert sum(m["errors"].values()) >= 1, (meth, m)
        assert m["errorsByType"].get("RuntimeError", 0) >= 1
        err = [r for r in records(_log_to_buffer) if r["event"] == "engine.error"]
        assert err and err[-1]["errorType"] == "RuntimeError" and err[-1]["phase"] in ("sync", "reconcile", "supervise")
        assert f"boom in {meth}" in err[-1]["msg"]
    drv.raise_in.clear()
    drv.healthy = True
    for _ in range(4):
        eng.tick()
    if meth == "remove":
        assert "d1" not in eng.state["applied"] and "d1" not in drv.units
    else:
        assert eng.state["applied"]["d1"]["wl"]["version"] in ("1.0.0", "2.0.0")


def test_reconcile_error_names_phase_and_deployment(tmp_path, _log_to_buffer):
    drv = FakeDriver()
    drv.raise_in["status"] = KeyError("gone")
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d7")])
    d1 = eng.tick()
    d2 = eng.tick()
    le = eng.metrics()["lastError"]
    assert le["phase"] == "reconcile" and le["dep"] == "d7" and le["type"] == "KeyError" and le["where"]
    assert d2 >= 1.0 and d1 >= 1.0                         # error back-off (1 s .. 10 s), not the 0.05 s poll
    drv.raise_in.clear()
    eng.tick()
    assert eng.err_backoff.n == 0 and "d7" in eng.state["applied"]


def test_driver_error_on_apply_keeps_its_semantics(tmp_path):
    """A DriverError from apply is still a failed apply: fail fast, no health gate, roll back (here: remove)."""
    drv = FakeDriver()
    drv.raise_in["apply"] = DriverError("executor refused")
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d1")])
    eng.tick()
    st = [r["state"] for r in reports(eng)]
    assert st == ["Installing", "Failed"]
    failed = reports(eng)[-1]
    assert failed["reason"] == "apply failed" and failed["failFast"] is True and failed["rolledBackTo"] is None
    assert ("health", "d1") not in drv.calls and ("remove", "d1") in drv.calls
    assert eng.metrics()["errors"] == {}                   # not an engine error


# ---- worker threads -----------------------------------------------------------------------------------------------
def _host(tmp_path, engines, **kw) -> hostmod.Host:
    cfg = {"cloud": {}, "dataDir": str(tmp_path), "devices": [{"deviceId": e.device} for e in engines]}
    return hostmod.Host(cfg, engines, [], **kw)


def test_failing_device_never_stops_the_others(tmp_path):
    bad, good = FakeDriver(), FakeDriver()
    for m in SPI_METHODS:
        bad.raise_in[m] = RuntimeError(m)
    a = make_engine(tmp_path, "dev-bad", {"native": bad})
    b = make_engine(tmp_path, "dev-good", {"native": good})
    set_desired(a, 1, [deployment("x")])
    set_desired(b, 1, [deployment("y")])
    h = _host(tmp_path, [a, b])
    h.start()
    try:
        t0 = time.time()
        while time.time() - t0 < 5 and not ("y" in b.state["applied"] and a.ticks >= 2):
            time.sleep(0.02)
        assert "y" in b.state["applied"]
        assert a.ticks >= 2 and h.running("dev-bad") and h.running("dev-good")
        assert sum(a.metrics()["errors"].values()) >= 2
        assert "last error: dev-bad" in h.status_text()
    finally:
        assert h.shutdown(5) == []


def test_slow_driver_on_one_device_does_not_delay_another(tmp_path):
    slow, fast = FakeDriver(), FakeDriver()
    slow.delay["supervise"] = 2.0                      # a blocked executor call
    a = make_engine(tmp_path, "dev-slow", {"native": slow})
    b = make_engine(tmp_path, "dev-fast", {"native": fast}, pollInterval=0.1)
    b.backoff.base = b.backoff.cap = 0.1              # offline back-off == one period
    ticks_b: list[tuple[float, float]] = []            # (start, end) of every tick of the fast device
    orig = b.tick

    def tick():
        t0 = time.monotonic()
        try:
            return orig()
        finally:
            ticks_b.append((t0, time.monotonic()))

    b.tick = tick
    h = _host(tmp_path, [a, b])
    h.start()
    try:
        time.sleep(1.5)                                  # inside the slow device's 2 s blocked call
    finally:
        h.shutdown(5)
    assert a.ticks <= 1
    # Isolation: the fast device keeps its own cadence while the slow one is blocked. The pause between two of its
    # ticks is its own period plus scheduling slack, never the slow device's 2 s; how long its own tick takes depends
    # on the runner and is not counted against it. Sharing a thread with the slow device would allow one tick here.
    pauses = [nxt[0] - prev[1] for prev, nxt in zip(ticks_b, ticks_b[1:])]
    assert len(ticks_b) >= 4, ticks_b
    assert max(pauses) < 0.1 + 0.25, pauses


def test_submit_runs_on_the_worker_thread(tmp_path):
    e = make_engine(tmp_path)
    h = _host(tmp_path, [e])
    h.start()
    try:
        fut = e.submit(lambda: threading.current_thread().name)
        assert fut.result(5) == "engine-dev-a"
        bad = e.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            bad.result(5)
    finally:
        h.shutdown(5)
    assert e.submit(lambda: 7).result(0) == 7          # no worker: inline


def test_shutdown_saves_state_and_reports_stuck_workers(tmp_path):
    drv = FakeDriver()
    e = make_engine(tmp_path, drivers={"native": drv})
    h = _host(tmp_path, [e])
    h.start()
    time.sleep(0.2)
    drv.delay["supervise"] = 3.0
    time.sleep(0.3)
    assert h.shutdown(0.5) == ["dev-a"]                # still inside the 3 s call
    drv.delay.clear()
    h.threads["dev-a"].join(5)
    assert not h.threads["dev-a"].is_alive()
    assert json.loads((tmp_path / "dev-a" / "state.json").read_text())["schemaVersion"] == 1


# ---- state.json --------------------------------------------------------------------------------------------------------
V0 = {"deviceId": "dev-a", "onboarded": True, "etag": "\"e1\"", "manifestVersion": 3, "desired": None,
      "applied": {}, "failed": {}, "rejected": None, "outbox": [], "seq": 4}


def test_v0_state_is_migrated_in_memory_and_written_as_v1(tmp_path):
    p = tmp_path / "dev-a" / "state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(V0))
    e = make_engine(tmp_path)
    assert e.state["schemaVersion"] == 1 and e.state["manifestVersion"] == 3 and e.state["seq"] == 4
    assert e.state["notices"] == {} and e.state["journal"] is None and e.state["health"] == {}
    assert "schemaVersion" not in json.loads(p.read_text())          # in memory only ...
    e.save()
    assert json.loads(p.read_text())["schemaVersion"] == 1            # ... until the next save


@pytest.mark.parametrize("content", [b"{\"deviceId\": \"dev-a\", \"outb", b"[1, 2]", b"\xff\xfe", b"{}",
                                     json.dumps({**V0, "deviceId": "other"}).encode()])
def test_corrupt_state_is_kept_and_refused(tmp_path, content):
    p = tmp_path / "dev-a" / "state.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(content)
    with pytest.raises(state_file.StateCorrupt) as ei:
        make_engine(tmp_path)
    assert "flexengine doctor --repair" in str(ei.value)
    copies = list(p.parent.glob("state.json.corrupt-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == content
    assert p.read_bytes() == content                                  # left in place: every restart refuses
    with pytest.raises(state_file.StateCorrupt):
        make_engine(tmp_path)
    assert len(list(p.parent.glob("state.json.corrupt-*"))) == 1     # no pile of identical copies


def test_newer_state_schema_is_refused(tmp_path):
    p = tmp_path / "dev-a" / "state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({**V0, "schemaVersion": 99}))
    with pytest.raises(state_file.StateTooNew):
        make_engine(tmp_path)


def test_host_refuses_only_the_device_with_corrupt_state(tmp_path):
    cfg = {"cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": crypto.public_b64(crypto.generate()),
                     "releasePublicKeys": [], "pollInterval": 0.05},
           "dataDir": str(tmp_path), "devices": [{"deviceId": "ok", "onboardingToken": "t"},
                                                 {"deviceId": "broken", "onboardingToken": "t"}]}
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "state.json").write_text("{not json")
    h = hostmod.Host.from_config(cfg)
    assert [e.device for e in h.engines] == ["ok"] and "broken" in h.refused
    assert "corrupt" in h.refused["broken"]
    assert set(h.data_dirs) == {"ok", "broken"}
    devs = {d["deviceId"]: d for d in h.devices()}
    assert devs["broken"]["refused"] and devs["ok"]["metrics"]["device"] == "ok"
    with pytest.raises(state_file.StateCorrupt):
        hostmod.build(cfg)                                            # without `refused`: raises


# ---- outbox bound -------------------------------------------------------------------------------------------------------
def R(dep, state, seq):
    return {"dep": dep, "state": state, "seq": seq}


def test_outbox_compaction_policy():
    ob = [R("a", "Pending", 1), R("a", "Installing", 2), R("a", "Installed", 3), R("b", "Installing", 4),
          R("a", "Installing", 5), R("a", "Failed", 6), R("b", "Installed", 7), R("c", "Pending", 8)]
    assert outbox.compact(ob, 8) == (ob, 0)
    kept, n = outbox.compact(ob, 6)        # step 1: superseded transitional reports go
    assert [r["seq"] for r in kept] == [3, 6, 7, 8] and n == 4
    kept, n = outbox.compact(ob, 3)        # step 2: superseded terminal reports go too
    assert [r["seq"] for r in kept] == [6, 7, 8]
    kept, n = outbox.compact(ob, 1)        # never the last report of a deployment
    assert {r["dep"] for r in kept} == {"a", "b", "c"} and len(kept) == 3


def test_engine_bounds_outbox_and_warns_once_per_episode(tmp_path, _log_to_buffer):
    e = make_engine(tmp_path, outboxMax=5)
    for i in range(30):
        e.report(f"d{i % 3}", "Pending" if i % 2 else "Installing", version=str(i))
    ob = reports(e)
    assert len(ob) <= 5
    assert {r["dep"] for r in ob} == {"d0", "d1", "d2"}
    assert [r["seq"] for r in ob] == sorted(r["seq"] for r in ob)
    for d in ("d0", "d1", "d2"):           # the newest report of every deployment survives
        assert max(r["seq"] for r in ob if r["dep"] == d) == max(
            s for s in range(1, 31) if (s - 1) % 3 == int(d[1]))
    warns = [r for r in records(_log_to_buffer) if r["event"] == "outbox.backpressure"]
    assert len(warns) == 1 and warns[0]["level"] == "warning"
    m = e.metrics()
    assert m["backpressure"] and m["outboxDropped"] == 30 - len(ob) and m["outboxLength"] == len(ob)
    e.state["outbox"].clear()
    e._bound_outbox()
    assert not e.metrics()["backpressure"]


# ---- onboarding token file (EV-06 wiring) --------------------------------------------------------------------------
@pytest.fixture
def cloud(tmp_path):
    from flexengine2.testing.fakecloud import CloudServer
    from flexengine2.testing.edge_lab import admin

    key = crypto.generate()
    srv = CloudServer(tmp_path / "cloud", 0, "admin-secret", key).start()
    srv.token = lambda dev: admin(srv.port, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})[1]["token"]
    srv.pub = crypto.public_b64(key)
    yield srv
    srv.stop()


def test_onboarding_consumes_the_token_file(tmp_path, cloud):
    tok = tmp_path / "tok"
    tok.write_text(cloud.token("dev-a") + "\n")
    os.chmod(tok, 0o600)
    e = make_engine(tmp_path, apiUrl=cloud.url, cloudPublicKey=cloud.pub, onboardingTokenFile=str(tok),
                    onboardingToken=None)
    e.tick()
    assert e.state["onboarded"] and not tok.exists()
    assert json.loads((tmp_path / "dev-a" / "state.json").read_text())["onboarded"] is True


def test_onboarding_refuses_a_world_readable_token_file(tmp_path, cloud, _log_to_buffer):
    tok = tmp_path / "tok"
    tok.write_text(cloud.token("dev-a"))
    os.chmod(tok, 0o644)
    e = make_engine(tmp_path, apiUrl=cloud.url, cloudPublicKey=cloud.pub, onboardingTokenFile=str(tok),
                    onboardingToken=None)
    e.tick()
    assert not e.state["onboarded"] and tok.exists()
    assert e.metrics()["lastError"]["type"] == "SecretPermissionError"
    os.chmod(tok, 0o600)
    e.tick()
    assert e.state["onboarded"] and not tok.exists()


def test_missing_token_file_falls_back_to_the_inline_token(tmp_path, cloud):
    e = make_engine(tmp_path, apiUrl=cloud.url, cloudPublicKey=cloud.pub, onboardingTokenFile=str(tmp_path / "gone"),
                    onboardingToken=cloud.token("dev-a"))
    e.tick()
    assert e.state["onboarded"]


# ---- host.run: SIGTERM, sd_notify, on_ready ---------------------------------------------------------------------------
def _host_cfg(tmp_path, **extra) -> dict:
    return {"cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": crypto.public_b64(crypto.generate()),
                      "releasePublicKeys": [], "pollInterval": 0.1, "backoff": [0.05, 0.2], "httpTimeout": 0.5},
            "dataDir": str(tmp_path / "data"),
            "devices": [{"deviceId": "dev-a", "onboardingToken": "t"}, {"deviceId": "dev-b", "onboardingToken": "t"}],
            **extra}


def test_sigterm_shuts_down_cleanly_with_state_saved(tmp_path):
    cfgp = tmp_path / "engine.json"
    cfgp.write_text(json.dumps(_host_cfg(tmp_path, logFormat="json")))
    log = tmp_path / "engine.log"
    p = subprocess.Popen([sys.executable, "-m", "flexengine2.local.cli", "run", "--config", str(cfgp)],
                         stdout=open(log, "wb"), stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 20 and log.read_text().count('"event":"offline"') < 4:
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        assert p.wait(15) == 0
    finally:
        if p.poll() is None:
            p.kill()
    recs = [json.loads(l) for l in log.read_text().splitlines() if l.startswith("{")]
    stops = {r["device"] for r in recs if r["event"] == "engine.stop"}
    assert stops == {"dev-a", "dev-b"}
    for dev in ("dev-a", "dev-b"):
        st = json.loads((tmp_path / "data" / dev / "state.json").read_text())
        assert st["schemaVersion"] == 1 and st["deviceId"] == dev


def test_sd_notify_ready_watchdog_status_stopping(tmp_path, sock_dir, monkeypatch):
    sock_path = str(sock_dir / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)   # macOS: small default datagram buffer
    srv.settimeout(0.2)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    monkeypatch.setenv("WATCHDOG_USEC", "1000000")           # WatchdogSec = 1 s: ping every <= 0.5 s
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    from flexengine2.local import sdnotify

    pings: list[float] = []                                   # when the engine pinged (its own clock, not ours)
    real_watchdog = sdnotify.watchdog

    def watchdog(env=None):
        pings.append(time.monotonic())
        return real_watchdog(env)
    monkeypatch.setattr(sdnotify, "watchdog", watchdog)
    got: list[tuple[float, str]] = []
    seen_ready: dict = {}
    stop = threading.Event()

    def on_ready(h):
        seen_ready.update(ticks={e.device: e.ticks for e in h.engines}, n=len(h.engines),
                          dirs=h.data_dirs, stop=h.stop_event)

    cfg = _host_cfg(tmp_path)
    cfg["devices"][1]["drivers"] = {"native": False}
    th = threading.Thread(target=hostmod.run, args=(cfg,), kwargs={"stop": stop, "on_ready": on_ready, "grace": 5})
    th.start()

    def collect(until: float, first_ping: bool = False):
        while time.time() < until and not (first_ping and any(m == "WATCHDOG=1" for _, m in got)):
            try:
                got.append((time.time(), srv.recv(4096).decode()))
            except socket.timeout:
                pass

    # the window starts at the first ping: how long the host takes to start (slow fsyncs on a busy runner) is not
    # what this test measures
    collect(time.time() + 10, first_ping=True)
    collect(time.time() + 3.2)
    stop.set()
    th.join(10)
    collect(time.time() + 0.3)
    srv.close()
    msgs = [m for _, m in got]
    ready = [m for m in msgs if m.startswith("READY=1")]
    assert len(ready) == 1 and "STATUS=2/2 devices running" in ready[0]
    assert all(v >= 1 for v in seen_ready["ticks"].values()) and seen_ready["n"] == 2   # after the first round
    assert seen_ready["stop"] is stop and set(seen_ready["dirs"]) == {"dev-a", "dev-b"}
    timeline = [(round(t - got[0][0], 3), m.split("\n")[0]) for t, m in got]
    assert len(pings) >= 6 and len([m for m in msgs if m == "WATCHDOG=1"]) >= 6, (pings, timeline)  # sent, received
    # The engine pings every WatchdogSec/2 = 0.5 s; systemd kills it only after WatchdogSec = 1 s without one.
    # (A 0.2 s watchdog measured the runner's scheduler on a busy macOS runner, not the engine.)
    # Measured where the engine sends (the receive times of this thread add its own scheduling delays on a busy
    # runner): no gap may reach WatchdogSec.
    assert max(b - a for a, b in zip(pings, pings[1:])) < 1.0, (pings, timeline)
    assert msgs[-1].startswith("STOPPING=1")


def test_run_with_ticks_returns_the_host(tmp_path):
    h = hostmod.run(_host_cfg(tmp_path), ticks=2, grace=5)
    assert all(e.ticks == 2 for e in h.engines) and not h.workers_alive()
    assert all(d["metrics"]["ticks"] == 2 for d in h.devices())


def test_locked_link_pool_serialises_threads():
    LP = hostmod._locked_link_pool()
    pool = LP("127.0.0.1", 9, timeout=0.1)
    inside, overlap = [0], [False]

    class FakeLink:
        def close(self):
            pass

    pool.get = lambda: FakeLink()

    def op(_):
        inside[0] += 1
        if inside[0] > 1:
            overlap[0] = True
        time.sleep(0.01)
        inside[0] -= 1

    def session_user():
        with pool.session():
            for _ in range(5):
                pool.call(op)

    ts = [threading.Thread(target=session_user) for _ in range(3)] + \
         [threading.Thread(target=lambda: [pool.call(op) for _ in range(5)]) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not overlap[0]

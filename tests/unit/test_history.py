"""EV-09 local status history (obs/history.py) and Engine.metrics()."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from fe_unit_fakes import FakeDriver, deployment, make_engine, set_desired
from flexengine2.obs import history
from flexengine2.obs import logging as flog


@pytest.fixture(autouse=True)
def quiet():
    flog.configure(stream=io.StringIO())
    yield
    flog.reset()


def test_record_and_read_since(tmp_path):
    h = history.History(tmp_path, "panda")
    a = h.record("Installing", dep="d1", version="1.0.0")
    time.sleep(0.01)
    b = h.record("Installed", dep="d1", version="1.0.0", health="healthy", nothing=None)
    assert "nothing" not in b
    got = history.read(tmp_path)
    assert [r["state"] for r in got] == ["Installing", "Installed"] and got[0]["device"] == "panda"
    assert [r["state"] for r in history.read(tmp_path, since=a["t"])] == ["Installed"]
    assert history.read(tmp_path, limit=1) == [got[-1]] and history.read(tmp_path, limit=0) == []
    assert history.read(tmp_path / "missing") == []
    assert [r["state"] for r in h.recent(since=a["t"])] == ["Installed"]


def test_rotation_bounds_the_disk_and_keeps_the_last_n(tmp_path):
    h = history.History(tmp_path, "panda", max_entries=5)
    for i in range(23):
        h.record("Pending", dep=f"d{i}")
    cur = (tmp_path / "history.jsonl").read_text().splitlines()
    old = (tmp_path / "history.jsonl.1").read_text().splitlines()
    assert len(cur) <= 5 and len(old) == 5
    got = history.read(tmp_path)
    assert len(got) >= 5 and got[-1]["dep"] == "d22"
    assert [r["dep"] for r in got] == [f"d{i}" for i in range(23 - len(got), 23)]
    assert [r["dep"] for r in h.recent()] == [f"d{i}" for i in range(18, 23)]   # in memory: exactly N


def test_restart_reloads_the_ring_and_continues_rotation(tmp_path):
    h = history.History(tmp_path, "panda", max_entries=4)
    for i in range(6):
        h.record("Pending", dep=f"d{i}")
    h2 = history.History(tmp_path, "panda", max_entries=4)
    assert [r["dep"] for r in h2.recent()] == ["d2", "d3", "d4", "d5"]
    for i in range(6, 12):
        h2.record("Pending", dep=f"d{i}")
    assert len((tmp_path / "history.jsonl").read_text().splitlines()) <= 4
    assert history.read(tmp_path)[-1]["dep"] == "d11"


def test_torn_last_line_is_skipped(tmp_path):
    h = history.History(tmp_path, "panda")
    h.record("Installed", dep="d1")
    with open(tmp_path / "history.jsonl", "a") as f:
        f.write('{"t": 1, "dep": "d2", "sta')
    assert [r["dep"] for r in history.read(tmp_path)] == ["d1"]


def test_concurrent_reader_sees_whole_records(tmp_path):
    h = history.History(tmp_path, "panda", max_entries=50)
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            for r in history.read(tmp_path):
                if set(r) < {"t", "state", "dep"}:
                    bad.append(r)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(500):
        h.record("Installing", dep=f"d{i}")
    stop.set()
    t.join()
    assert not bad


def test_engine_records_reported_and_internal_transitions(tmp_path):
    drv = FakeDriver()
    e = make_engine(tmp_path, drivers={"fake": drv}, healthEveryTicks=100)
    set_desired(e, 1, [deployment("d1")])
    e.tick()
    e.tick()                                      # nothing new: internal states are not repeated
    drv.healthy = False
    set_desired(e, 2, [deployment("d1", "2.0.0")])
    e.tick()
    got = history.read(e.dir)
    seq = [(r["state"], r["kind"]) for r in got]
    assert seq == [("PREPARING", "internal"), ("Installing", "reported"), ("TESTING", "internal"),
                   ("COMMITTING", "internal"), ("Installed", "reported"),
                   ("PREPARING", "internal"), ("Installing", "reported"), ("TESTING", "internal"),
                   ("ROLLING_BACK", "internal"), ("TESTING", "internal"), ("Failed", "reported")]
    inst = got[4]
    assert inst["version"] == "1.0.0" and inst["seq"] == 2 and inst["manifestVersion"] == 1


def test_gate_and_pending_are_in_the_history(tmp_path):
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"machine_parked": False}))
    e = make_engine(tmp_path, safeStateFile=str(safe))
    d = deployment("d1")
    d["x-flex-activation"] = {"requires": ["machine_parked"]}
    set_desired(e, 1, [d])
    e.tick()
    e.tick()
    states = [r["state"] for r in history.read(e.dir)]
    assert states == ["PREPARING", "GATE", "Pending"]


def test_engine_metrics(tmp_path):
    e = make_engine(tmp_path)
    set_desired(e, 1, [deployment("d1")])
    for _ in range(3):
        e.tick()
    m = e.metrics()
    assert m["ticks"] == 3 and m["device"] == "dev-a" and m["applied"] == ["d1"]
    assert m["reconcileSeconds"]["count"] == 3 and m["reconcileSeconds"]["max"] >= m["reconcileSeconds"]["avg"] > 0
    assert m["gateSeconds"]["count"] == 1 and m["gateSeconds"]["last"] is not None
    assert m["outboxLength"] == len(e.state["outbox"]) == 2 and m["outboxMax"] == 1000
    assert m["errors"] == {} and m["lastError"] is None and m["lastTickAt"] > m["startedAt"]
    json.dumps(m)   # JSON-serialisable for the local API

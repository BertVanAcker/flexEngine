"""EV-05 structured logging (obs/logging.py): JSON lines by default, the P2 text format on request, one line per
record, Engine.log / Engine.action through it, stdlib logging bridged."""

from __future__ import annotations

import io
import json
import logging
import re
import threading

import pytest

from fe_unit_fakes import _digest, deployment, make_engine, set_desired
from flexengine2.obs import logging as flog


@pytest.fixture
def buf():
    b = io.StringIO()
    flog.reset()
    flog.configure(stream=b)
    yield b
    flog.reset()


def lines(b) -> list[str]:
    return b.getvalue().splitlines()


def test_json_is_the_default_and_has_the_fields(buf, monkeypatch):
    monkeypatch.delenv("FLEX_LOG_FORMAT", raising=False)
    assert flog.get_format() == "json"
    flog.emit("info", "apply d1 1.0.0", device="panda", event="action.apply", dep="d1", version="1.0.0", digest="sha256:x",
              nothing=None)
    rec = json.loads(lines(buf)[0])
    assert list(rec)[:7] == ["ts", "level", "device", "event", "dep", "version", "msg"]
    assert rec["level"] == "info" and rec["device"] == "panda" and rec["dep"] == "d1" and rec["digest"] == "sha256:x"
    assert "nothing" not in rec
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", rec["ts"])


def test_text_format_is_the_p2_line(buf):
    flog.configure("text")
    flog.emit("info", "accepted desired state v3 (1 deployments)", device="panda")
    flog.emit("warning", "outbox full", device="panda")
    a, b = lines(buf)
    assert re.fullmatch(r"\S+Z \[panda\] accepted desired state v3 \(1 deployments\)", a)
    assert b.endswith("[panda] warning: outbox full")


def test_env_selects_the_format_when_unset(buf, monkeypatch):
    monkeypatch.setenv("FLEX_LOG_FORMAT", "text")
    assert flog.get_format() == "text"
    flog.configure("json")
    assert flog.get_format() == "json"
    with pytest.raises(ValueError):
        flog.configure("color")


def test_one_line_per_record_no_colours(buf):
    for fmt in ("json", "text"):
        flog.configure(fmt)
        flog.emit("error", "first\nsecond\r\nthird \x1b", device="d", event="x")
    out = lines(buf)
    assert len(out) == 2
    assert "first second third" in out[1]
    assert json.loads(out[0])["msg"].startswith("first second third")


def test_non_ascii_and_odd_values_survive(buf):
    flog.emit("info", "UC3 Greenhouse 🌱", device="unoq", event="e", obj=object(), lst=(1, 2), d={"k": {1}})
    rec = json.loads(lines(buf)[0])
    assert rec["msg"] == "UC3 Greenhouse 🌱" and rec["lst"] == [1, 2] and isinstance(rec["obj"], str)


def test_threads_never_interleave_lines(buf):
    def spam(i):
        for j in range(200):
            flog.emit("info", f"t{i}-{j} " + "x" * 200, device=f"d{i}")

    ts = [threading.Thread(target=spam, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    out = lines(buf)
    assert len(out) == 1600 and all(json.loads(l)["msg"].endswith("x" * 200) for l in out)


def test_engine_log_and_action_are_structured(tmp_path, buf):
    e = make_engine(tmp_path, device="panda")
    set_desired(e, 1, [deployment("d1")])
    e.tick()
    recs = [json.loads(l) for l in lines(buf)]
    recs = [r for r in recs if not r["event"].startswith("flexengine2.")]   # bridged stdlib records (other tests)
    assert all(r["device"] == "panda" for r in recs)
    apply = next(r for r in recs if r["event"] == "action.apply")
    assert apply["dep"] == "d1" and apply["version"] == "1.0.0" and apply["msg"] == "apply d1 1.0.0"
    assert apply["digest"] == _digest("d1-1.0.0")
    off = next(r for r in recs if r["event"] == "offline")
    assert off["level"] == "warning" and off["msg"].startswith("offline (")
    # the action log keeps its own JSONL format
    act = [json.loads(l) for l in (tmp_path / "panda" / "actions.jsonl").read_text().splitlines()]
    assert [a["action"] for a in act] == ["apply", "commit"]


def test_text_mode_keeps_the_substrings_the_integration_suites_grep(tmp_path, buf):
    flog.configure("text")
    e = make_engine(tmp_path, device="panda")
    e.log("one app per board: foreign app running: UC3 Greenhouse")
    assert "[panda] one app per board: foreign app running: UC3 Greenhouse" in buf.getvalue()


def test_stdlib_logging_is_bridged(buf):
    flog.bridge_stdlib()
    flog.bridge_stdlib()  # idempotent
    logging.getLogger("flexengine2.cloud.client").warning("plain HTTP: lab/test only (%s)", "http://x")
    out = [json.loads(l) for l in lines(buf)]
    assert len(out) == 1 and out[0]["level"] == "warning" and out[0]["event"] == "flexengine2.cloud.client"
    assert out[0]["msg"] == "plain HTTP: lab/test only (http://x)"

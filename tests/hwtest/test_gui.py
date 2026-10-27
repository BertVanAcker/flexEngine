"""The pinned runbook GUI with the flex step types (the `run_tests.sh gui` equivalent of flexNode-arduino): the EV-HW
runbooks are discovered with their sidecar metadata and board images, and a run through the HTTP API executes flex
steps, turns a power cycle into a browser prompt, and writes the runner's result.json (with the pin)."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

import gui as flexgui
import run

RB = """
id: GT-11
title: flex steps in the GUI
vars:
  greeting: {default: hello}
  api_token: {default: s3cret}
steps:
  - id: write
    write_file: {path: "out/g.json", json: {g: "${greeting}"}}
  - id: read
    cmd_json_expect: {cmd: "cat out/g.json", match: {/g: "${greeting}"}, save: {seen: /g}}
  - id: ssh
    ssh_run: {host: local, cmd: "echo seen=${seen}", match: "seen=hello"}
  - id: cut
    power_cycle: {instruction: "pull the plug", simulate: "true"}
  - id: look
    manual: {instruction: "look", confirm: "ok?"}
"""


@pytest.fixture
def app(tmp_path, runner):
    g = flexgui.load_gui()
    root = tmp_path / "repo"
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks" / "GT-11_flex.yaml").write_text(RB)
    from catalog import Catalog, Secrets
    from history import History
    from server import App, serve

    secrets = Secrets(str(tmp_path / ".hwtest" / "secrets.yaml"))
    results = tmp_path / "results"
    cat = Catalog([str(run.upstream.ensure()), str(run.HERE), str(root)], secrets)
    a = App(cat, secrets, History([str(results)]), str(results))
    httpd = serve(a, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, results, g
    httpd.shutdown()


def call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw


def test_catalog_lists_the_ev_hw_runbooks_with_boards(app):
    base, _, g = app
    rbs = {r["id"]: r for r in call(base, "/api/runbooks")}
    for i in range(1, 13):
        e = rbs[f"EV-HW-{i:02d}"]
        assert e["error"] is None, e["error"]
        assert e["goal"] and e["duration"]
    assert rbs["EV-HW-02"]["target"]["name"] == "LattePanda 3 Delta"
    assert rbs["EV-HW-12"]["target"]["board"] == "lattepanda_3_delta"
    assert rbs["EV-HW-06"]["target"]["board"] == "uno_q"
    assert rbs["EV-HW-07"]["target"]["name"].startswith("Beckhoff CX")
    assert rbs["EV-HW-04"]["target"]["board"] == "portenta_h7"           # the GUI's own board image
    svg = call(base, "/static/boards/lattepanda_3_delta_top.svg")
    assert b"<svg" in svg and b"LattePanda 3 Delta" in svg
    assert b"<svg" in call(base, "/static/boards/portenta_h7_top.svg")   # upstream images still served
    detail = call(base, "/api/runbooks/EV-HW-09")
    kinds = {s["kind"] for s in detail["steps"]}
    assert {"fe_status_expect", "power_cycle", "fe_history_expect"} <= kinds


def test_a_gui_run_with_flex_steps(app):
    base, results, _ = app
    run_name = call(base, "/api/run", {"key": "GT-11"})["run"]
    answered, end = [], time.time() + 60
    while time.time() < end:
        s = call(base, "/api/run?since=1e9&serial_since=1e9")
        if s["state"] in ("finished", "aborted"):
            break
        p = s["prompt"]
        if p:
            value = {"input": "", "manual": "y", "setup": "go"}.get(p["kind"], "c")
            answered.append((p["kind"], s["steps"][s["current"]]["id"] if s.get("current") is not None else None))
            call(base, "/api/run/answer", {"id": p["id"], "value": value})
        time.sleep(0.05)
    assert s["state"] == "finished", s
    assert [st["status"] for st in s["steps"]] == ["PASS"] * 5, s["steps"]
    assert ("input", "cut") in answered and ("manual", "look") in answered   # the power cut asked the operator
    res = json.loads(next(results.glob(f"{run_name}/result.json")).read_text())
    assert res["summary"]["PASS"] == 5 and res["flexhwtest"]["upstream"]["gui"]["commit"].startswith("0bfb8c7")
    assert res["vars"]["api_token"] == "(hidden)"

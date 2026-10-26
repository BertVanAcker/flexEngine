"""The pinned flexNode-arduino runner and GUI: fetched to the pin, checked byte for byte, used unmodified, with the
flex step types registered through the runner's ACTIONS table (plan 8.2 option (c))."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

import flexsteps
import run
import upstream


def test_lock_pins_commits_and_files():
    lock = upstream.lock()
    names = {s["name"]: s for s in lock["sources"]}
    assert names["runner"]["commit"].startswith("684bc02") and names["runner"]["ref"] == "porting-strategy"
    assert names["gui"]["commit"].startswith("0bfb8c7")
    assert "tools/hwtest/runbook.py" in names["runner"]["files"] and "LICENSE.txt" in names["runner"]["files"]
    assert "tools/hwtest/gui.py" in names["gui"]["files"]
    assert any(p.startswith("tools/hwtest/hwgui/") for p in names["gui"]["files"])
    for s in lock["sources"]:
        assert all(len(h) == 64 for h in s["files"].values())
    assert "LGPL" in lock["license"]


def test_cache_matches_the_pin(runner):
    root = upstream.ensure()
    assert upstream.check(root) == []
    assert (root / "tools" / "hwtest" / "runbook.py").read_bytes()  # unmodified file of the pinned commit


def test_tampered_copy_is_refused(tmp_path, monkeypatch, runner):
    copy = tmp_path / "up"
    shutil.copytree(upstream.ensure(), copy)
    p = copy / "tools" / "hwtest" / "runbook.py"
    p.write_bytes(p.read_bytes() + b"\n# changed\n")
    monkeypatch.setenv("FLEX_HWTEST_UPSTREAM", str(copy))
    with pytest.raises(upstream.UpstreamError, match="sha256 differs"):
        upstream.ensure()
    assert any("runbook.py" in x for x in upstream.check(copy))


def test_runner_is_the_pinned_module_with_flex_steps(runner):
    assert runner.__file__.startswith(str(upstream.CACHE)) or "FLEX_HWTEST_UPSTREAM" in __import__("os").environ
    for kind in flexsteps.NEW:
        assert runner.ACTIONS[kind] is flexsteps.ACTIONS[kind]
    for kind in ("check_tcp", "run", "upload", "serial_expect", "redis_expect", "mqtt_expect", "wait", "serve"):
        assert runner.ACTIONS[kind].__module__ == "runbook"          # the runner's own steps are untouched
    # extended (not replaced): the runner's manual/write_file stay reachable for desk runs
    assert flexsteps._ORIG["manual"].__module__ == "runbook" and flexsteps._ORIG["write_file"].__module__ == "runbook"


def test_report_carries_the_pin_and_hides_secrets(tmp_path, runner):
    rb = tmp_path / "T-01.yaml"
    rb.write_text("id: T-01\ntitle: t\nvars:\n  admin_token: {default: s3cret}\n  fe_api_token_file: {default: /x}\n"
                  "steps:\n  - id: a\n    cmd_json_expect: {cmd: \"echo '{\\\"ok\\\": true}'\", match: {/ok: true}}\n")
    r = subprocess.run([sys.executable, str(run.HERE / "run.py"), str(rb), "--yes", "--unattended", "--results",
                        str(tmp_path / "res")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads(next((tmp_path / "res").glob("*_T-01/result.json")).read_text())
    assert res["summary"] == {"PASS": 1, "FAIL": 0, "SKIPPED": 0}
    assert res["vars"]["admin_token"] == "(hidden)" and res["vars"]["fe_api_token_file"] == "/x"
    assert res["flexhwtest"]["upstream"]["runner"]["commit"].startswith("684bc02")
    assert "s3cret" not in (tmp_path / "res").glob("*_T-01").__next__().joinpath("result.md").read_text()


def test_list_fails_on_a_bad_runbook(tmp_path, runner):
    rb = tmp_path / "T-02.yaml"
    rb.write_text("id: T-02\ntitle: t\nsteps:\n  - id: a\n    http_expect: {urll: x}\n"
                  "  - id: b\n    run: {cmd: \"echo ${nope}\"}\n")
    r = subprocess.run([sys.executable, str(run.HERE / "run.py"), str(rb), "--list"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 1
    assert "http_expect misses ['url']" in r.stdout and "unknown parameters ['urll']" in r.stdout
    assert "${nope} is not a variable" in r.stdout

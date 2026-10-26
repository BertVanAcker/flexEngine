"""App packaging, fake arduino-app-cli, and the flexEngineV2 `applab` driver driven from the reference cloud fake
(port of probes/p4-applab-target/tests/test_deploy.py, same assertions; the engine is `flexengine run`)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .applablab import PROBE, WORK, item, metric, requires_board_sim

from flexp4 import ardrelease, pipeline

VALIDATE = PROBE / ".ref" / "bin" / "applab-validate"


# ---- packaging -----------------------------------------------------------------------------------

def test_release_archive_layout_matches_arduino_app_cli(built, tmp_path):
    info = ardrelease.read_ard(built["1.0.0"]["ard"])
    assert info["releaseName"] == "uc3-greenhouse-1-0-0-unoq"
    assert info["manifest"]["schema"] == 1 and info["manifest"]["target"] == "unoq"
    names = info["files"]
    assert names[:0] == [] and "release.yaml" in names and "src/app.yaml" in names
    assert {"src/python/main.py", "src/sketch/sketch.ino", "src/sketch/sketch.yaml", "prebuild/flex-host-mcu",
            "prebuild/bricks-list.yaml", "prebuild/models-list.yaml"} <= set(names)
    import tarfile
    with tarfile.open(built["1.0.0"]["ard"]) as tf:
        order = [m.name for m in tf.getmembers() if m.isfile()]
    assert order[:3] == [f"{info['releaseName']}/release.yaml", f"{info['releaseName']}/prebuild/bricks-list.yaml",
                         f"{info['releaseName']}/prebuild/models-list.yaml"]
    # reproducible: same inputs, same bytes
    again = ardrelease.build_ard(built["_apps"]["v1"]["app"], tmp_path, board="unoq", label="1.0.0",
                                 prebuild={"flex-host-mcu": built["_apps"]["v1"]["hostMcu"]},
                                 created_at=pipeline.CREATED)
    if not (built.get("_fw") or {}).get("v1"):
        assert again.read_bytes() == built["1.0.0"]["ard"].read_bytes()
    bad = tmp_path / "x.tar.gz"
    shutil.copy(built["1.0.0"]["ard"], bad)
    with pytest.raises(ardrelease.ReleaseError, match="expected '.ard'"):
        ardrelease.read_ard(bad)


@pytest.mark.skipif(not VALIDATE.exists(), reason="applab-validate (arduino-app-cli's own loaders) not built")
def test_arduino_app_cli_loaders_accept_generated_app_and_archives(built):
    r = subprocess.run([str(VALIDATE), "app", str(built["_apps"]["v1"]["app"])], capture_output=True, text=True)
    assert json.loads(r.stdout) == {"ok": True, "name": "UC3 Greenhouse", "sketch": True, "bricks": [], "icon": "🌱"}
    for label in ("1.0.0", "1.1.0", "1.0.0-ventunoq"):
        r = subprocess.run([str(VALIDATE), "ard", str(built[label]["ard"])], capture_output=True, text=True)
        out = json.loads(r.stdout)
        assert out["ok"] and out["schema"] == 1 and out["name"] == "UC3 Greenhouse", out


def test_p2_release_wraps_the_archive(registry, built):
    info = registry["info"]["1.0.0"]
    layout = registry["layout"]
    man = layout.get_manifest(info["digest"])
    desc = json.loads(layout.get_blob(man["config"]["digest"]))
    assert desc["module"] == "uc3_greenhouse" and desc["profiles"] == ["applab"]
    assert desc["interfaceHash"] == desc["bridgeContractHash"] == built["1.0.0"]["contract"]
    assert desc["safetyClass"] == "none" and desc["applab"]["board"] == "unoq"
    assert set(desc["composite"]["mcu"]) == {"io"} and set(desc["composite"]["mpu"]) == {"ctl"}
    layer = next(l for l in man["layers"] if l["annotations"].get("dev.flex.profile") == "applab")
    assert layer["annotations"]["dev.flex.contractHash"] == built["1.0.0"]["contract"]
    assert layout.get_blob(layer["digest"]) == built["1.0.0"]["ard"].read_bytes()
    assert layout.referrers(info["digest"])["manifests"], "signature referrer missing"


# ---- fake arduino-app-cli --------------------------------------------------------------------------

@requires_board_sim
def test_fake_cli_lifecycle_and_one_app_rule(board, built, tmp_path):
    r = board.cli("app", "install", str(built["1.0.0"]["ard"]), "--yes")
    assert r["release"] == "uc3-greenhouse-1-0-0-unoq" and r["target"] == "unoq"
    assert board.cli("app", "install", str(built["1.0.0"]["ard"]), "--yes", check=False)["rc"] == 1
    bad = board.cli("app", "install", str(built["1.0.0-ventunoq"]["ard"]), "--yes", check=False)
    assert bad["rc"] == 7 and "built for ventunoq, this board is a unoq" in bad["error"]
    board.cli("app", "install", str(built["1.1.0"]["ard"]), "--yes")
    lst = board.cli("app", "list", "--releases")["apps"]
    assert {a["release_id"] for a in lst} == {"uc3-greenhouse-1-0-0-unoq", "uc3-greenhouse-1-1-0-unoq"}
    assert board.cli("app", "start", "release:uc3-greenhouse-1-0-0-unoq")["status"] == "started"
    _wait(lambda: "FLEX_CONTRACT_OK" in str(board.cli("app", "logs", "release:uc3-greenhouse-1-0-0-unoq")), 30)
    assert board.ps()["uc3-greenhouse-1-0-0-unoq"] == "running"
    busy = board.cli("app", "start", "release:uc3-greenhouse-1-1-0-unoq", check=False)
    assert busy["rc"] == 1 and 'app "UC3 Greenhouse" is running' in busy["error"]
    board.cli("app", "stop", "release:uc3-greenhouse-1-0-0-unoq")
    assert board.ps()["uc3-greenhouse-1-0-0-unoq"] == "stopped"
    board.cli("app", "destroy", "release:uc3-greenhouse-1-1-0-unoq")
    assert len(board.cli("app", "list", "--releases")["apps"]) == 1


# ---- flexEngine applab driver, end to end -----------------------------------------------------------

def _wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    raise AssertionError("timeout")


def _installed(v):
    return lambda r: r["state"] == "Installed" and r.get("version") == v and r.get("health") == "healthy"


@requires_board_sim
def test_deploy_update_and_rollback_from_flexcloud(env, built):
    """The P4 success criterion in software: deploy, update, automatic rollback on a release whose firmware
    does not match its Python side (only the runtime FLEX01 handshake can see it), pre-install refusal of a
    skewed release, removal by absence."""
    eng = env.engine().start()
    c, dev, board = env.cloud, eng.dev, env.board
    t = {}

    # (a) fresh deploy 1.0.0
    pub = c.deploy(dev, [item("1.0.0", parameters={"ctl.open_above_c": 25.0})])
    r = c.wait_state(dev, "greenhouse", _installed("1.0.0"), 90, "1.0.0 installed")
    t["deploy_1.0.0"] = round(r["receivedAt"] - pub["publishedAt"], 2)
    assert r["interfaceHash"] == built["1.0.0"]["contract"]
    assert board.ps()["uc3-greenhouse-1-0-0-unoq"] == "running"
    # deployment parameter reached the app (data/flex_params.json): 26 degC > 25 opens the vent
    board.sim.set_analog("A0", 260)
    _wait(lambda: "IO D3=255" in board.io(), 30)

    # (b) update to 1.1.0 (new contract on both sides)
    pub = c.deploy(dev, [item("1.1.0", parameters={"ctl.open_above_c": 25.0})])
    r = c.wait_state(dev, "greenhouse", _installed("1.1.0"), 90, "1.1.0 installed")
    t["update_1.1.0"] = round(r["receivedAt"] - pub["publishedAt"], 2)
    ps = board.ps()
    assert ps["uc3-greenhouse-1-1-0-unoq"] == "running" and ps["uc3-greenhouse-1-0-0-unoq"] == "stopped"

    # (c) 1.2.0: v2 sources but v1 firmware -> handshake mismatch at runtime -> automatic rollback to 1.1.0
    pub = c.deploy(dev, [item("1.2.0", parameters={"ctl.open_above_c": 25.0})])
    r = c.wait_state(dev, "greenhouse", lambda r: r["state"] == "Failed" and r.get("version") == "1.2.0", 120,
                     "1.2.0 failed")
    t["rollback_1.2.0_total"] = round(r["receivedAt"] - pub["publishedAt"], 2)
    t["rollback_1.2.0_detect"] = r.get("detectSeconds")
    assert r["reason"] == "health check failed" and r["rolledBackTo"] == "1.1.0" and r["rollbackHealthy"] is True
    # fail fast (P4 amendment, consolidation): the app is `failed` (exit 3) -> no wait for the 15 s health timeout
    assert r["failFast"] is True and "failed" in r["failure"] and r["detectSeconds"] < 6, r
    ps = board.ps()
    assert ps["uc3-greenhouse-1-1-0-unoq"] == "running" and ps["uc3-greenhouse-1-2-0-unoq"] in ("failed", "stopped")
    logs = board.cli("app", "logs", "release:uc3-greenhouse-1-2-0-unoq")
    assert "FLEX_CONTRACT_MISMATCH" in str(logs)
    hs = _hs(env, eng)
    assert hs["mcu"][0] == hs["mpu"][0] == built["1.1.0"]["contract"]

    # (d) 1.3.0: skewed sources (wrapped with the CI gate off) -> refused before install, nothing stopped
    n_before = len([x for x in eng.cli_calls() if x["args"][:2] == ["app", "install"]])
    pub = c.deploy(dev, [item("1.3.0", parameters={"ctl.open_above_c": 25.0})])
    r = c.wait_state(dev, "greenhouse", lambda r: r["state"] == "Failed" and r.get("version") == "1.3.0", 90,
                     "1.3.0 refused")
    t["refuse_1.3.0_total"] = round(r["receivedAt"] - pub["publishedAt"], 2)
    assert r["rolledBackTo"] == "1.1.0" and r["rollbackHealthy"] is True
    # fail fast: a refused apply() never enters the health gate (was = the 15 s timeout)
    assert r["reason"] == "apply failed" and r["detectSeconds"] == 0.0 and t["refuse_1.3.0_total"] < 10, (r, t)
    assert "contract hash mismatch in the archive sources" in eng.log()
    assert len([x for x in eng.cli_calls() if x["args"][:2] == ["app", "install"]]) == n_before
    assert board.ps()["uc3-greenhouse-1-1-0-unoq"] == "running"

    # (e) removal by absence
    c.deploy(dev, [])
    c.wait_state(dev, "greenhouse", lambda r: r["state"] == "Removed", 60, "removed")
    assert board.cli("app", "list", "--releases")["apps"] == []
    assert not any(s == "running" for s in board.ps().values())

    acts = [a["action"] for a in eng.actions()]
    assert acts.count("rollback") == 2
    metric("engine_timings_seconds", t)
    metric("engine_actions", acts)
    metric("cli_calls_total", len(eng.cli_calls()))


@requires_board_sim
def test_plan_time_placement_one_app_per_board(env, built):
    """Exclusive executor (P4 amendment, consolidation): the applab driver reports `exclusive, maxDeployments 1`
    in the device capabilities, and the flexCloud stub refuses a plan with two App deployments for the board."""
    eng = env.engine().start()
    c = env.cloud
    c.wait(lambda: (c.st.s["devices"].get(eng.dev) or {}).get("capabilities"), 30, what="capabilities")
    frag = c.st.s["devices"][eng.dev]["capabilities"]["x-flex"]["drivers"]["applab"]
    assert frag["exclusive"] is True and frag["maxDeployments"] == 1
    s, b = c.admin("POST", "/api/v1/admin/plans", {"deviceId": eng.dev, "deployments": [
        item("1.0.0", dep_id="greenhouse"), item("1.1.0", dep_id="greenhouse-2")]})
    assert s == 422 and b["title"] == "Placement refused: exclusive executor", b
    s, b = c.admin("POST", "/api/v1/admin/plans", {"deviceId": eng.dev, "deployments": [item("1.0.0")]})
    assert s == 201, b


@requires_board_sim
def test_one_app_per_board_foreign_app(env, built):
    """A user app started from App Lab is running: the deployment is refused (no takeover) or the app is
    stopped first (takeover)."""
    board = env.board
    board.cli("app", "install", str(built["1.0.0"]["ard"]), "--yes")  # stands in for an App Lab user app
    board.cli("app", "start", "release:uc3-greenhouse-1-0-0-unoq")
    _wait(lambda: board.ps().get("uc3-greenhouse-1-0-0-unoq") == "running", 20)
    eng = env.engine().start()
    env.cloud.deploy(eng.dev, [item("1.1.0")])
    r = env.cloud.wait_state(eng.dev, "greenhouse", lambda r: r["state"] == "Failed", 90, "refused")
    assert "one app per board: foreign app running: UC3 Greenhouse" in eng.log()
    assert board.ps()["uc3-greenhouse-1-0-0-unoq"] == "running"
    assert r["rolledBackTo"] is None and r["reason"] == "apply failed"
    eng.kill()
    eng2 = env.engine(applab={"takeoverForeignApps": True}).start()
    env.cloud.deploy(eng2.dev, [item("1.1.0")])
    env.cloud.wait_state(eng2.dev, "greenhouse", _installed("1.1.0"), 90, "takeover install")
    ps = board.ps()
    assert ps["uc3-greenhouse-1-0-0-unoq"] == "stopped" and ps["uc3-greenhouse-1-1-0-unoq"] == "running"


@requires_board_sim
def test_engine_crash_resume_adopts_running_app(env, built):
    eng = env.engine().start()
    env.cloud.deploy(eng.dev, [item("1.0.0")])
    env.cloud.wait_state(eng.dev, "greenhouse", _installed("1.0.0"), 90)
    starts = len([x for x in eng.cli_calls() if x["args"][:2] == ["app", "start"]])
    eng.kill()
    eng.start()
    time.sleep(6)
    assert len([x for x in eng.cli_calls() if x["args"][:2] == ["app", "start"]]) == starts
    assert env.board.ps()["uc3-greenhouse-1-0-0-unoq"] == "running"


def _hs(env, eng):
    from flexp4.rpc import RawClient  # test oracle (stand-in client), not the driver's copy
    with RawClient(f"unix://{env.board.sim.sock}") as c:
        return {"mcu": c.call("flex.status.mcu"), "mpu": c.call("flex.status.mpu")}


@requires_board_sim
def test_supervision_restarts_a_crashed_app(env, built):
    """The Python part dies after the commit: the driver sees `failed` in `app ps` and restarts it
    (Restart=on-failure); the handshake runs again and the deployment reports healthy."""
    import os
    import signal
    from flexengine2.util import read_json
    eng = env.engine().start()
    env.cloud.deploy(eng.dev, [item("1.0.0")])
    env.cloud.wait_state(eng.dev, "greenhouse", _installed("1.0.0"), 90)
    st = read_json(env.board.state_path, {})
    rel = next(k for k in st["apps"] if k.endswith("uc3-greenhouse-1-0-0-unoq"))
    pid = st["apps"][rel]["pid"]
    # kill only the Python interpreter (the sh wrapper records its exit code, as compose records a container exit)
    py = [int(x) for x in subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()]
    os.kill(py[0], signal.SIGKILL)
    _wait(lambda: env.board.ps().get("uc3-greenhouse-1-0-0-unoq") == "failed", 20)
    r = env.cloud.wait(lambda: next((x for x in env.cloud.history(eng.dev)
                                     if "supervisor" in str(x.get("reason", ""))), None), 60,
                       what="supervisor restart reported")
    assert "restarted" in r["reason"]
    _wait(lambda: env.board.ps().get("uc3-greenhouse-1-0-0-unoq") == "running", 30)
    env.cloud.wait_state(eng.dev, "greenhouse", lambda r: r.get("health") == "healthy", 60, "healthy again")


def test_publish_cli_uploads_to_the_flexcloud_stub(built, keys, tmp_path, capsys):
    from flexengine2.testing.fakecloud import CloudServer
    from flexp4 import publish
    srv = CloudServer(tmp_path / "cloud", 0, "admin-secret", keys["cloud"], model_dirs=[pipeline.UC3_MODEL]).start()
    try:
        tok = tmp_path / "tok"
        tok.write_text("admin-secret")
        rc = publish.main(["--cloud", srv.url, "--admin-token-file", str(tok), "--ard", str(built["1.0.0"]["ard"]),
                           "--model", str(pipeline.UC3_MODEL), "--version", "1.0.0",
                           "--release-key", str(WORK / "keys" / "release.key")])
        assert rc == 0
        assert srv.state.layout.resolve("flexnodes/uc3_greenhouse:1.0.0") is not None
        rc = publish.main(["--ard", str(built["1.3.0"]["ard"]), "--model", str(pipeline.UC3_MODEL),
                           "--version", "1.3.0", "--release-key", str(WORK / "keys" / "release.key"), "--dry-run"])
        assert rc == 1 and "contract gate" in capsys.readouterr().err
    finally:
        srv.stop()


@requires_board_sim
def test_remote_mode_over_ssh_with_log_based_health(env, built, tmp_path):
    """The engine runs off-board (LattePanda) and drives the board's arduino-app-cli over SSH; the archive is
    copied with scp; health comes from `app logs` (FLEX_CONTRACT_OK <hash>). ssh/scp are local stand-ins."""
    fb = PROBE / "tools" / "fakebin"
    eng = env.engine(applab={"ssh": "arduino@unoq-sim", "sshCmd": [str(fb / "fake-ssh")],
                             "scpCmd": [str(fb / "fake-scp"), "-q"], "remoteStaging": str(tmp_path / "staging"),
                             "cli": "arduino-app-cli", "healthVia": "logs"}).start()
    env.cloud.deploy(eng.dev, [item("1.1.0", parameters={"ctl.open_above_c": 25.0})])
    env.cloud.wait_state(eng.dev, "greenhouse", _installed("1.1.0"), 90, "remote install")
    assert (tmp_path / "staging" / "uc3-greenhouse-1-1-0-unoq.ard").exists()
    env.cloud.deploy(eng.dev, [item("1.2.0", parameters={"ctl.open_above_c": 25.0})])
    r = env.cloud.wait_state(eng.dev, "greenhouse", lambda r: r["state"] == "Failed", 120, "remote rollback")
    assert r["rolledBackTo"] == "1.1.0" and r["rollbackHealthy"] is True

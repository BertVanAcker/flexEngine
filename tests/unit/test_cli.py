"""EV-07 CLI (local/cli.py, local/doctor.py): golden human output, --json against cli-status.v1, socket and
offline modes, bundle import, enroll, doctor, and the commands that existed before (run, version, config check)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from flexengine2 import __version__, contracts
from flexengine2.local import cli, doctor
from flexengine2.local.api import EngineProvider, LocalApi

from _local_fixtures import BUNDLE, DEVICE, TAMPERED, installed_engine, make_engine, write_config

GOLDEN = Path(__file__).resolve().parent / "golden" / "cli"
UPDATE = os.environ.get("FLEX_UPDATE_GOLDEN") == "1"


def golden(name: str, text: str):
    p = GOLDEN / name
    if UPDATE:
        p.write_text(text + "\n")
    assert text + "\n" == p.read_text(), f"golden {name} differs (FLEX_UPDATE_GOLDEN=1 regenerates)"


def run(capsys, *argv) -> tuple[int, str, str]:
    capsys.readouterr()                      # drop what the fixtures' engines logged
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def json_out(out: str):
    return json.loads(out)


# ---- golden human format (pure formatters) -----------------------------------------------------------------------
def test_golden_status():
    doc = json.loads((GOLDEN / "status.json").read_text())
    contracts.validate("cli-status", doc)
    golden("status.txt", cli.format_status(doc))
    doc["engine"]["mode"] = "offline"
    doc["engine"].pop("configFile")
    golden("status-offline.txt", cli.format_status({**doc, "devices": []}))


def test_golden_deployments():
    doc = json.loads((GOLDEN / "status.json").read_text())
    golden("deployments.txt", cli.format_deployments(doc))
    golden("deployments-device.txt", cli.format_deployments(doc, "panda.portenta"))
    assert cli.format_deployments(doc, "nobody") == "no deployments"


def test_golden_history_and_logs():
    hist = json.loads((GOLDEN / "history.json").read_text())
    golden("history.txt", cli.format_history(hist))
    assert cli.format_history([]) == "no status reports in range"
    logs = [json.loads(x) for x in (GOLDEN / "logs.jsonl").read_text().splitlines()]
    golden("logs.txt", "\n".join(cli.format_log(r) for r in logs))


def test_golden_doctor():
    checks = [{"name": "config", "status": "ok", "detail": "/etc/flexengine/engine.yaml: valid"},
              {"name": "panda: state.json", "status": "repaired", "detail": "moved aside"},
              {"name": "local API", "status": "warn", "detail": "unix:/run/flexengine/api.sock: no socket"},
              {"name": "cloud", "status": "fail", "detail": "https://x: refused"},
              {"name": "panda: plc sim", "status": "skip", "detail": "shm"}]
    golden("doctor.txt", doctor.format_checks(checks))


def test_parse_since():
    assert cli.parse_since("90s", now=1000) == 910
    assert cli.parse_since("2h", now=10000) == 2800
    assert cli.parse_since("1d", now=100000) == 100000 - 86400
    assert cli.parse_since("1792195000.5") == 1792195000.5
    assert cli.parse_since(None) is None
    with pytest.raises(cli.CliError):
        cli.parse_since("yesterday")


# ---- pre-EV-07 commands unchanged --------------------------------------------------------------------------------
def test_version_and_config_check(capsys, tmp_path):
    assert run(capsys, "version")[:2] == (0, __version__ + "\n")
    _, path = write_config(tmp_path)
    rc, out, _ = run(capsys, "config", "check", str(path), "--json")
    assert rc == 0 and json.loads(out) == {"valid": True, "errors": []}
    bad = tmp_path / "bad.yaml"
    bad.write_text("dataDir: d\n")
    rc, out, _ = run(capsys, "config", "check", str(bad))
    assert rc == 1 and "missing required key" in out


def test_run_ticks(capsys, tmp_path):
    _, path = write_config(tmp_path)
    assert cli.main(["run", "--config", str(path), "--ticks", "1"]) == 0
    assert (tmp_path / "data" / DEVICE / "state.json").exists()


# ---- status / deployments / history / logs -----------------------------------------------------------------------
@pytest.fixture
def offline_host(tmp_path, monkeypatch):
    cfg, path = write_config(tmp_path)
    installed_engine(cfg)
    monkeypatch.setenv("FLEXENGINE_CONFIG", str(path))
    return cfg, path


@pytest.fixture
def online_host(tmp_path, sock_dir, monkeypatch):
    cfg, path = write_config(tmp_path, sock_dir=sock_dir)
    eng = installed_engine(cfg)
    api = LocalApi.from_config(cfg, EngineProvider([eng], cfg, config_file=str(path)), log=lambda m: None).start()
    monkeypatch.setenv("FLEXENGINE_CONFIG", str(path))
    yield cfg, path, eng
    api.stop()


def test_status_json_offline_validates(capsys, offline_host):
    rc, out, _ = run(capsys, "status", "--json")
    assert rc == 0
    doc = json_out(out)
    contracts.validate("cli-status", doc)
    assert doc["engine"]["mode"] == "offline" and doc["devices"][0]["manifestVersion"] == 3


def test_status_json_via_socket_validates(capsys, online_host):
    cfg, path, _ = online_host
    rc, out, _ = run(capsys, "status", "--json")
    doc = json_out(out)
    contracts.validate("cli-status", doc)
    assert doc["engine"]["mode"] == "running" and doc["engine"]["configFile"] == str(path)
    # --socket without any config works too; --offline ignores the socket
    rc, out, _ = run(capsys, "status", "--json", "--config", str(path.parent / "missing.yaml"),
                     "--socket", cfg["local"]["socket"])
    assert rc == 0 and json_out(out)["engine"]["mode"] == "running"
    rc, out, _ = run(capsys, "status", "--json", "--offline")
    assert json_out(out)["engine"]["mode"] == "offline"


def test_status_human(capsys, offline_host):
    rc, out, _ = run(capsys, "status")
    assert rc == 0 and out.startswith(f"flexengine {__version__} (spi ") and "vec-dev" in out and "2 installed" in out


def test_status_without_engine_or_config(capsys, tmp_path, monkeypatch):
    monkeypatch.delenv("FLEXENGINE_CONFIG", raising=False)
    rc, _, err = run(capsys, "status", "--socket", str(tmp_path / "none.sock"), "--config", str(tmp_path / "x.yaml"))
    assert rc == 2 and "engine not reachable" in err


def test_deployments(capsys, online_host):
    rc, out, _ = run(capsys, "deployments")
    assert rc == 0 and out.splitlines()[0].split()[:4] == ["DEVICE", "DEPLOYMENT", "DRIVER", "STATE"]
    rc, out, _ = run(capsys, "deployments", "--json")
    rows = json_out(out)
    assert {r["id"] for r in rows} == {"native-app", "safety-app"} and all(r["deviceId"] == DEVICE for r in rows)
    rc, out, _ = run(capsys, "deployments", "--device", DEVICE, "native-app", "--json")
    dep = json_out(out)
    contracts.validate("cli-status#Deployment", dep)
    rc, _, err = run(capsys, "deployments", "--device", DEVICE, "nope")
    assert rc == 1 and "404" in err
    assert run(capsys, "deployments", "native-app")[0] == 2
    assert run(capsys, "deployments", "--device", "nobody")[0] == 1


def test_history(capsys, offline_host):
    rc, out, _ = run(capsys, "history", "--since", "1h", "--json")
    hist = json_out(out)
    assert rc == 0 and len(hist) == 4
    for e in hist:
        contracts.validate("status", e["record"])
    rc, out, _ = run(capsys, "history", "--since", str(hist[-1]["record"]["deviceTime"] + 1))
    assert out.strip() == "no status reports in range"
    assert run(capsys, "history", "--since", "soon")[0] == 2


def test_logs(capsys, online_host):
    rc, out, _ = run(capsys, "logs", "-n", "1", "--json")
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 1 and json.loads(lines[0])["action"] == "apply"
    rc, out, _ = run(capsys, "logs", "--device", DEVICE)
    assert "[vec-dev] actions apply native-app" in out
    assert run(capsys, "logs", "--device", "nope")[0] == 1


# ---- bundle import -----------------------------------------------------------------------------------------------
def test_bundle_import_offline_and_online(capsys, tmp_path, sock_dir, monkeypatch):
    cfg, path = write_config(tmp_path, sock_dir=sock_dir)
    monkeypatch.setenv("FLEXENGINE_CONFIG", str(path))
    rc, out, _ = run(capsys, "bundle", "import", str(BUNDLE))
    assert rc == 0 and out.strip() == (f"{DEVICE}: manifestVersion 3 imported into state.json (installed at the next "
                                       "engine start)")
    eng = make_engine(cfg)
    assert eng.state["manifestVersion"] == 3
    api = LocalApi.from_config(cfg, EngineProvider([eng], cfg), log=lambda m: None).start()
    try:
        rc, out, _ = run(capsys, "bundle", "import", str(BUNDLE), "--device", DEVICE, "--json")
        assert rc == 0 and json_out(out)["changed"] is False
        rc, _, err = run(capsys, "bundle", "import", str(TAMPERED))
        assert rc == 1 and "422" in err and "FE-BUN-001" in err
        assert run(capsys, "bundle", "import", str(BUNDLE), "--device", "other")[0] == 1
        assert run(capsys, "bundle", "import", str(tmp_path / "missing.tar"))[0] == 2
    finally:
        api.stop()


# ---- enroll ------------------------------------------------------------------------------------------------------
def test_enroll_writes_token_file_0600(capsys, tmp_path):
    cfg, path = write_config(tmp_path)
    target = tmp_path / "tokens" / DEVICE
    cfg["devices"][0].pop("onboardingToken")
    cfg["devices"][0]["onboardingTokenFile"] = str(target)
    import yaml

    path.write_text(yaml.safe_dump(cfg))
    src = tmp_path / "from-cloud.txt"
    src.write_text("tok-123\n")
    rc, out, _ = run(capsys, "enroll", "--config", str(path), "--token-file", str(src))
    assert rc == 0 and "tok-123" not in out and str(target) in out
    assert target.read_text() == "tok-123\n" and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    from flexengine2.identity.secrets import read_secret_file

    assert read_secret_file(target) == "tok-123"
    rc, out, err = run(capsys, "config", "check", str(path), "--resolve")
    assert rc == 0


def test_enroll_errors(capsys, tmp_path):
    _, path = write_config(tmp_path)
    src = tmp_path / "t"
    src.write_text("tok\n")
    rc, _, err = run(capsys, "enroll", "--config", str(path), "--token-file", str(src))
    assert rc == 2 and "onboardingTokenFile" in err
    rc, _, err = run(capsys, "enroll", "--config", str(path), "--token-file", str(src), "--device", "x")
    assert rc == 2 and "unknown device" in err
    src.write_text("")
    cfg = path.read_text().replace("onboardingToken: t", f"onboardingTokenFile: {tmp_path / 'tok'}")
    path.write_text(cfg)
    rc, _, err = run(capsys, "enroll", "--config", str(path), "--token-file", str(src))
    assert rc == 2 and "expected one line" in err


# ---- doctor ------------------------------------------------------------------------------------------------------
def _doctor(path, **kw):
    kw.setdefault("tcp", lambda h, p, t: None)
    kw.setdefault("cmd", lambda argv, t: (True, " ".join(argv)))
    kw.setdefault("cloud", lambda c: ("ok", f"{c['apiUrl']}: HTTP 404"))
    return doctor.Doctor(str(path), **kw).run()


def by_name(checks):
    return {c["name"]: c for c in checks}


def test_doctor_healthy_host(tmp_path):
    cfg, path = write_config(tmp_path)
    installed_engine(cfg)
    checks = by_name(_doctor(path))
    assert doctor.ok(list(checks.values()))
    assert checks["config"]["status"] == "ok" and checks[f"{DEVICE}: device key"]["status"] == "ok"
    assert checks[f"{DEVICE}: state.json"]["status"] == "ok" and checks["cloud"]["status"] == "ok"
    assert checks["local API"]["status"] == "warn"            # engine not running


def test_doctor_with_running_engine_and_drivers(tmp_path, sock_dir):
    import yaml

    cfg, path = write_config(tmp_path, sock_dir=sock_dir)
    eng = installed_engine(cfg)
    cfg["devices"][0]["drivers"].update({"container": {"runtime": "podman"}, "applab": {"ssh": "arduino@unoq"},
                                         "plc-verify": {"plcs": {"cx": {"via": "ads", "amsNetId": "1.2.3.4.1.1",
                                                                        "ip": "10.0.0.9"},
                                                                 "opta": {"via": "modbus", "host": "10.0.0.8"}}}})
    path.write_text(yaml.safe_dump(cfg))
    api = LocalApi.from_config(cfg, EngineProvider([eng], cfg), log=lambda m: None).start()
    calls = []
    try:
        checks = by_name(_doctor(path, tcp=lambda h, p, t: calls.append((h, p)) or ("refused" if h == "10.0.0.8" else None),
                                 cmd=lambda argv, t: (argv[0] != "ssh", " ".join(argv))))
    finally:
        api.stop()
    assert checks["local API"]["status"] == "ok" and "ok=True" in checks["local API"]["detail"]
    assert checks[f"{DEVICE}: container"]["status"] == "ok" and "podman version" in checks[f"{DEVICE}: container"]["detail"]
    assert checks[f"{DEVICE}: applab"]["status"] == "fail" and "arduino@unoq arduino-app-cli version" in \
        checks[f"{DEVICE}: applab"]["detail"]
    assert checks[f"{DEVICE}: plc cx"]["status"] == "ok" and checks[f"{DEVICE}: plc opta"]["status"] == "fail"
    assert ("10.0.0.9", 48898) in calls and ("10.0.0.8", 502) in calls


def test_doctor_bad_perms(tmp_path):
    cfg, path = write_config(tmp_path)
    installed_engine(cfg)
    key = tmp_path / "data" / DEVICE / "device.key"
    os.chmod(key, 0o644)
    os.chmod(tmp_path / "data", 0o777)
    try:
        checks = by_name(_doctor(path))
    finally:
        os.chmod(tmp_path / "data", 0o755)
    assert checks[f"{DEVICE}: device key"]["status"] == "fail" and "0644" in checks[f"{DEVICE}: device key"]["detail"]
    assert checks["dataDir"]["status"] == "fail" and "world-writable" in checks["dataDir"]["detail"]


def test_doctor_invalid_config(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dataDir: d\n")
    checks = _doctor(bad)
    assert checks[0]["name"] == "config" and checks[0]["status"] == "fail"
    rc, out, _ = run(capsys, "doctor", "--config", str(bad), "--json", "--socket", str(tmp_path / "s"))
    assert rc == 1 and json_out(out)["ok"] is False


@pytest.mark.parametrize("content", ["{not json", "[]", '{"deviceId": "vec-dev"}'])
def test_doctor_repair_corrupt_state(capsys, tmp_path, content):
    cfg, path = write_config(tmp_path)
    st = tmp_path / "data" / DEVICE / "state.json"
    st.parent.mkdir(parents=True)
    st.write_text(content)
    name = f"{DEVICE}: state.json"
    c = by_name(_doctor(path))[name]
    assert c["status"] == "fail" and "--repair --yes" in c["detail"] and st.exists()
    c = by_name(_doctor(path, repair=True))[name]
    assert c["status"] == "fail" and "--yes" in c["detail"] and st.read_text() == content
    c = by_name(_doctor(path, repair=True, yes=True))[name]
    assert c["status"] == "repaired" and not st.exists()
    moved = list(st.parent.glob("state.json.corrupt-*"))
    assert len(moved) == 1 and moved[0].read_text() == content
    make_engine(cfg)                                          # the engine starts fresh again


def test_doctor_state_too_new_is_not_repaired(tmp_path):
    cfg, path = write_config(tmp_path)
    eng = installed_engine(cfg)
    st = json.loads(eng.state_path.read_text())
    st["schemaVersion"] = 99
    eng.state_path.write_text(json.dumps(st))
    c = by_name(_doctor(path, repair=True, yes=True))[f"{DEVICE}: state.json"]
    assert c["status"] == "fail" and "newer" in c["detail"] and eng.state_path.exists()


def test_doctor_tcp_token_check(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("x\n")
    os.chmod(tok, 0o644)
    _, path = write_config(tmp_path, tcp={"bind": "127.0.0.1:8781", "tokenFile": str(tok)})
    c = by_name(_doctor(path))["local API tcp"]
    assert c["status"] == "fail" and "0644" in c["detail"]
    os.chmod(tok, 0o600)
    assert by_name(_doctor(path))["local API tcp"]["status"] == "ok"


# ---- relay test tools (ported from the flexedge CLI) -------------------------------------------------------------
def test_relay_commands_parse():
    a = cli.parser().parse_args(["relay-inject", "--smp", "127.0.0.1:5555", "--model", "m", "--work", "w",
                                 "--instance", "grip", "--port", "cmd", "--json", "{}"])
    assert (a.cmd, a.deployment, a.device, a.instance) == ("relay-inject", "d_pick_lab", "portenta", "grip")
    a = cli.parser().parse_args(["relay-status", "--smp", "h:1", "--model", "m", "--work", "w"])
    assert cli.COMMANDS[a.cmd] is cli.cmd_relay

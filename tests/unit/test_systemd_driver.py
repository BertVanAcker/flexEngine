"""EV-19: the native driver on systemd (drivers/native/systemd.py) -- unit rendering (golden unit files), the
environment it hands a workload, name sanitising, restart-policy mapping, preflight, capabilities, and adopt after an
engine restart against the push-tier fake systemctl (tests/contract/spi_harness/fakes/fakesystemctl.py). The same
driver on real systemd: tests/contract (nightly, -m docker) and tests/integration/systemd."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import pytest

from flexengine2 import contracts
from flexengine2.drivers.base import Workload, wait_until
from flexengine2.drivers.native import systemd as sd
from flexengine2.drivers.native.systemd import SystemdDriver, sanitize, unit_name

FLEXENGINE = Path(__file__).resolve().parents[2]
CONTRACT = FLEXENGINE / "tests" / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

from spi_harness.base import FAKES, Release, digest_of, failing_shim, shim, targz  # noqa: E402
from spi_harness.process import APP  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "systemd"
UPDATE = os.environ.get("FLEX_UPDATE_GOLDEN") == "1"


def golden(name: str, text: str):
    p = GOLDEN / name
    if UPDATE:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    assert text == p.read_text(), f"golden {name} differs (FLEX_UPDATE_GOLDEN=1 regenerates)"


def wl(**kw) -> Workload:
    d = dict(dep_id="d-pick-cell", module="cell_controller", version="1.2.0", digest="sha256:" + "ab" * 32,
             payload_path="/nonexistent.tar.gz", payload_annotations={"dev.flex.profile": "native"},
             parameters={"rate": 10, "mode": "fast"}, health={"type": "http", "path": "/healthz"},
             restart={"policy": "on-failure", "maxRetries": 2, "restartSec": 1.5})
    d.update(kw)
    return Workload(**d)


def drv(tmp: Path | str = "/var/lib/flexengine", **kw) -> SystemdDriver:
    kw.setdefault("python", "/usr/bin/python3")
    return SystemdDriver("panda", Path(tmp), **kw)


WORK = drv().dep_dir("d-pick-cell") / ("ab" * 8)


# ---- rendering (golden) --------------------------------------------------------------------------------------------
def test_golden_system_unit():
    d = drv(user="flex")
    golden("system.service", d.render(wl(), WORK, 41234, ["/usr/bin/python3", "app.py"]))


def test_golden_user_unit():
    d = drv(scope="user", unit_dir="/home/flex/.config/systemd/user")
    golden("user.service", d.render(wl(restart={"policy": "never"}), WORK, 41234, ["/usr/bin/python3", "app.py"]))


def test_golden_unit_json_exec_start_quoting(tmp_path):
    """unit.json ExecStart: {python} = the engine's interpreter, a relative program resolved in the payload tree,
    $ and % never expanded by systemd, quotes and backslashes escaped."""
    work = tmp_path / "payload"
    work.mkdir()
    (work / "unit.json").write_text(json.dumps({"ExecStart": ["bin/run", "--name=a b", "50%", "$HOME", 'q"\\']}))
    d = drv()
    argv = d._exec_start(work)
    assert argv == [str(work / "bin/run"), "--name=a b", "50%", "$HOME", 'q"\\']
    text = d.render(wl(), WORK, 41234, ["/opt/x/bin/run", "--name=a b", "50%", "$HOME", 'q"\\'])
    assert 'ExecStart="/opt/x/bin/run" "--name=a b" "50%%" "$$HOME" "q\\"\\\\"' in text
    (work / "unit.json").write_text(json.dumps({"ExecStart": ["{python}", "-m", "app"]}))
    assert d._exec_start(work) == ["/usr/bin/python3", "-m", "app"]


def test_app_py_default_exec_start(tmp_path):
    (tmp_path / "app.py").write_text("")
    assert drv()._exec_start(tmp_path) == ["/usr/bin/python3", "app.py"]


def test_default_user_is_the_engine_user():
    text = drv().render(wl(), WORK, 1, ["/bin/true"])
    if os.geteuid() == 0:
        assert "\nUser=" not in text           # root engine: the unit runs as root (no line)
    else:
        import pwd
        assert f"\nUser={pwd.getpwuid(os.geteuid()).pw_name}\n" in text
    assert "\nUser=" not in drv(scope="user", unit_dir="/x").render(wl(), WORK, 1, ["/bin/true"])


# ---- environment: only explicit variables -------------------------------------------------------------------------
def test_environment_is_explicit_and_never_the_engines(monkeypatch):
    monkeypatch.setenv("FLEX_CLOUD_TOKEN", "s3cret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cret")
    monkeypatch.setenv("HOME", "/root")
    d = drv()
    env = d.environment(wl(parameters={"rate": 10, "line-speed": "2 m/s", "x": "a\nb"}), 41234)
    assert set(env) == {"FLEX_UNIT", "FLEX_HEALTH_PORT", "FLEX_DATA_DIR", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONUNBUFFERED", "FLEX_PARAM_RATE", "FLEX_PARAM_LINE_SPEED", "FLEX_PARAM_X"}
    assert env["FLEX_UNIT"] == "panda/d-pick-cell" and env["FLEX_HEALTH_PORT"] == "41234"
    text = d.render(wl(parameters={"x": "a\nb"}), WORK, 41234, ["/bin/true"])
    assert "s3cret" not in text and "HOME" not in text and "PATH" not in text
    assert 'Environment="FLEX_PARAM_X=a\\nb"' in text   # one line, C escape


# ---- names ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dev,dep,want", [
    ("panda", "dpick", "flex-panda-dpick.service"),
    ("panda.portenta", "cell_1", "flex-panda.portenta-cell_1.service"),
])
def test_unit_names(dev, dep, want):
    assert unit_name(dev, dep) == want


def test_sanitising_is_unambiguous_and_bounded():
    names = {unit_name(a, b) for a, b in [("a-b", "c"), ("a", "b-c"), ("a_b", "c"), ("a", "b_c"), ("a/b", "c"),
                                          ("a", "b/c")]}
    assert len(names) == 6
    for n in names:
        assert all(ch.isalnum() or ch in "_.-" for ch in n), n
    long = unit_name("d" * 300, "x" * 300)
    assert len(long) < 200
    assert sanitize("ok_id.1") == "ok_id.1" and sanitize("") != ""


# ---- restart policy ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("pol,restart,burst,sec", [
    ({}, "on-failure", 6, "500ms"),
    ({"policy": "on-failure", "maxRetries": 2, "restartSec": 0.2}, "on-failure", 3, "200ms"),
    ({"policy": "always", "maxRetries": 0}, "always", 1, "500ms"),
    ({"policy": "never"}, "no", 6, "500ms"),
    ({"policy": "on-failure", "restartSec": 3}, "on-failure", 6, "3000ms"),
])
def test_restart_policy_mapping(pol, restart, burst, sec):
    text = drv().render(wl(restart=pol), WORK, 1, ["/bin/true"])
    assert f"\nRestart={restart}\n" in text
    assert f"\nStartLimitBurst={burst}\n" in text and "\nStartLimitIntervalSec=60\n" in text
    assert f"\nRestartSec={sec}\n" in text


# ---- preflight --------------------------------------------------------------------------------------------------
def _rel(tmp: Path, files: dict | None, descriptor: dict | None = None, raw: bytes | None = None) -> Release:
    p = tmp / f"p-{len(list(tmp.iterdir()))}.tar.gz"
    p.write_bytes(raw if raw is not None else targz(files))
    return Release(str(p), {}, descriptor or {})


def test_preflight(tmp_path):
    d = drv(tmp_path)
    assert d.preflight(_rel(tmp_path, {"app.py": b""}), {}) is None
    assert d.preflight(_rel(tmp_path, {"unit.json": b'{"ExecStart": ["{python}", "x.py"]}'}), {}) is None
    cases = [
        (_rel(tmp_path, {"README": b""}), "native payload has neither app.py nor unit.json"),
        (_rel(tmp_path, {"unit.json": b'{"ExecStart": []}'}), "unit.json: ExecStart must be a non-empty list"),
        (_rel(tmp_path, {"unit.json": b"{"}), "unit.json is not valid JSON"),
        (_rel(tmp_path, {"../evil.py": b"", "app.py": b""}), "escapes the payload directory"),
        (_rel(tmp_path, None, raw=b"not a tarball"), "native payload is not a tar.gz archive"),
        (_rel(tmp_path, {"app.py": b""}, {"restartPolicy": {"policy": "sometimes"}}), "is not supported"),
    ]
    for rel, want in cases:
        r = d.preflight(rel, {})
        assert r and want in r, (want, r)
        e = contracts.reason_code(f"artefact rejected: {r}", "reason")
        assert e and e["code"] == "FE-REC-002"
        inner = contracts.reason_code(r, "detail")
        assert inner and inner["code"].startswith("FE-SYD-"), (r, inner)


# ---- capabilities: never raise ------------------------------------------------------------------------------------
def _native_schema():
    s = json.loads((FLEXENGINE / "src/flexengine2/api/capabilities.v1alpha1.schema.json").read_text())
    return {**s["$defs"]["Native"], "$defs": s["$defs"]}


def test_capabilities_missing_unreachable_and_reachable(tmp_path, monkeypatch):
    schema = _native_schema()
    d = SystemdDriver("panda", tmp_path, systemctl=str(tmp_path / "no-such-systemctl"))
    c = d.capabilities()
    assert c["available"] is False and c["profile"] == "native" and c["driver"] == "systemd"
    jsonschema.validate(c, schema)
    bindir = tmp_path / "down"
    failing_shim(bindir, "systemctl", "Failed to connect to bus: No such file or directory")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    c = SystemdDriver("panda", tmp_path).capabilities()
    assert c["reachable"] is False and "Failed to connect to bus" in c["detail"] and "systemd" not in c
    jsonschema.validate(c, schema)
    up = tmp_path / "up"
    shim(up, "systemctl", FAKES / "fakesystemctl.py", {"FAKE_SYSTEMD_STATE": tmp_path / "st",
                                                         "FAKE_SYSTEMD_UNITDIR": tmp_path / "u"})
    monkeypatch.setenv("PATH", f"{up}{os.pathsep}{os.environ['PATH']}")
    c = SystemdDriver("panda", tmp_path, unit_dir=tmp_path / "u").capabilities()
    assert c["systemd"] == "255-fake" and "reachable" not in c and c["scope"] == "system"
    jsonschema.validate(c, schema)


# ---- against the fake systemctl: adopt after an engine restart, supervise, remove ---------------------------------
@pytest.fixture
def fake(tmp_path, monkeypatch):
    units, state, bindir = tmp_path / "units", tmp_path / "fakesystemd", tmp_path / "bin"
    shim(bindir, "systemctl", FAKES / "fakesystemctl.py", {"FAKE_SYSTEMD_STATE": state, "FAKE_SYSTEMD_UNITDIR": units})
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    n = {"extract": 0}
    real = sd._extract

    def counting(src, dst):
        n["extract"] += 1
        return real(src, dst)
    monkeypatch.setattr(sd, "_extract", counting)
    data = targz({"app.py": APP.encode(), "release.json": json.dumps({"version": "1", "ready": True}).encode()})
    p = tmp_path / "payload.tar.gz"
    p.write_bytes(data)
    w = wl(dep_id="dep-a", payload_path=str(p), digest=digest_of(data), parameters={},
           restart={"policy": "on-failure", "maxRetries": 3, "restartSec": 0.1})
    ns = type("Fake", (), {})()
    ns.units, ns.bindir, ns.n, ns.wl, ns.data = units, bindir, n, w, tmp_path / "data"
    ns.new = lambda: SystemdDriver("panda", ns.data, unit_dir=units)
    ns.ctl = lambda *a: subprocess.run([str(bindir / "systemctl"), *a], capture_output=True, text=True, timeout=30)
    yield ns
    for u in units.glob("*.service") if units.exists() else []:
        ns.ctl("stop", u.name)


def test_adopt_after_engine_restart_supervise_and_remove(fake):
    d1 = fake.new()
    d1.apply(None, fake.wl)
    unit = d1.unit("dep-a")
    assert (fake.units / unit).exists()
    assert fake.ctl("is-enabled", unit).stdout.strip() == "enabled"
    assert wait_until(lambda: d1.health("dep-a", fake.wl.health), 10)
    # "engine restart": a new instance rebuilds everything from the unit file + systemctl show
    d2 = fake.new()
    o = d2.list()["dep-a"]
    assert o.present and o.running and o.digest == fake.wl.digest and o.version == fake.wl.version
    assert d2.health("dep-a", fake.wl.health) and d2.failed("dep-a") is None
    d2.apply(None, fake.wl)                              # idempotent: no restart, no unpack
    assert fake.n["extract"] == 1
    pid = fake.ctl("show", "-p", "MainPID", "--value", unit).stdout.strip()
    assert d2.supervise() == []
    fake.ctl("kill", "--signal=SIGKILL", unit)           # systemd (here: the fake) restarts it, not the engine
    assert wait_until(lambda: d2.status("dep-a").restarts >= 1 and d2.status("dep-a").running, 10), d2.status("dep-a")
    assert fake.ctl("show", "-p", "MainPID", "--value", unit).stdout.strip() != pid
    evs = fake.new().supervise()                         # yet another instance: the baseline is in the data dir
    assert evs == [{"dep": "dep-a", "event": "restarted", "restarts": 1}], evs
    assert fake.new().supervise() == []
    d2.remove("dep-a")
    assert not (fake.units / unit).exists() and not d2.dep_dir("dep-a").exists()
    assert fake.ctl("is-enabled", unit).stdout.strip() == "disabled"
    assert d2.status("dep-a").present is False
    d2.remove("dep-a")


def test_start_limit_hit_is_failed_and_reported_once(fake, tmp_path):
    data = targz({"app.py": b"import sys; sys.exit(2)\n"})
    p = tmp_path / "crash.tar.gz"
    p.write_bytes(data)
    w = wl(dep_id="dep-c", payload_path=str(p), digest=digest_of(data), parameters={},
           restart={"policy": "on-failure", "maxRetries": 2, "restartSec": 0.05})
    d = fake.new()
    try:
        d.apply(None, w)
    except sd.DriverError:
        pass                                             # it may already be dead when apply looks
    assert wait_until(lambda: d.failed("dep-c") is not None, 10)
    why = d.failed("dep-c")
    assert "start limit hit after 2 restarts" in why, why
    assert contracts.reason_code(why, "failure")["code"] == "FE-SYD-010"
    evs = d.supervise()
    assert {"dep": "dep-c", "event": "start-limit-hit", "restarts": 2} in evs, evs
    assert not [e for e in d.supervise() if e["event"] == "start-limit-hit"]
    d.remove("dep-c")


def test_unreachable_status_is_present_not_running(fake, tmp_path, monkeypatch):
    d = fake.new()
    d.apply(None, fake.wl)
    down = tmp_path / "down"
    failing_shim(down, "systemctl", "Failed to connect to bus: No such file or directory")
    with monkeypatch.context() as mp:
        mp.setenv("PATH", f"{down}{os.pathsep}{os.environ['PATH']}")
        o = fake.new().status("dep-a")
        assert o.present and not o.running and o.detail.startswith("executor unreachable: ") and o.digest
        assert fake.new().failed("dep-a") is None and fake.new().supervise() == []
        with pytest.raises(sd.DriverError):
            fake.new().apply(None, fake.wl)
    time.sleep(0.05)
    d.remove("dep-a")


# ---- config wiring (core/reconciler.build_drivers) ------------------------------------------------------------------
@pytest.mark.parametrize("native,cls", [
    (True, "ProcessDriver"), ({"runtime": "process"}, "ProcessDriver"), (False, None),
    ({"runtime": "systemd", "scope": "user", "unitDir": "/tmp/u"}, "SystemdDriver"), ({}, "SystemdDriver"),
])
def test_build_drivers_native(tmp_path, native, cls):
    from flexengine2.core.reconciler import build_drivers

    out = build_drivers({"deviceId": "panda", "drivers": {"native": native}}, tmp_path)
    assert (type(out["native"]).__name__ if "native" in out else None) == cls
    if cls == "SystemdDriver" and native:
        assert out["native"].scope == "user" and str(out["native"].unit_dir) == "/tmp/u"

"""EV-13 fixes at unit level: torn JSON Lines tail, drift by digest, install-journal resolution, rollback resume,
the process driver's spawning record / orphan reaping, the bundle's empty referrer index for unsigned releases.
The end-to-end schedules are in tests/property/."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from fe_unit_fakes import FakeDriver, _digest, deployment, make_engine, reports, set_desired
from flexengine2.util import repair_jsonl_tail


def _actions(eng) -> list[dict]:
    return [json.loads(line) for line in eng.actions_path.read_text().splitlines()]


# ---- torn tail ---------------------------------------------------------------------------------------------------

def test_repair_jsonl_tail(tmp_path):
    p = tmp_path / "a.jsonl"
    assert repair_jsonl_tail(p) is False                       # absent
    p.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    assert repair_jsonl_tail(p) is False and p.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    p.write_bytes(b'{"a": 1}\n{"b": 2')                         # torn record: cut off
    assert repair_jsonl_tail(p) is True and p.read_bytes() == b'{"a": 1}\n'
    p.write_bytes(b'{"a": 1}\n{"b": 2}')                        # complete, only the newline missing
    assert repair_jsonl_tail(p) is True and p.read_bytes() == b'{"a": 1}\n{"b": 2}\n'
    p.write_bytes(b'{"tor')                                     # a single torn line
    assert repair_jsonl_tail(p) is True and p.read_bytes() == b""


def test_engine_start_cuts_a_torn_action_line(tmp_path):
    eng = make_engine(tmp_path)
    eng.action("apply", "d1", version="1.0.0")
    with open(eng.actions_path, "a") as f:
        f.write('{"t": 1, "action": "remo')
    eng = make_engine(tmp_path, drivers={"native": FakeDriver()})
    eng.action("remove", "d1", version="1.0.0")
    assert [a["action"] for a in _actions(eng)] == ["apply", "remove"]


# ---- reconciler ------------------------------------------------------------------------------------------------

def test_supervise_repairs_drift_by_digest(tmp_path):
    drv = FakeDriver()
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d1", "1.0.0")])
    eng.tick()
    good = eng.state["applied"]["d1"]["wl"]["digest"]
    drv.units["d1"] = {"digest": _digest("other"), "version": "9.9.9"}   # executor runs another release
    eng.supervise()
    assert drv.units["d1"]["digest"] == good
    a = _actions(eng)[-1]
    assert a["action"] == "repair" and a["digest"] == good and a["observed"] == _digest("other")
    r = reports(eng)[-1]
    assert r["state"] == "Installed" and r["reason"] == "drift repaired (executor ran another release)"
    assert r["reasonCode"] == "FE-REC-008"


def test_stale_journal_restores_the_committed_version(tmp_path):
    drv = FakeDriver()
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d1", "1.0.0")])
    eng.tick()
    old = eng.state["applied"]["d1"]["wl"]["digest"]
    # crash after the executor applied 1.1.0, before the commit: journal on disk, 1.1.0 runs
    new = _digest("d1-1.1.0")
    eng.state["journal"] = {"dep": "d1", "digest": new, "version": "1.1.0", "driver": "native",
                            "phase": "applying", "at": time.time()}
    eng.save()
    drv.units["d1"] = {"digest": new, "version": "1.1.0"}
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 3, [deployment("d1", "1.0.0")])            # the operator reverted
    eng.tick()
    assert eng.state["journal"] is None and drv.units["d1"]["digest"] == old
    assert _actions(eng)[-1]["action"] == "repair" and _actions(eng)[-1]["abandoned"] == "1.1.0"
    r = [x for x in reports(eng) if x.get("reason", "").startswith("interrupted")][-1]
    assert r["state"] == "Installed" and r["reasonCode"] == "FE-REC-009"


def test_stale_journal_of_a_dropped_deployment_removes_it(tmp_path):
    drv = FakeDriver()
    eng = make_engine(tmp_path, drivers={"native": drv})
    d = _digest("d1-1.0.0")
    eng.state["journal"] = {"dep": "d1", "digest": d, "version": "1.0.0", "driver": "native",
                            "phase": "applying", "at": time.time()}
    drv.units["d1"] = {"digest": d, "version": "1.0.0"}
    set_desired(eng, 2, [])
    eng.tick()
    assert eng.state["journal"] is None and "d1" not in drv.units
    assert reports(eng)[-1]["state"] == "Removed"


def test_rollback_resumes_after_a_crash_and_never_reapplies(tmp_path):
    drv = FakeDriver()
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 1, [deployment("d1", "1.0.0")])
    eng.tick()
    bad = _digest("d1-2.0.0")
    # crash inside the rollback of 2.0.0: verdict + rolling-back phase durable, executor still runs 2.0.0
    eng.state["failed"]["d1"] = bad
    eng.state["journal"] = {"dep": "d1", "digest": bad, "version": "2.0.0", "driver": "native",
                            "phase": "rolling-back", "reason": "health check failed", "failure": "crashed",
                            "detectSeconds": 0.1, "at": time.time(), "startedAt": time.time()}
    eng.save()
    set_desired(eng, 2, [deployment("d1", "2.0.0")])
    eng.save()
    drv.units["d1"] = {"digest": bad, "version": "2.0.0"}
    eng = make_engine(tmp_path, drivers={"native": drv})
    eng.tick()
    eng.tick()
    acts = _actions(eng)
    assert not [a for a in acts if a["action"] == "apply" and a.get("digest") == bad]
    assert drv.units["d1"]["digest"] == eng.state["applied"]["d1"]["wl"]["digest"] != bad
    assert eng.state["journal"] is None
    f = [r for r in reports(eng) if r["state"] == "Failed"][-1]
    assert f["rolledBackTo"] == "1.0.0" and f["reason"] == "health check failed"


# ---- process driver ------------------------------------------------------------------------------------------

def test_process_driver_readopts_its_unit_after_an_engine_restart(tmp_path):
    """Every platform: Linux identifies the unit's process by its FLEX_UNIT marker in /proc, macOS (no procfs) by pid
    + the start time recorded at spawn. A live process under the unit's pid that is not the unit (a reused pid) is
    not taken for it."""
    from flexengine2.drivers.base import wait_until
    from flexengine2.drivers.process import ProcessDriver

    drv = ProcessDriver("pd-dev", tmp_path)
    d = drv.root / "d1" / "abc"
    d.mkdir(parents=True)
    drv.units["d1"] = {"marker": drv.marker("d1"), "dir": str(d), "digest": "sha256:" + "0" * 64, "version": "1",
                       "port": 1, "parameters": {}, "restart": {}, "restarts": 0, "failures": [],
                       "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "wantRunning": True, "pid": None}
    drv._spawn("d1")
    first = dict(drv.units["d1"])
    try:
        assert wait_until(lambda: drv._alive(first), 5, 0.02)
        drv2 = ProcessDriver("pd-dev", tmp_path)            # the engine restarted: no Popen, only the unit table
        assert drv2.status("d1").running and drv2.units["d1"]["pid"] == first["pid"]
        assert not drv2._alive({**drv2.units["d1"], "pid": os.getpid()})
        drv2.remove("d1")
        assert "d1" not in drv2.units and wait_until(lambda: not drv._alive(first), 5, 0.05)
    finally:
        try:
            os.killpg(first["pid"], 9)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_process_driver_adopts_a_spawning_orphan_and_kills_strangers(tmp_path):
    from flexengine2.drivers.process import ProcessDriver

    drv = ProcessDriver("pd-dev", tmp_path)
    d = drv.root / "d1" / "abc"
    d.mkdir(parents=True)
    x = drv.root / "d2" / "xyz"
    x.mkdir(parents=True)
    drv.units["d1"] = {"marker": drv.marker("d1"), "dir": str(d), "digest": "sha256:" + "0" * 64, "version": "1",
                       "port": 1, "parameters": {}, "restart": {}, "restarts": 0, "failures": [],
                       "wantRunning": True, "pid": None, "spawning": time.time()}
    drv._save()

    def spawn(dep, cwd):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd,
                                env={"FLEX_UNIT": drv.marker(dep), "PATH": os.environ.get("PATH", "")},
                                start_new_session=True)
    p1, p2 = spawn("d1", d), spawn("d2", x)      # d1: killed between Popen and the save; d2: unknown to the table
    try:
        time.sleep(0.3)
        drv2 = ProcessDriver("pd-dev", tmp_path)
        assert drv2.units["d1"]["pid"] == p1.pid and "spawning" not in drv2.units["d1"]
        assert drv2.status("d1").running
        assert p2.wait(5) is not None             # the stranger was killed
    finally:
        for p in (p1, p2):
            try:
                os.killpg(p.pid, 9)
            except ProcessLookupError:
                pass
            p.wait(5)


# ---- artefact store ------------------------------------------------------------------------------------------

def test_bundle_records_an_empty_referrer_index_for_unsigned_releases(tmp_path):
    import io
    import tarfile

    from flexengine2.artefacts.store import ArtefactStore
    from fe_unit_fakes import desired

    dg = _digest("rel")
    doc = desired("dev-a", 1, [deployment("d1", "1.0.0", digest="rel")])
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in (("desired.json", json.dumps(doc).encode()), ("desired.sig", b"sig")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    st = ArtefactStore(tmp_path, None, [])
    st.import_bundle(buf.getvalue())
    idx = tmp_path / "cas" / "referrers" / dg.split(":", 1)[1]
    assert json.loads(idx.read_text()) == {"manifests": []}
    assert st.referrers("r", dg) == []

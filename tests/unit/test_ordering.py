"""EV-42 deployment ordering on one device (`x-flex-dependsOn`) at unit level: install order, the dependency hold at the
GATE (reported once), conditions started/healthy, a failed dependency keeps its dependents on their previous version
(no cascade rollback), reverse order on removal, and crash-resume keeps the order."""

from __future__ import annotations

import json
import time

from fe_unit_fakes import FakeDriver, _digest, deployment, make_engine, reports, set_desired, write_json


class Drv(FakeDriver):
    """FakeDriver whose releases listed in `bad` (digests) fail their health gate."""

    def __init__(self):
        super().__init__()
        self.bad: set[str] = set()

    def health(self, dep_id, probe):
        self._enter("health", dep_id)
        u = self.units.get(dep_id)
        return bool(u) and u["digest"] not in self.bad

    def failed(self, dep_id):
        u = self.units.get(dep_id)
        return "crashed" if u and u["digest"] in self.bad else None


def dep(dep_id, version="1.0.0", after=(), **kw):
    d = deployment(dep_id, version)
    if after:
        d["x-flex-dependsOn"] = [{"deployment": a} if isinstance(a, str) else a for a in after]
    d.update(kw)
    return d


def actions(eng) -> list[tuple]:
    out = []
    for line in eng.actions_path.read_text().splitlines():
        a = json.loads(line)
        out.append((a["action"], a["dep"], a.get("version")))
    return out


def last(eng, dep_id) -> dict:
    return [r for r in reports(eng) if r.get("dep") == dep_id][-1]


def applied(eng) -> dict:
    return {k: v["wl"]["version"] for k, v in eng.state["applied"].items()}


def engine(tmp_path, safe: dict | None = None):
    drv = Drv()
    kw = {}
    if safe is not None:
        kw["safeStateFile"] = str(write_json(tmp_path / "safe.json", safe))
    return make_engine(tmp_path, drivers={"native": drv}, **kw), drv


def test_installs_in_dependency_order_in_one_tick(tmp_path):
    eng, drv = engine(tmp_path)
    # a listed first, depends on b; c depends on a (started)
    set_desired(eng, 1, [dep("c", after=[{"deployment": "a", "condition": "started"}]), dep("a", after=["b"]), dep("b")])
    eng.tick()
    applies = [x[1] for x in actions(eng) if x[0] == "apply"]
    assert applies == ["b", "a", "c"]
    assert applied(eng) == {"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}
    assert not [r for r in reports(eng) if r["state"] == "Pending"]


def test_dependent_held_at_the_gate_until_dependency_installed(tmp_path):
    eng, drv = engine(tmp_path, safe={"operator_confirmed": False})
    set_desired(eng, 1, [dep("a", after=["b"]), dep("b", **{"x-flex-activation": {"requires": ["operator_confirmed"]}})])
    for _ in range(4):
        eng.tick()
    assert applied(eng) == {} and drv.units == {}
    pend = [r for r in reports(eng) if r["dep"] == "a" and r["state"] == "Pending"]
    assert len(pend) == 1, pend                                   # reported once, re-checked every tick
    assert pend[0]["reason"] == "held: waiting for b" and pend[0]["reasonCode"] == "FE-GATE-004"
    assert pend[0]["waitingFor"] == ["b"] and pend[0]["version"] == "1.0.0"
    assert last(eng, "b")["reasonCode"] == "FE-GATE-001"
    gate = [h for h in eng.history.recent() if h.get("dep") == "a" and h.get("state") == "GATE"]
    assert len(gate) == 1, gate
    write_json(tmp_path / "safe.json", {"operator_confirmed": True})
    eng.tick()
    assert [x[1] for x in actions(eng) if x[0] == "apply"] == ["b", "a"]
    assert last(eng, "a")["state"] == "Installed" and last(eng, "b")["state"] == "Installed"


def test_condition_healthy_waits_for_health_started_does_not(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("b")])
    eng.tick()
    eng.state["health"]["b"] = False                            # supervision saw b unhealthy
    set_desired(eng, 2, [dep("b"), dep("a", after=["b"]), dep("s", after=[{"deployment": "b", "condition": "started"}])])
    eng.tick()
    assert "a" not in eng.state["applied"] and eng.state["applied"]["s"]["wl"]["version"] == "1.0.0"
    assert last(eng, "a")["reason"] == "held: waiting for b"
    eng.state["health"]["b"] = True                             # healthy again
    eng.tick()
    assert applied(eng)["a"] == "1.0.0"


def test_failed_dependency_keeps_dependents_on_their_previous_version(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", after=["b"]), dep("b")])
    eng.tick()
    assert applied(eng) == {"a": "1.0.0", "b": "1.0.0"}
    drv.bad.add(_digest("b-2.0.0"))
    set_desired(eng, 2, [dep("a", "1.1.0", after=["b"]), dep("b", "2.0.0")])
    n0 = len(actions(eng))
    for _ in range(3):
        eng.tick()
    fb = last(eng, "b")
    assert fb["state"] == "Failed" and fb["rolledBackTo"] == "1.0.0" and fb["rollbackHealthy"] is True
    # a: no new apply, stays on 1.0.0 (no cascade either way: the dependency's rollback does not touch it)
    assert applied(eng) == {"a": "1.0.0", "b": "1.0.0"} and drv.units["a"]["version"] == "1.0.0"
    assert [x for x in actions(eng)[n0:] if x[1] == "a"] == []
    pa = last(eng, "a")
    assert pa["state"] == "Pending" and pa["reason"] == "held: waiting for b" and pa["version"] == "1.1.0"
    assert len([r for r in reports(eng) if r["dep"] == "a" and r["state"] == "Pending"]) == 1
    # the operator reverts b to what runs: a's update goes ahead
    set_desired(eng, 3, [dep("a", "1.1.0", after=["b"]), dep("b", "1.0.0")])
    eng.tick()
    assert applied(eng) == {"a": "1.1.0", "b": "1.0.0"}


def test_dependency_rollback_does_not_cascade_to_an_installed_dependent(tmp_path):
    """a already runs its new version; b's later update fails and rolls back; a is left alone."""
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", "1.1.0", after=["b"]), dep("b")])
    eng.tick()
    drv.bad.add(_digest("b-2.0.0"))
    set_desired(eng, 2, [dep("a", "1.1.0", after=["b"]), dep("b", "2.0.0")])
    n0 = len(actions(eng))
    eng.tick()
    assert [x for x in actions(eng)[n0:] if x[1] == "a"] == []
    assert applied(eng) == {"a": "1.1.0", "b": "1.0.0"} and last(eng, "b")["state"] == "Failed"


def test_removal_in_reverse_dependency_order(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("b"), dep("c", after=["a"]), dep("a", after=["b"]), dep("x")])
    eng.tick()
    set_desired(eng, 2, [dep("x")])
    eng.tick()
    removes = [x[1] for x in actions(eng) if x[0] == "remove"]
    assert removes == ["c", "a", "b"]
    assert applied(eng) == {"x": "1.0.0"}


def test_removal_waits_for_a_held_dependent_removal(tmp_path):
    eng, drv = engine(tmp_path, safe={"machine_parked": True})
    set_desired(eng, 1, [dep("a", after=["b"], **{"x-flex-activation": {"requires": ["machine_parked"]}}), dep("b")])
    eng.tick()
    write_json(tmp_path / "safe.json", {"machine_parked": False})
    set_desired(eng, 2, [])
    for _ in range(3):
        eng.tick()
    assert set(drv.units) == {"a", "b"}
    pb = [r for r in reports(eng) if r["dep"] == "b" and r["state"] == "Pending"]
    assert len(pb) == 1 and pb[0]["reason"] == "held: waiting for removal of a" and pb[0]["operation"] == "remove"
    assert pb[0]["reasonCode"] == "FE-GATE-005" and pb[0]["waitingFor"] == ["a"]
    assert last(eng, "a")["reasonCode"] == "FE-GATE-001"
    write_json(tmp_path / "safe.json", {"machine_parked": True})
    eng.tick()
    assert drv.units == {} and [x[1] for x in actions(eng) if x[0] == "remove"] == ["a", "b"]


def test_dependency_only_change_does_not_reinstall(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a"), dep("b")])
    eng.tick()
    n0 = len(actions(eng))
    set_desired(eng, 2, [dep("a", after=["b"]), dep("b")])
    eng.tick()
    assert actions(eng)[n0:] == []


def test_crash_resume_does_not_run_a_dependent_ahead_of_its_dependency(tmp_path):
    """Crash while a's update 1.1.0 was applied (journal open). The next desired state keeps a@1.1.0 but also moves b
    to a release that is not ready yet: a's uncommitted release is taken off (FE-REC-010) and installed again only
    after b."""
    eng, drv = engine(tmp_path, safe={"operator_confirmed": True})
    set_desired(eng, 1, [dep("a", after=["b"]), dep("b")])
    eng.tick()
    new = _digest("a-1.1.0")
    eng.state["journal"] = {"dep": "a", "digest": new, "version": "1.1.0", "driver": "native", "phase": "applying",
                            "at": time.time()}
    eng.save()
    drv.units["a"] = {"digest": new, "version": "1.1.0"}
    eng = make_engine(tmp_path, drivers={"native": drv}, safeStateFile=str(tmp_path / "safe.json"))
    write_json(tmp_path / "safe.json", {"operator_confirmed": False})
    set_desired(eng, 2, [dep("a", "1.1.0", after=["b"]),
                         dep("b", "1.1.0", **{"x-flex-activation": {"requires": ["operator_confirmed"]}})])
    eng.tick()
    assert eng.state["journal"] is None
    assert drv.units["a"]["version"] == "1.0.0" and applied(eng) == {"a": "1.0.0", "b": "1.0.0"}
    rs = [r for r in reports(eng) if r["dep"] == "a"]
    assert any(r["state"] == "Installed" and r.get("reasonCode") == "FE-REC-010"
               and r["reason"] == "interrupted install of 1.1.0 held: waiting for b" for r in rs), rs
    assert rs[-1]["state"] == "Pending" and rs[-1]["reasonCode"] == "FE-GATE-004"
    write_json(tmp_path / "safe.json", {"operator_confirmed": True})
    n0 = len(actions(eng))
    eng.tick()
    assert [x[1:] for x in actions(eng)[n0:] if x[0] == "apply"] == [("b", "1.1.0"), ("a", "1.1.0")]
    assert applied(eng) == {"a": "1.1.0", "b": "1.1.0"}


def test_crash_resume_first_install_ahead_of_dependency_is_removed(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("b")])
    eng.tick()
    na = _digest("a-1.0.0")
    eng.state["journal"] = {"dep": "a", "digest": na, "version": "1.0.0", "driver": "native", "phase": "applying",
                            "at": time.time()}
    eng.save()
    drv.units["a"] = {"digest": na, "version": "1.0.0"}
    drv.bad.add(_digest("b-2.0.0"))
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 2, [dep("a", after=["b"]), dep("b", "2.0.0")])
    eng.tick()
    ra = [r for r in reports(eng) if r["dep"] == "a"]
    assert ra[0]["state"] == "Removed" and ra[0]["reasonCode"] == "FE-REC-010", ra
    assert last(eng, "b")["state"] == "Failed" and "a" not in drv.units and "a" not in eng.state["applied"]
    assert ra[-1]["state"] == "Pending" and ra[-1]["reason"] == "held: waiting for b"


def test_journal_resumed_when_dependencies_are_ready(tmp_path):
    """The ordinary crash-resume (EV-13) is unchanged when the dependency is ready: adopt, no second apply."""
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", after=["b"]), dep("b")])
    eng.tick()
    new = _digest("a-1.1.0")
    eng.state["journal"] = {"dep": "a", "digest": new, "version": "1.1.0", "driver": "native", "phase": "applying",
                            "at": time.time()}
    eng.save()
    drv.units["a"] = {"digest": new, "version": "1.1.0"}
    eng = make_engine(tmp_path, drivers={"native": drv})
    set_desired(eng, 2, [dep("a", "1.1.0", after=["b"]), dep("b")])
    n0 = len(actions(eng))
    eng.tick()
    assert [x[0] for x in actions(eng)[n0:] if x[1] == "a"] == ["adopt", "commit"] or \
        [x[0] for x in actions(eng)[n0:] if x[1] == "a"] == ["adopt"]
    assert applied(eng)["a"] == "1.1.0"

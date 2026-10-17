"""EV-42 end to end: deployment ordering on one device through the reference fake cloud's plan API (`dependsOn` ->
x-flex-dependsOn) and a real engine process (`python -m flexengine2.testing.single`) with the native process driver.

A depends on B (healthy): B installs first, A waits ("held: waiting for b", FE-GATE-004) while B is held at its own
safe-state gate; B's update fails its health gate -> B rolls back, A stays on its previous version (held); removal
runs A before B. Native releases only: no Docker needed."""

from __future__ import annotations

import time

import pytest

from flexengine2.artefacts.oci import Layout
from flexengine2.identity import crypto
from flexengine2.testing.release import make_release, model_safety, module_hashes

from .p2lab import MODEL, Cloud, EngineProc, item

VERSIONS = [("1.0.0", False), ("1.1.0", False), ("1.2.0", True)]   # 1.2.0 fails its health gate


@pytest.fixture(scope="module")
def native_lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("ordering")
    keys = {"release": crypto.generate(), "cloud": crypto.generate()}
    oci = root / "oci"
    layout = Layout(oci)
    hashes, classes = module_hashes(MODEL), model_safety(MODEL)
    for version, fail in VERSIONS:
        make_release(layout, root / "work", keys["release"], model_dir=MODEL, module="detection_logger",
                     version=version, profile="native", fail_health=fail, hashes=hashes, classes=classes)
    return {"keys": keys, "registry": {"dir": oci}, "root": root}


@pytest.fixture
def lab(native_lab, tmp_path):
    cloud = Cloud(tmp_path, native_lab["keys"], native_lab["registry"])
    engines: list[EngineProc] = []

    def engine(dev: str) -> EngineProc:
        e = EngineProc(tmp_path, cloud, native_lab["keys"], dev, poll=0.3, extra={"drivers": {"native": True}})
        e.set_safe(operator_confirmed=False)
        engines.append(e.start())
        return e

    yield cloud, engine
    for e in engines:
        e.cleanup()
    cloud.stop()


def dl(dep_id: str, version: str, **kw) -> dict:
    return item("detection_logger", version, "native", dep_id, **kw)


def applies(eng: EngineProc, since: int = 0) -> list[tuple]:
    return [(a["action"], a["dep"], a.get("version")) for a in eng.actions()[since:]
            if a["action"] in ("apply", "commit", "remove", "rollback")]


def test_dependency_order_hold_failure_and_removal(lab):
    cloud, engine = lab
    eng = engine("ord-dev")
    gated = {"activation": {"requires": ["operator_confirmed"]}}
    healthy_b = [{"deployment": "b", "condition": "healthy"}]

    # 1. a listed first and depends on b; b is held at its safe-state gate -> a waits for b
    p = cloud.deploy("ord-dev", [dl("a", "1.0.0", dependsOn=healthy_b), dl("b", "1.0.0", **gated)])
    desired = cloud.st.desired_doc("ord-dev")
    assert {d["metadata"]["id"]: d.get("x-flex-dependsOn") for d in desired["deployments"]} == \
        {"a": healthy_b, "b": None}, p
    st = cloud.wait(lambda: (s := cloud.status("ord-dev")).get("a", {}).get("state") == "Pending"
                    and s.get("b", {}).get("state") == "Pending" and s, 30, what="a and b Pending")
    assert st["a"]["reason"] == "held: waiting for b" and st["a"]["reasonCode"] == "FE-GATE-004"
    assert st["a"]["waitingFor"] == ["b"] and st["b"]["reasonCode"] == "FE-GATE-001"
    time.sleep(1.5)                                              # several ticks: still held, reported once
    log = [r for r in cloud.st.s["statusLog"] if r.get("dep") == "a" and r.get("deviceId", "ord-dev") == "ord-dev"]
    assert [r["state"] for r in log] == ["Pending"], log
    assert eng.units() == {}

    eng.set_safe(operator_confirmed=True)
    cloud.wait_converged("ord-dev", {"a": "1.0.0", "b": "1.0.0"}, timeout=60)
    acts = applies(eng)
    assert acts.index(("apply", "b", "1.0.0")) < acts.index(("apply", "a", "1.0.0")), acts
    commit_b = acts.index(("commit", "b", None)) if ("commit", "b", None) in acts else None
    if commit_b is not None:                                     # b passed its health gate before a started
        assert commit_b < acts.index(("apply", "a", "1.0.0")), acts

    # 2. b's update fails its health gate and rolls back; a's update waits on 1.0.0 (no apply, no cascade)
    n0 = len(eng.actions())
    cloud.deploy("ord-dev", [dl("a", "1.1.0", dependsOn=healthy_b), dl("b", "1.2.0")])
    st = cloud.wait(lambda: (s := cloud.status("ord-dev")).get("b", {}).get("state") == "Failed"
                    and s.get("a", {}).get("state") == "Pending" and s, 60, what="b Failed, a held")
    assert st["b"]["rolledBackTo"] == "1.0.0" and st["b"]["rollbackHealthy"] is True
    assert st["a"]["reason"] == "held: waiting for b" and st["a"]["version"] == "1.1.0"
    time.sleep(1.5)
    assert [x for x in applies(eng, n0) if x[1] == "a"] == []
    units = eng.units()
    assert sorted(u["version"] for u in units.values()) == ["1.0.0", "1.0.0"], units

    # 3. the operator reverts b: a's update goes ahead
    cloud.deploy("ord-dev", [dl("a", "1.1.0", dependsOn=healthy_b), dl("b", "1.0.0")])
    cloud.wait_converged("ord-dev", {"a": "1.1.0", "b": "1.0.0"}, timeout=60)

    # 4. removal in reverse order: a (the dependent) before b
    n0 = len(eng.actions())
    cloud.deploy("ord-dev", [])
    cloud.wait_converged("ord-dev", {}, timeout=60)
    removes = [x[1] for x in applies(eng, n0) if x[0] == "remove"]
    assert removes == ["a", "b"], removes


def test_plan_with_a_dependency_cycle_is_refused(lab):
    cloud, _ = lab
    s, p = cloud.admin("POST", "/api/v1/admin/plans", {"deviceId": "nowhere", "allowUnknownDevice": True,
                                                       "deployments": [dl("a", "1.0.0", dependsOn=[{"deployment": "b"}]),
                                                                       dl("b", "1.0.0", dependsOn=[{"deployment": "a"}])]})
    assert s == 422 and "dependency cycle" in str(p), (s, p)

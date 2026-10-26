"""EV-42 `x-flex-dependsOn` (CP-1 additive field): schema, the semantic rules (no unknown ids, no duplicates, no cycles),
the engine's refusal of a document that breaks them (FE-DS-006, as a whole, the previous state keeps running), the
registered hold codes, and the reference cloud's plan item `dependsOn`."""

from __future__ import annotations

import copy
import json

from flexengine2 import contracts
from flexengine2.core.reconciler import Engine
from flexengine2.identity import crypto
from flexengine2.util import canonical_json

from _api_helpers import VECTORS
from test_engine_contracts import admin, fake_cloud, vkey  # noqa: F401  (fixture re-export)

DESIRED = json.loads((VECTORS / "manifest" / "desired.json").read_text())


def _with(*pairs) -> dict:
    """DESIRED with x-flex-dependsOn set per deployment index: _with((0, [...]), (1, [...]))."""
    doc = copy.deepcopy(DESIRED)
    for i, deps in pairs:
        doc["deployments"][i]["x-flex-dependsOn"] = deps
    return doc


def _signed(vectors, doc) -> tuple[bytes, str]:
    body = canonical_json(doc)
    return body, crypto.sign(vkey(vectors, "cloud"), body)


def test_schema_accepts_the_field_and_defaults_condition():
    doc = _with((0, [{"deployment": "safety-app"}]), (1, []))
    assert contracts.errors("desired-state", doc) == []
    doc = _with((0, [{"deployment": "safety-app", "condition": "started"}]))
    assert contracts.errors("desired-state", doc) == []
    dep = contracts.load("desired-state")["$defs"]["DependsOn"]
    assert dep["items"]["properties"]["condition"]["default"] == "healthy"
    assert contracts.depends_on({"x-flex-dependsOn": [{"deployment": "x"}]}) == [("x", "healthy")]


def test_semantic_rules():
    assert contracts.dependency_errors(_with((0, [{"deployment": "ghost"}]))) == [
        "$.deployments[0].x-flex-dependsOn[0].deployment: unknown deployment 'ghost' (not in this desired state)"]
    assert "depends on itself" in contracts.dependency_errors(_with((0, [{"deployment": "native-app"}])))[0]
    errs = contracts.dependency_errors(_with((0, [{"deployment": "safety-app"}]), (1, [{"deployment": "native-app"}])))
    assert errs == ["$.deployments: dependency cycle native-app -> safety-app -> native-app"]


def test_cycle_detection_and_order_helpers():
    g = {"a": ["b"], "b": ["c"], "c": ["a"], "d": []}
    assert contracts.dependency_cycle(g) == ["a", "b", "c", "a"]
    assert contracts.dependency_cycle({"a": ["b"], "b": [], "c": ["a", "b"]}) is None

    def d(i, *after):
        return {"metadata": {"id": i}, "x-flex-dependsOn": [{"deployment": a} for a in after]}
    order = contracts.dependency_order([d("a", "b"), d("c", "a", "x"), d("b"), d("e")])
    assert [x["metadata"]["id"] for x in order] == ["b", "a", "c", "e"]
    cyc = contracts.dependency_order([d("a", "b"), d("b", "a")])          # tolerated (refused upstream)
    assert sorted(x["metadata"]["id"] for x in cyc) == ["a", "b"]


def test_engine_refuses_a_cyclic_desired_state_as_a_whole(engine_cfg, vectors):
    eng = Engine(engine_cfg(cloudPublicKey=vectors["keys"]["cloud"]["public"]), {})
    assert eng.accept_manifest(*_signed(vectors, DESIRED), etag=None) is True
    bad = _with((0, [{"deployment": "safety-app"}]), (1, [{"deployment": "native-app"}]))
    bad["manifestVersion"] = DESIRED["manifestVersion"] + 1
    body, sig = _signed(vectors, bad)
    for _ in range(2):
        assert eng.accept_manifest(body, sig, etag=None) is False
    assert eng.state["desired"] == DESIRED and eng.state["manifestVersion"] == DESIRED["manifestVersion"]
    ev = [r for r in eng.state["outbox"] if r.get("dep") is None]
    assert len(ev) == 1, ev
    e = {k: v for k, v in ev[0].items() if k != "dep"}
    contracts.validate("device-event", e)
    assert e["reasonCode"] == "FE-DS-006" and "dependency cycle native-app -> safety-app -> native-app" in e["reason"]
    assert e["reason"].startswith("desired state has invalid deployment dependencies: ")
    # an unknown id is the same refusal; a document with other CP-1 errors too keeps the generic FE-DS-005
    unknown = _with((0, [{"deployment": "ghost"}]))
    unknown["manifestVersion"] = DESIRED["manifestVersion"] + 2
    assert eng.accept_manifest(*_signed(vectors, unknown), etag=None) is False
    assert eng.state["lastRefusal"]["reasonCode"] == "FE-DS-006"
    mixed = copy.deepcopy(unknown)
    mixed["deployments"][1]["x-flex-driver"] = "teleporter"
    assert eng.accept_manifest(*_signed(vectors, mixed), etag=None) is False
    assert eng.state["lastRefusal"]["reasonCode"] == "FE-DS-005"


def test_hold_reasons_are_registered():
    for text, code in [("held: waiting for b", "FE-GATE-004"), ("held: waiting for a, b", "FE-GATE-004"),
                       ("held: waiting for removal of a", "FE-GATE-005"),
                       ("interrupted install of 1.1.0 held: waiting for b", "FE-REC-010"),
                       ("desired state has invalid deployment dependencies: $.deployments: dependency cycle a -> b -> a",
                        "FE-DS-006")]:
        assert contracts.reason_code(text)["code"] == code, text
    rec = {"dep": "a", "seq": 1, "state": "Pending", "deviceTime": 1.0, "manifestVersion": 2,
           "reason": "held: waiting for b", "reasonCode": "FE-GATE-004", "version": "1.0.0", "waitingFor": ["b"]}
    contracts.validate("status", rec)


def test_reference_cloud_serves_plan_item_depends_on(fake_cloud):  # noqa: F811
    items = [{"id": "native-app", "module": "vec_native", "version": "1.0.0", "driver": "native",
              "dependsOn": [{"deployment": "second", "condition": "started"}]},
             {"id": "second", "module": "vec_native", "version": "1.0.0", "driver": "native"}]
    s, p = admin(fake_cloud, "POST", "/api/v1/admin/plans", {"deviceId": "vec-dev", "allowUnknownDevice": True,
                                                             "deployments": items})
    assert s == 201, p
    s, b = admin(fake_cloud, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 200, b
    served = fake_cloud.state.desired_doc("vec-dev")
    assert contracts.errors("desired-state", served) == []
    by = {d["metadata"]["id"]: d for d in served["deployments"]}
    assert by["native-app"]["x-flex-dependsOn"] == [{"deployment": "second", "condition": "started"}]
    assert "x-flex-dependsOn" not in by["second"]


def test_reference_cloud_refuses_a_cyclic_or_dangling_plan(fake_cloud):  # noqa: F811
    for deps in ([{"id": "x", "dependsOn": [{"deployment": "y"}]}, {"id": "y", "dependsOn": [{"deployment": "x"}]}],
                 [{"id": "x", "dependsOn": [{"deployment": "ghost"}]}],
                 [{"id": "x", "dependsOn": [{"deployment": "x", "condition": "soon"}]}]):
        items = [{"module": "vec_native", "version": "1.0.0", "driver": "native", **d} for d in deps]
        s, p = admin(fake_cloud, "POST", "/api/v1/admin/plans", {"deviceId": "vec-dev", "allowUnknownDevice": True,
                                                                 "deployments": items})
        assert s == 422 and "CP-1" in json.dumps(p), (s, p)

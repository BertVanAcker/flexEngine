"""Desired-state refusals and reason codes (lead decisions 2026-10-17):

* every desired state is checked against CP-1 (desired-state.v1alpha1) before it is accepted;
* a refused document is reported once per distinct (document, reason) as a device event (POST /devices/{id}/events)
  and kept as `lastRefusal` for the local API; the previous state keeps running;
* every status record with a reason carries its stable `reasonCode`.
"""

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


def _engine(engine_cfg, vectors, **kw):
    return Engine(engine_cfg(cloudPublicKey=vectors["keys"]["cloud"]["public"], **kw), {})


def _signed(vectors, doc) -> tuple[bytes, str]:
    body = canonical_json(doc)
    return body, crypto.sign(vkey(vectors, "cloud"), body)


def _events(eng):
    return [r for r in eng.state["outbox"] if r.get("dep") is None]


def test_valid_desired_state_is_accepted(engine_cfg, vectors):
    eng = _engine(engine_cfg, vectors)
    assert eng.accept_manifest(*_signed(vectors, DESIRED), etag=None) is True
    assert eng.state["manifestVersion"] == DESIRED["manifestVersion"] and not _events(eng)


def test_schema_invalid_document_is_refused_and_reported_once(engine_cfg, vectors):
    eng = _engine(engine_cfg, vectors)
    bad = copy.deepcopy(DESIRED)
    bad["deployments"][0]["x-flex-driver"] = "teleporter"          # not a driver profile of CP-1
    del bad["deployments"][0]["spec"]["applicationId"]
    body, sig = _signed(vectors, bad)
    for _ in range(3):                                              # the cloud keeps serving it
        assert eng.accept_manifest(body, sig, etag=None) is False
    assert eng.state["desired"] is None and eng.state["manifestVersion"] == 0
    ev = _events(eng)
    assert len(ev) == 1, ev
    e = {k: v for k, v in ev[0].items() if k != "dep"}
    contracts.validate("device-event", e)
    assert e["event"] == "DesiredStateRefused" and e["reasonCode"] == "FE-DS-005"
    assert e["reason"].startswith("desired state does not match the CP-1 schema: ")
    lr = eng.state["lastRefusal"]
    assert lr["count"] == 3 and lr["reasonCode"] == "FE-DS-005" and lr["acceptedManifestVersion"] == 0


def test_bad_signature_other_device_and_downgrade_are_reported(engine_cfg, vectors):
    eng = _engine(engine_cfg, vectors)
    body, sig = _signed(vectors, DESIRED)
    assert eng.accept_manifest(body, "AAAA", etag=None) is False                       # bad signature
    other = dict(DESIRED, deviceId="someone-else")
    other["deployments"] = [dict(d, metadata=dict(d["metadata"], deviceId="someone-else")) for d in DESIRED["deployments"]]
    assert eng.accept_manifest(*_signed(vectors, other), etag=None) is False             # another device
    assert eng.accept_manifest(body, sig, etag=None) is True
    older = dict(DESIRED, manifestVersion=DESIRED["manifestVersion"] - 1)
    assert eng.accept_manifest(*_signed(vectors, older), etag=None) is False             # downgrade
    codes = [r["reasonCode"] for r in _events(eng)]
    assert codes == ["FE-DS-001", "FE-DS-002", "FE-DS-003"], codes
    assert eng.state["manifestVersion"] == DESIRED["manifestVersion"]


def test_not_json_is_refused(engine_cfg, vectors):
    eng = _engine(engine_cfg, vectors)
    body = b"{not json"
    assert eng.accept_manifest(body, crypto.sign(vkey(vectors, "cloud"), body), etag=None) is False
    assert _events(eng)[0]["reasonCode"] == "FE-DS-004"


def test_status_records_carry_reason_codes(engine_cfg, vectors):
    eng = _engine(engine_cfg, vectors)
    eng.report("app", "Pending", reason="awaiting safe state: machine_parked")
    eng.report("app", "Failed", reason="artefact rejected: release not signed by a trusted key (no signature)")
    eng.report("app", "Installed", version="1.0.0")
    recs = eng.state["outbox"]
    assert [r.get("reasonCode") for r in recs] == ["FE-GATE-001", "FE-REC-002", None]
    for r in recs:
        contracts.validate("status", r)


def test_device_event_reaches_the_cloud_and_shares_the_seq_space(fake_cloud, engine_cfg, vectors):  # noqa: F811
    s, tok = admin(fake_cloud, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": "vec-dev"})
    eng = _engine(engine_cfg, vectors, apiUrl=fake_cloud.url, onboardingToken=tok["token"])
    eng.onboard()
    body = b"{not json"
    eng.accept_manifest(body, crypto.sign(vkey(vectors, "cloud"), body), etag=None)
    eng.report("app", "Pending", reason="awaiting safe state: machine_parked")
    eng.flush_outbox()
    assert eng.state["outbox"] == []
    evs = fake_cloud.state.s.get("events", [])
    assert [e["reasonCode"] for e in evs] == ["FE-DS-004"], evs
    seqs = [e["seq"] for e in evs] + [r["seq"] for r in fake_cloud.state.s["statusLog"]]
    assert sorted(seqs) == list(range(min(seqs), min(seqs) + len(seqs)))   # one seq space, no gaps


def test_local_api_shows_the_last_refusal(engine_cfg, vectors, tmp_path):
    from flexengine2.local.api import LocalApi, OfflineProvider

    eng = _engine(engine_cfg, vectors)
    body = b"{not json"
    eng.accept_manifest(body, crypto.sign(vkey(vectors, "cloud"), body), etag=None)
    cfg = {"dataDir": str(tmp_path), "cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": vectors["keys"]["cloud"]["public"]},
           "devices": [{"deviceId": "vec-dev", "dataDir": eng_dir(eng)}]}
    r = LocalApi(OfflineProvider(cfg), socket_path=None, log=lambda m: None).handle("GET", "/v1/status")
    doc = json.loads(r.body)
    contracts.validate("cli-status", doc)
    dev = doc["devices"][0]
    assert dev["lastRefusal"]["reasonCode"] == "FE-DS-004" and dev["lastRefusal"]["count"] == 1


def eng_dir(eng) -> str:
    return str(eng.dir)

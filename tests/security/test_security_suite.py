"""EV-12 security suite (layer 4, docs/flexengine-v2-plan.md §7.2): the index of every refusal flexEngineV2 and its
reference cloud must make, each with its precise reason code (CP-2 registry, `contracts.reason_code`) where one
exists, and with proof that nothing reached an executor (FakeDriver call log empty since the baseline, and no
apply/remove/rollback/repair/commit/adopt in actions.jsonl).

Cases already covered elsewhere (tests/api/test_vectors.py, test_refusals.py, tests/security/test_tls.py,
test_keys.py, test_secrets.py, P2 scenarios f/g/a) are repeated here end to end (real engine, reference fake cloud,
capturing/MITM proxy from tests/api/_conformance.py), so this file is the layer's single index:

    section          case (INDEX key)                         refused with
    release          tampered blob / unsigned / unknown key / bad signature / single approver /
                     class downgrade / class upgrade           Failed, FE-REC-002 + FE-ART-* detail
    desired state    tampered body / rogue signature / no signature / not JSON / schema-invalid / other device /
                     downgrade / same version other body       DesiredStateRefused event, FE-DS-00x
    cloud            replayed nonce (client + raw) / stale + future X-Flex-Date / unsigned / bad signature /
                     unknown device / other device's resources (403) / self-approval     401/403, FE-CLD-001
    identity         plain HTTP without lab flag / https without CA / TLS wrong CA, wrong host, expired /
                     key file or key dir permissions / token file permissions / token reuse (file, value,
                     other device)                            typed exception (no code exists) or FE-CLD-001
    bundle           tampered blob / garbage / no desired state / traversal + oversized member / bad signature /
                     another device's bundle                  FE-ART-DIG-001, FE-BUN-001, FE-DS-001/002
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))   # shared helpers of the API layer

from flexengine2 import contracts  # noqa: E402
from flexengine2.artefacts.store import VerificationError  # noqa: E402
from flexengine2.cloud import client as client_mod  # noqa: E402
from flexengine2.cloud.client import Rejected, TlsConfigError, TlsError  # noqa: E402
from flexengine2.core.reconciler import Engine  # noqa: E402
from flexengine2.identity import crypto  # noqa: E402
from flexengine2.identity.keys import KeyPermissionError  # noqa: E402
from flexengine2.identity.secrets import SecretPermissionError, TokenConsumedError  # noqa: E402
from flexengine2.util import canonical_json  # noqa: E402

from _api_helpers import VECTORS  # noqa: E402
from _conformance import (CaptureProxy, VersionFakeDriver, actions, admin, deploy, engine_cfg,  # noqa: E402
                          inject_desired, publish, start_cloud, token, vkey)

# Expected code per case: the reason code (registry) or, where the registry has none, the exception type.
INDEX = {
    # release verification (outer reason FE-REC-002 "artefact rejected: <detail>")
    "tampered-blob": "FE-ART-DIG-002",
    "unsigned-release": "FE-ART-SIG-001",
    "unknown-release-key": "FE-ART-SIG-002",          # inner: FE-ART-SIG-004 untrusted key
    "bad-release-signature": "FE-ART-SIG-002",        # inner: FE-ART-SIG-005 bad signature
    "single-approver-safety-release": "FE-ART-SAF-001",
    "safety-class-downgrade": "FE-ART-SAF-002",
    "safety-class-upgrade-unapproved": "FE-ART-SAF-001",
    # desired state (device event DesiredStateRefused)
    "desired-tampered-body": "FE-DS-001",
    "desired-rogue-signature": "FE-DS-001",
    "desired-missing-signature": "FE-DS-001",
    "desired-vector-tampered": "FE-DS-001",
    "desired-vector-wrong-key": "FE-DS-001",
    "desired-vector-missing": "FE-DS-001",
    "desired-not-json": "FE-DS-004",
    "desired-schema-invalid": "FE-DS-005",
    "desired-other-device": "FE-DS-002",
    "desired-downgrade": "FE-DS-003",
    "desired-same-version-other-body": "FE-DS-003",
    # cloud-side request checks (engine sees Rejected "<status>: <problem>")
    "replayed-nonce": "FE-CLD-001",
    "replayed-request-raw": "FE-CLD-001",
    "stale-date": "FE-CLD-001",
    "future-date": "FE-CLD-001",
    "unsigned-request": "FE-CLD-001",
    "bad-request-signature": "FE-CLD-001",
    "unknown-device": "FE-CLD-001",
    "other-device-resources": "FE-CLD-001",
    "self-approval": "FE-CLD-001",
    # identity / transport / secrets (no registered code: typed exceptions)
    "plain-http-without-lab-flag": "TlsConfigError",
    "https-without-ca": "TlsConfigError",
    "tls-wrong-ca": "TlsError",
    "tls-wrong-host": "TlsError",
    "tls-expired": "TlsError",
    "key-file-permissions": "KeyPermissionError",
    "key-dir-permissions": "KeyPermissionError",
    "token-file-permissions": "SecretPermissionError",
    "token-file-reuse": "TokenConsumedError",
    "token-value-reuse": "FE-CLD-001",
    "token-other-device": "FE-CLD-001",
    # offline bundle
    "bundle-tampered-blob": "FE-ART-DIG-001",
    "bundle-garbage": "ReadError",
    "bundle-no-desired-state": "FE-BUN-001",
    "bundle-traversal-member": "FE-ART-DIG-001",
    "bundle-oversized-member": "FE-ART-DIG-001",
    "bundle-bad-signature": "FE-DS-001",
    "bundle-other-device": "FE-DS-002",
}
EXECUTED: set[str] = set()
DRIVER_ACTIONS = {"apply", "remove", "rollback", "repair", "commit", "adopt"}
BASE = {"id": "base", "module": "sec_base", "version": "1.0.0", "driver": "native"}


def code_of(text: str, field: str | None = None) -> str | None:
    e = contracts.reason_code(text, field)
    return e["code"] if e else None


def expect(case: str, got: str | None):
    assert got == INDEX[case], (case, got)
    EXECUTED.add(case)


# ---- lab -------------------------------------------------------------------------------------------------------

class Dev:
    def __init__(self, lab, eng: Engine, drv: VersionFakeDriver, data: Path):
        self.lab, self.eng, self.drv, self.data, self.dev = lab, eng, drv, data, eng.device
        self.mark()

    def tick(self, n: int = 1):
        for _ in range(n):
            self.eng.tick()

    def mark(self):
        self._calls, self._actions = len(self.drv.calls), len(actions(self.data))

    def untouched(self):
        """No executor call since the last mark (FakeDriver log and actions.jsonl)."""
        assert self.drv.calls[self._calls:] == [], self.drv.calls[self._calls:]
        new = [a for a in actions(self.data)[self._actions:] if a["action"] in DRIVER_ACTIONS]
        assert new == [], new

    def cloud_status(self, dep: str) -> dict:
        return (self.lab.srv.state.s["status"].get(self.dev) or {}).get(dep) or {}

    def cloud_events(self) -> list[dict]:
        return [e for e in self.lab.srv.state.s.get("events", []) if e["deviceId"] == self.dev]


class Lab:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.srv = start_cloud(tmp / "cloud")
        self.proxy = CaptureProxy(self.srv.port).start()
        publish(self.srv, "sec_base", "1.0.0")
        self.n = 0

    def engine(self, dev: str = "sec-dev", *, onboard_token: bool = True, via_proxy: bool = True, **kw) -> Dev:
        self.n += 1
        d = self.tmp / f"dev{self.n}"
        d.mkdir()
        safe = d / "safe.json"
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        if onboard_token:
            kw.setdefault("onboardingToken", token(self.srv, dev))
        cfg = engine_cfg(d / "data", dev, self.proxy.url if via_proxy else self.srv.url, safeStateFile=str(safe), **kw)
        drv = VersionFakeDriver("native")
        return Dev(self, Engine(cfg, {"native": drv}), drv, d / "data")

    def baseline(self, dev: str = "sec-dev") -> Dev:
        """An onboarded engine running `base` (manifestVersion 1): the previous state every refusal must keep."""
        d = self.engine(dev)
        d.tick()                                      # onboarding, capabilities, empty desired state
        deploy(self.srv, dev, [BASE])
        d.tick(2)
        assert d.cloud_status("base").get("state") == "Installed", d.cloud_status("base")
        assert d.eng.state["manifestVersion"] == 1 and set(d.drv.running) == {"base"}
        d.mark()
        return d

    def stop(self):
        self.proxy.stop()
        self.srv.stop()


@pytest.fixture
def lab(tmp_path):
    lb = Lab(tmp_path)
    yield lb
    lb.stop()


# ---- release verification (end to end: plan -> pull -> prepare refuses -> previous state kept) ------------------

def _release_refused(lab: Lab, case: str, item: dict, inner: str | None = None, *, inject: bool = False,
                     before_pull=None) -> dict:
    d = lab.baseline()
    app = {"id": "app", "driver": "native", **item}
    if inject:   # a backend that bypassed its own checks: served as planned, but with the given safety class
        s, p = admin(lab.srv, "POST", "/api/v1/admin/plans", {"deviceId": d.dev, "deployments": [
            BASE, {k: v for k, v in app.items() if k != "safetyClass"}]})
        assert s == 201, p
        deps = lab.srv.state.s["plans"][p["planHash"]]["deployments"]
        deps[1]["x-flex-safetyClass"] = item["safetyClass"]
        inject_desired(lab.srv, d.dev, 2, deps)
    else:
        deploy(lab.srv, d.dev, [BASE, app])
    if before_pull:
        before_pull()
    d.tick(2)
    r = d.cloud_status("app")                     # what the cloud received
    assert r.get("state") == "Failed" and r["reasonCode"] == "FE-REC-002" and r["keptPrevious"] is True, r
    detail = r["reason"].removeprefix("artefact rejected: ")
    expect(case, code_of(detail, "detail"))
    if inner:
        m = re.fullmatch(r"release not signed by a trusted key \((.*)\)", detail)
        assert m and code_of(m[1], "detail") == inner, detail
    d.untouched()
    assert set(d.drv.running) == {"base"} and d.eng.state["rejected"]["deployment"] == "app"
    return r


def test_tampered_blob_refused(lab):
    case = next(c for c in json.loads((VECTORS / "release" / "cases.json").read_text())["cases"] if c["name"] == "tampered")
    def tamper():   # after planning (the fake reads the descriptor at plan time): the registry serves other bytes
        for dig, rel in case["serve"].items():
            lab.srv.state.layout.blob_path(dig).write_bytes((VECTORS / "release" / rel).read_bytes())
    _release_refused(lab, "tampered-blob", {"module": "vec_native", "version": "1.0.0"}, before_pull=tamper)


def test_unsigned_release_refused(lab):
    _release_refused(lab, "unsigned-release", {"module": "vec_unsigned", "version": "1.0.0"})


def test_unknown_release_key_refused(lab):
    _release_refused(lab, "unknown-release-key", {"module": "vec_rogue", "version": "1.0.0"}, inner="FE-ART-SIG-004")


def test_bad_release_signature_refused(lab):
    _release_refused(lab, "bad-release-signature", {"module": "vec_badsig", "version": "1.0.0"}, inner="FE-ART-SIG-005")


def test_single_approver_on_safety_release_refused(lab):
    r = _release_refused(lab, "single-approver-safety-release", {"module": "vec_safety", "version": "1.0.1"})
    assert r["version"] == "1.0.1"


def test_safety_class_downgrade_refused_by_backend_and_device(lab):
    # backend: a plan may not declare another class than the release's (S4)
    s, b = admin(lab.srv, "POST", "/api/v1/admin/plans", {"deviceId": "x", "allowUnknownDevice": True, "deployments": [
        {"id": "app", "module": "vec_safety", "version": "1.0.0", "driver": "native", "safetyClass": "none"}]})
    assert s == 422 and "safety class" in b["title"], b
    # device: a desired state that gates a safety-relevant release as class none (interlock skipped) is refused
    _release_refused(lab, "safety-class-downgrade",
                     {"module": "vec_safety", "version": "1.0.0", "safetyClass": "none"}, inject=True)


def test_safety_class_upgrade_of_unapproved_release_refused(lab):
    _release_refused(lab, "safety-class-upgrade-unapproved",
                     {"module": "vec_native", "version": "1.0.0", "safetyClass": "safety-relevant"}, inject=True)


# ---- desired state (pull -> accept_manifest refuses -> event to the cloud, previous state kept) ------------------

def _refused(d: Dev, case: str, n_events: int = 1):
    evs = d.cloud_events()
    assert len(evs) == n_events, evs
    ev = {k: v for k, v in evs[-1].items() if k not in ("deviceId", "receivedAt")}
    contracts.validate("device-event", ev)
    assert ev["event"] == "DesiredStateRefused" and ev["acceptedManifestVersion"] == 1
    assert code_of(ev["reason"], "event") == ev["reasonCode"]
    expect(case, ev["reasonCode"])
    assert d.eng.state["lastRefusal"]["reasonCode"] == ev["reasonCode"] and d.eng.state["manifestVersion"] == 1
    audit = [a for a in d.lab.srv.state.s["audit"] if a["action"] == "device.event"]
    assert audit[-1]["detail"]["reasonCode"] == ev["reasonCode"]          # DA-8: in the audit chain
    d.untouched()
    assert set(d.drv.running) == {"base"}


def _mitm(lab: Lab, dev: str, fn):
    """MITM on the desired-state answer: fn(body, headers) -> (body, headers)."""
    def mutate(ex):
        if ex.method == "GET" and ex.path == f"/api/v1/devices/{dev}/deployments" and ex.status == 200:
            body, hdrs = fn(ex.resp_body, dict(ex.resp_headers))
            return 200, [(k, v) for k, v in hdrs.items() if v is not None], body
        return None
    lab.proxy.mutate = mutate


def _new_version(lab: Lab, d: Dev):
    deploy(lab.srv, d.dev, [{**BASE, "parameters": {"mode": "changed"}}])   # manifestVersion 2 is served
    d.tick(2)


def test_desired_tampered_body_refused(lab):
    d = lab.baseline()

    def fn(body, h):
        doc = json.loads(body)
        doc["deployments"][0]["spec"]["parameters"] = {"mode": "evil"}
        return canonical_json(doc), h                                       # cloud signature of the original
    _mitm(lab, d.dev, fn)
    _new_version(lab, d)
    _refused(d, "desired-tampered-body")


def test_desired_rogue_signature_refused(lab):
    d = lab.baseline()
    _mitm(lab, d.dev, lambda body, h: (body, {**h, "x-flex-manifest-signature": crypto.sign(vkey("rogue"), body)}))
    _new_version(lab, d)
    _refused(d, "desired-rogue-signature")


def test_desired_missing_signature_refused(lab):
    d = lab.baseline()
    _mitm(lab, d.dev, lambda body, h: (body, {**h, "x-flex-manifest-signature": None}))
    _new_version(lab, d)
    _refused(d, "desired-missing-signature")


@pytest.mark.parametrize("name", ["tampered", "wrong-key", "missing"])
def test_desired_signature_vectors_refused(name, tmp_path, vectors_manifest):
    """CP-4 manifest vectors through the engine (offline), then a tick: nothing reaches the driver."""
    mc = vectors_manifest
    c = next(x for x in mc["cases"] if x["name"] == name)
    drv = VersionFakeDriver()
    eng = Engine(engine_cfg(tmp_path / "d", "vec-dev", "http://127.0.0.1:9", httpTimeout=0.3), {"native": drv})
    assert eng.accept_manifest((VECTORS / "manifest" / c["body"]).read_bytes(), c["signature"], mc["etag"]) is False
    eng.tick()
    assert eng.state["desired"] is None and drv.calls == [] and not actions(tmp_path / "d")
    assert eng.state["lastRefusal"]["reasonCode"] == c["reasonCode"]
    expect(f"desired-vector-{name}", eng.state["outbox"][0]["reasonCode"])


@pytest.fixture(scope="module")
def vectors_manifest():
    return json.loads((VECTORS / "manifest" / "cases.json").read_text())


def test_desired_not_json_refused(lab):
    """Even a body signed with the real cloud key (compromised/buggy backend) must parse."""
    d = lab.baseline()
    bad = b"{not json"
    _mitm(lab, d.dev, lambda body, h: (bad, {**h, "x-flex-manifest-signature": crypto.sign(vkey("cloud"), bad)}))
    _new_version(lab, d)
    _refused(d, "desired-not-json")


def test_desired_schema_invalid_refused(lab):
    d = lab.baseline()
    deps = json.loads(json.dumps(lab.srv.state.s["desired"][d.dev]["deployments"]))
    deps[0]["x-flex-driver"] = "teleporter"
    del deps[0]["spec"]["applicationId"]
    inject_desired(lab.srv, d.dev, 2, deps)                                 # bypasses the fake's CP-1 plan check
    d.tick(3)                                                               # served on every pull: reported once
    _refused(d, "desired-schema-invalid")
    assert d.eng.state["lastRefusal"]["count"] >= 2


def test_desired_for_another_device_refused(lab):
    d = lab.baseline()
    other = lab.srv.state.desired_doc(d.dev)
    other["deviceId"], other["manifestVersion"] = "sec-other", 7
    for x in other["deployments"]:
        x["metadata"]["deviceId"] = "sec-other"
    body = canonical_json(other)                                            # validly signed, for someone else
    _mitm(lab, d.dev, lambda b, h: (body, {**h, "x-flex-manifest-signature": crypto.sign(vkey("cloud"), body)}))
    _new_version(lab, d)
    _refused(d, "desired-other-device")


def test_desired_downgrade_refused(lab):
    d = lab.baseline()
    inject_desired(lab.srv, d.dev, 0, [])                                   # replayed / rolled-back document
    d.tick(2)
    _refused(d, "desired-downgrade")
    assert d.eng.state["downgradesRefused"] >= 1


def test_desired_same_version_other_body_refused(lab):
    d = lab.baseline()
    inject_desired(lab.srv, d.dev, 1, [])                                   # same manifestVersion, other content
    d.tick(2)
    _refused(d, "desired-same-version-other-body")


# ---- cloud-side request checks (reference fake) -----------------------------------------------------------------

def _rejected(case: str, exc: Rejected, status: int, title: str):
    assert exc.status == status, exc
    m = re.fullmatch(r"(\d{3}): (.*)", str(exc), flags=re.S)
    assert json.loads(m[2])["title"] == title, str(exc)
    expect(case, code_of(str(exc), "detail"))


def test_replayed_nonce_refused(lab, monkeypatch):
    d = lab.engine(via_proxy=False)
    d.tick()
    assert d.eng.state["onboarded"]
    fixed = SimpleNamespace(urandom=lambda n: b"\x07" * n)
    monkeypatch.setattr(crypto, "os", fixed)                                # every request now reuses one nonce
    path = f"/api/v1/devices/{d.dev}/deployments"
    assert d.eng.client.request("GET", path)[0] == 200
    with pytest.raises(Rejected) as ei:
        d.eng.client.request("GET", path)
    _rejected("replayed-nonce", ei.value, 401, "Replayed request")
    d.untouched()


def test_replayed_request_raw_refused(lab):
    """An attacker on the link replays a captured, validly signed request byte for byte."""
    import http.client

    d = lab.engine()
    d.tick()
    ex = next(x for x in lab.proxy.exchanges if x.method == "GET" and x.path.endswith("/deployments") and x.status == 200)
    c = http.client.HTTPConnection("127.0.0.1", lab.srv.port, timeout=5)
    c.request("GET", ex.path, headers={k: v for k, v in ex.req_headers.items() if k not in ("host", "connection")})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    _rejected("replayed-request-raw", Rejected(r.status, body), 401, "Replayed request")


@pytest.mark.parametrize("case,skew", [("stale-date", -301), ("future-date", 301)])
def test_request_time_outside_window_refused(lab, monkeypatch, case, skew):
    d = lab.engine(via_proxy=False)
    d.tick()
    real = time.time
    monkeypatch.setattr(client_mod, "time", SimpleNamespace(time=lambda: real() + skew))
    with pytest.raises(Rejected) as ei:
        d.eng.client.request("GET", f"/api/v1/devices/{d.dev}/deployments")
    _rejected(case, ei.value, 401, "Request time outside the allowed window")
    d.untouched()


def test_unsigned_request_refused(lab):
    d = lab.engine(via_proxy=False)
    d.tick()
    with pytest.raises(Rejected) as ei:
        d.eng.client.request("GET", f"/api/v1/devices/{d.dev}/deployments", signed=False)
    _rejected("unsigned-request", ei.value, 401, "Unsigned request")


def test_bad_request_signature_refused(lab):
    d = lab.engine(via_proxy=False)
    d.tick()
    d.eng.client.key = vkey("rogue")                                        # not the key registered at onboarding
    with pytest.raises(Rejected) as ei:
        d.eng.client.request("GET", f"/api/v1/devices/{d.dev}/deployments")
    _rejected("bad-request-signature", ei.value, 401, "Bad request signature")


def test_unknown_device_refused(lab):
    d = lab.engine("sec-never-onboarded", onboard_token=False, via_proxy=False)
    with pytest.raises(Rejected) as ei:
        d.eng.client.request("GET", "/api/v1/devices/sec-never-onboarded/deployments")
    _rejected("unknown-device", ei.value, 401, "Unknown device")


def test_other_devices_desired_state_and_resources_refused(lab):
    a = lab.engine("sec-a", via_proxy=False)
    b = lab.engine("sec-b", via_proxy=False)
    a.tick()
    b.tick()
    caps_b = json.dumps(lab.srv.state.s["devices"]["sec-b"]["capabilities"], sort_keys=True)
    seq = a.eng.state["seq"] + 100
    calls = [("GET", "/api/v1/devices/sec-b/deployments", None),
             ("PUT", "/api/v1/devices/sec-b/capabilities", a.eng.capabilities()),
             ("POST", "/api/v1/devices/sec-b/deployments/x/status",
              {"dep": "x", "seq": seq, "state": "Installed", "deviceTime": time.time(), "manifestVersion": 0}),
             ("POST", "/api/v1/devices/sec-b/events",
              {"kind": "DeviceEvent", "event": "DesiredStateRefused", "seq": seq + 1, "deviceTime": time.time(),
               "reason": "desired state for another device ignored", "documentDigest": "sha256:" + "0" * 64,
               "acceptedManifestVersion": 0}),
             ("GET", "/api/v1/devices/sec-b/bundle", None)]
    for method, path, body in calls:
        with pytest.raises(Rejected) as ei:
            if body is None:
                a.eng.client.request(method, path)
            else:
                a.eng.client.post_json(path, body, method=method)
        _rejected("other-device-resources", ei.value, 403, "Device may only access its own resources")
    assert json.dumps(lab.srv.state.s["devices"]["sec-b"]["capabilities"], sort_keys=True) == caps_b
    assert "sec-b" not in lab.srv.state.s["status"] and not lab.srv.state.s.get("events")
    a.untouched()
    b.untouched()


def test_self_approval_refused_by_backend(lab):
    """Two-person rule at the backend: the requester cannot approve, and an unapproved safety plan cannot apply."""
    s, p = admin(lab.srv, "POST", "/api/v1/admin/plans", {"deviceId": "x", "allowUnknownDevice": True, "deployments": [
        {"id": "app", "module": "vec_safety", "version": "1.0.0", "driver": "native"}]}, user="carol")
    assert s == 201 and p["requiresApproval"], p
    s, b = admin(lab.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, user="carol")
    _rejected("self-approval", Rejected(s, json.dumps(b)), 403, "Approver must differ from requester (two-person rule)")
    s, b = admin(lab.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 403 and b["title"] == "Plan needs approval by a second person", b
    assert "x" not in lab.srv.state.s["desired"]


# ---- identity, transport, secrets ---------------------------------------------------------------------------------

def test_plain_http_refused_without_lab_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEX_ALLOW_PLAIN_HTTP", raising=False)
    drv = VersionFakeDriver()
    with pytest.raises(TlsConfigError, match="plain HTTP refused") as ei:
        Engine(engine_cfg(tmp_path / "d", "sec-dev", "http://127.0.0.1:9"), {"native": drv})
    expect("plain-http-without-lab-flag", type(ei.value).__name__)
    Engine(engine_cfg(tmp_path / "d2", "sec-dev", "http://127.0.0.1:9", allowPlainHttp=True), {"native": drv})
    assert drv.calls == []


def test_https_without_pinned_ca_refused(tmp_path):
    with pytest.raises(TlsConfigError, match="pinned CA") as ei:
        Engine(engine_cfg(tmp_path / "d", "sec-dev", "https://cloud.lab:8443"), {"native": VersionFakeDriver()})
    expect("https-without-ca", type(ei.value).__name__)


@pytest.mark.parametrize("case,leaf,pattern", [("tls-wrong-ca", "rogue", "verif"),
                                               ("tls-wrong-host", "wronghost", "(?i)hostname|match"),
                                               ("tls-expired", "expired", "(?i)expired")])
def test_tls_failures_refused_by_the_engine(tmp_path, pki, tls_server, case, leaf, pattern):
    port = tls_server(leaf)
    drv = VersionFakeDriver()
    eng = Engine(engine_cfg(tmp_path / "d", "sec-dev", f"https://127.0.0.1:{port}", caFile=str(pki.ca),
                            onboardingToken="t", httpTimeout=3), {"native": drv})
    eng.tick()                                                  # Offline (TlsError): retried later, nothing happens
    assert not eng.state["onboarded"] and eng.state["desired"] is None and drv.calls == []
    with pytest.raises(TlsError, match=pattern) as ei:
        eng.onboard()
    expect(case, type(ei.value).__name__)


@pytest.mark.parametrize("case", ["key-file-permissions", "key-dir-permissions"])
def test_device_key_with_unsafe_permissions_refused(tmp_path, case):
    data = tmp_path / "d"
    data.mkdir(mode=0o700)
    crypto.load_or_create(data / "device.key")
    target, mode = (data / "device.key", 0o644) if case == "key-file-permissions" else (data, 0o777)
    os.chmod(target, mode)
    drv = VersionFakeDriver()
    with pytest.raises(KeyPermissionError) as ei:
        Engine(engine_cfg(data, "sec-dev", "http://127.0.0.1:9"), {"native": drv})
    assert stat.S_IMODE(os.stat(target).st_mode) == mode       # never silently "fixed"
    expect(case, type(ei.value).__name__)
    assert drv.calls == []


def _token_file(dirpath: Path, value: str, mode: int = 0o600) -> Path:
    dirpath.mkdir(exist_ok=True)
    p = dirpath / "onboarding.token"
    p.write_text(value + "\n")
    os.chmod(p, mode)
    return p


def test_token_file_with_unsafe_permissions_refused(lab, tmp_path):
    p = _token_file(tmp_path / "tok", token(lab.srv, "sec-dev"), 0o644)
    d = lab.engine(onboard_token=False, onboardingTokenFile=str(p))
    d.tick()
    assert not d.eng.state["onboarded"] and d.eng.metrics()["lastError"]["type"] == "SecretPermissionError"
    with pytest.raises(SecretPermissionError) as ei:
        d.eng.onboard()
    expect("token-file-permissions", type(ei.value).__name__)
    assert p.exists() and "sec-dev" not in lab.srv.state.s["devices"]
    d.untouched()


def test_onboarding_token_reuse_refused(lab, tmp_path):
    value = token(lab.srv, "sec-dev")
    p = _token_file(tmp_path / "tok", value)
    first = lab.engine(onboard_token=False, onboardingTokenFile=str(p))
    first.tick()
    assert first.eng.state["onboarded"] and not p.exists()                   # consumed: overwritten + deleted
    # the same token file again (a second engine / a restore of an old data dir)
    second = lab.engine(onboard_token=False, onboardingTokenFile=str(p))
    second.tick()
    assert not second.eng.state["onboarded"] and second.eng.metrics()["lastError"]["type"] == "TokenConsumedError"
    with pytest.raises(TokenConsumedError) as ei:
        second.eng.onboard()
    expect("token-file-reuse", type(ei.value).__name__)
    # the token value replayed to the cloud
    third = lab.engine(onboard_token=False, onboardingToken=value)
    with pytest.raises(Rejected) as ei:
        third.eng.onboard()
    _rejected("token-value-reuse", ei.value, 403, "Invalid or used onboarding token")
    # a fresh token issued for another device
    fourth = lab.engine("sec-imposter", onboard_token=False, onboardingToken=token(lab.srv, "sec-dev"))
    with pytest.raises(Rejected) as ei:
        fourth.eng.onboard()
    _rejected("token-other-device", ei.value, 403, "Invalid or used onboarding token")
    assert "sec-imposter" not in lab.srv.state.s["devices"]
    for x in (second, third, fourth):
        assert not x.eng.state["onboarded"]
        x.untouched()


# ---- offline bundle -----------------------------------------------------------------------------------------------

def _offline(tmp_path: Path, dev: str = "vec-dev") -> Dev:
    drv = VersionFakeDriver()
    eng = Engine(engine_cfg(tmp_path / "d", dev, "http://127.0.0.1:9", httpTimeout=0.3), {"native": drv})
    return Dev(None, eng, drv, tmp_path / "d")


def _tar(members: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _vector_members() -> list[tuple[str, bytes]]:
    with tarfile.open(VECTORS / "bundle" / "bundle.tar") as tf:
        return [(m.name, tf.extractfile(m).read()) for m in tf.getmembers() if m.isfile()]


def _import(d: Dev, data: bytes, tmp: Path):
    p = tmp / "bundle.tar"
    p.write_bytes(data)
    d.eng.import_bundle(str(p))


def _after_bundle(d: Dev):
    d.tick()
    assert d.eng.state["desired"] is None and d.eng.state["manifestVersion"] == 0
    d.untouched()
    assert d.drv.calls == []


def test_bundle_positive_control(tmp_path):
    d = _offline(tmp_path)
    _import(d, _tar(_vector_members()), tmp_path)
    assert d.eng.state["manifestVersion"] == 3


def test_bundle_tampered_blob_refused(tmp_path):
    d = _offline(tmp_path)
    with pytest.raises(VerificationError) as ei:
        d.eng.import_bundle(str(VECTORS / "bundle" / "bundle-tampered.tar"))
    expect("bundle-tampered-blob", code_of(str(ei.value), "detail"))
    _after_bundle(d)


def test_bundle_garbage_refused(tmp_path):
    d = _offline(tmp_path)
    with pytest.raises(tarfile.ReadError) as ei:
        _import(d, os.urandom(4096), tmp_path)
    expect("bundle-garbage", type(ei.value).__name__)
    _after_bundle(d)


def test_bundle_without_desired_state_refused(tmp_path):
    d = _offline(tmp_path)
    with pytest.raises(VerificationError) as ei:
        _import(d, _tar([m for m in _vector_members() if m[0] != "desired.json"]), tmp_path)
    expect("bundle-no-desired-state", code_of(str(ei.value), "log"))
    _after_bundle(d)


def test_bundle_traversal_member_refused(tmp_path):
    d = _offline(tmp_path)
    evil = [("blobs/sha256/../../../../evil", b"#!/bin/sh\necho owned\n")]
    with pytest.raises(VerificationError) as ei:
        _import(d, _tar(_vector_members()[:2] + evil), tmp_path)
    expect("bundle-traversal-member", code_of(str(ei.value), "detail"))
    assert not [p for p in tmp_path.rglob("evil")]
    _after_bundle(d)


def test_bundle_oversized_member_refused(tmp_path):
    """A large member that does not hash to its name is refused before anything is accepted. (There is no size cap
    on bundles or members in alpha: the whole tar is read into memory, bundle.v1alpha1 limits, EV-26.)"""
    d = _offline(tmp_path)
    big = ("blobs/sha256/" + "ab" * 32, b"\0" * (24 << 20))
    with pytest.raises(VerificationError) as ei:
        _import(d, _tar(_vector_members()[:2] + [big] + _vector_members()[2:]), tmp_path)
    expect("bundle-oversized-member", code_of(str(ei.value), "detail"))
    assert not (tmp_path / "d" / "cas" / "sha256" / ("ab" * 32)).exists()
    _after_bundle(d)


def test_bundle_bad_signature_refused(tmp_path):
    d = _offline(tmp_path)
    members = _vector_members()
    desired = dict(members)["desired.json"]
    members = [(n, crypto.sign(vkey("rogue"), desired).encode() if n == "desired.sig" else b) for n, b in members]
    _import(d, _tar(members), tmp_path)
    expect("bundle-bad-signature", d.eng.state["lastRefusal"]["reasonCode"])
    _after_bundle(d)


def test_bundle_for_another_device_refused(tmp_path):
    d = _offline(tmp_path, dev="sec-dev")                    # the vector bundle is for vec-dev
    d.eng.import_bundle(str(VECTORS / "bundle" / "bundle.tar"))
    ev = d.eng.state["outbox"][-1]
    assert ev["event"] == "DesiredStateRefused" and ev["reasonCode"] == d.eng.state["lastRefusal"]["reasonCode"]
    expect("bundle-other-device", ev["reasonCode"])
    _after_bundle(d)


@pytest.mark.xfail(strict=True, reason="bug (engine): ArtefactStore.import_bundle stores the blobs and rewrites the "
                                       "cached referrers index (cas/referrers/<release>) from any signature manifest "
                                       "in the tar BEFORE the bundle's desired state is authenticated; the cached "
                                       "index is then trusted forever (referrers() never refetches), so one refused "
                                       "bundle carrying a forged signature manifest for a release makes every later "
                                       "online install of that release fail with FE-ART-SIG-002 (denial of service)")
def test_refused_bundle_does_not_poison_the_artefact_cache(lab, tmp_path):
    from flexengine2.artefacts.oci import AT_SIGNATURE, EMPTY_JSON, MT_EMPTY, MT_MANIFEST
    from flexengine2.util import sha256_digest

    d = lab.baseline()
    target = lab.srv.state.layout.resolve("flexnodes/vec_native:1.0.0")
    payload = canonical_json({"critical": {"identity": {"repository": "flexnodes/vec_native"},
                                           "image": {"manifest-digest": target["digest"]}}})
    env = canonical_json({"payload": crypto.b64(payload), "signature": crypto.sign(vkey("rogue"), payload),
                          "keyid": crypto.key_id(crypto.public_b64(vkey("rogue")))})
    sman = canonical_json({"schemaVersion": 2, "mediaType": MT_MANIFEST, "artifactType": AT_SIGNATURE,
                           "config": {"mediaType": MT_EMPTY, "digest": sha256_digest(EMPTY_JSON), "size": 2},
                           "layers": [{"mediaType": AT_SIGNATURE, "digest": sha256_digest(env), "size": len(env)}],
                           "subject": {"mediaType": MT_MANIFEST, "digest": target["digest"], "size": target["size"]}})
    junk = b'{"not": "a desired state"}'
    bundle = _tar([("desired.json", junk), ("desired.sig", crypto.sign(vkey("rogue"), junk).encode())]
                  + [(f"blobs/sha256/{sha256_digest(b)[7:]}", b) for b in (EMPTY_JSON, env, sman)])
    _import(d, bundle, tmp_path)
    assert d.eng.state["lastRefusal"]["reasonCode"] == "FE-DS-001"         # refused, as it must be
    deploy(lab.srv, d.dev, [BASE, {"id": "app", "module": "vec_native", "version": "1.0.0", "driver": "native"}])
    d.tick(2)
    r = d.cloud_status("app")
    assert r.get("state") == "Installed", r.get("reason")                   # fails: cached forged referrer wins


# ---- the index itself -----------------------------------------------------------------------------------------------

def test_index_codes_are_registered():
    codes = {e["code"] for e in contracts.reason_codes()["codes"]}
    for case, code in INDEX.items():
        assert code in codes or not code.startswith("FE-"), (case, code)


def test_zz_every_indexed_case_ran(request):
    """Last in the file: every INDEX entry was asserted by a case above (skipped when run with -k / a subset)."""
    names = {i.name for i in request.session.items if i.module is request.module}
    if len(names) < 40:
        pytest.skip("subset run")
    if request.session.testsfailed:
        pytest.skip("other cases failed")
    assert set(INDEX) == EXECUTED, sorted(set(INDEX) ^ EXECUTED)

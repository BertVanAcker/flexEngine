"""S4: safety class from the model (release tool + stub), backend-side approval verification, release
upload CLI, container runOptions allow-list. No Docker needed (native payloads only)."""

from __future__ import annotations

import http.client
import json
import time

import pytest

from .p2lab import MODEL, item
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.util import canonical_json, iso
from flexengine2.drivers.base import DriverError
from flexengine2.drivers.container import run_option_args
from flexengine2.artefacts.oci import Layout
from flexengine2.testing.publish import main as publish_main
from flexengine2.testing.release import SafetyClassMismatch, descriptor_from_module, make_release, model_safety, module_hashes


@pytest.fixture(scope="module")
def classes():
    return model_safety(MODEL)


def test_release_tool_derives_class_from_model(classes):
    h = module_hashes(MODEL)
    pg = descriptor_from_module(MODEL, "perception_guard", "9.0.0", profile="native", health={}, restart={},
                                hashes=h, classes=classes)
    dl = descriptor_from_module(MODEL, "detection_logger", "9.0.0", profile="native", health={}, restart={},
                                hashes=h, classes=classes)
    assert pg["safetyClass"] == "safety-relevant" and dl["safetyClass"] == "none"
    assert pg["safetyClassSource"].startswith("model")
    with pytest.raises(SafetyClassMismatch):  # the P2/P6 inconsistency: perception_guard declared "none"
        descriptor_from_module(MODEL, "perception_guard", "9.0.0", profile="native", health={}, restart={},
                               safety_class="none", hashes=h, classes=classes)


def _admin(srv, method, path, body=None, user="carol", raw: bytes | None = None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    c.request(method, path, body=data, headers={"Authorization": "Bearer t", "X-Flex-User": user,
                                                 "Content-Type": ctype, "Connection": "close"})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (json.loads(out) if out else None)


def _forged_release(layout: Layout, key, module: str, version: str, declared: str, classes) -> dict:
    """A release whose descriptor declares `declared` regardless of the model (what P2 used to build)."""
    from flexengine2.testing.release import build_native_payload, publish_release, sbom_stub, sign_release
    from flexengine2.artefacts.oci import MT_PAYLOAD_NATIVE
    desc = descriptor_from_module(MODEL, module, version, profile="native", health={}, restart={},
                                  hashes=module_hashes(MODEL), classes=classes)
    desc["safetyClass"] = declared
    m = publish_release(layout, f"flexnodes/{module}", desc,
                        [(MT_PAYLOAD_NATIVE, build_native_payload(module, version), {"dev.flex.profile": "native"})],
                        sbom_stub(module, version, "native"))
    sign_release(layout, key, f"flexnodes/{module}", m, version)
    return m


def _onboard(srv, dev: str):
    s, b = _admin(srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
    srv.state.s["devices"][dev] = {"publicKey": crypto.public_b64(crypto.generate()), "capabilities": None}


def test_stub_refuses_release_class_differing_from_model(tmp_path, classes):
    key = crypto.generate()
    layout = Layout(tmp_path / "cloud" / "oci")
    _forged_release(layout, key, "perception_guard", "1.0.0", "none", classes)
    make_release(layout, tmp_path / "w", key, model_dir=MODEL, module="perception_guard", version="1.1.0",
                 profile="native", hashes=module_hashes(MODEL), classes=classes)
    srv = CloudServer(tmp_path / "cloud", 0, "t", model_dirs=[MODEL]).start()
    try:
        _onboard(srv, "d1")
        s, b = _admin(srv, "POST", "/api/v1/admin/plans",
                      {"deviceId": "d1", "deployments": [item("perception_guard", "1.0.0", "native")]})
        assert s == 422 and "differs from the model" in b["title"], b
        s, b = _admin(srv, "POST", "/api/v1/admin/plans",
                      {"deviceId": "d1", "deployments": [item("perception_guard", "1.1.0", "native")]})
        assert s == 201 and b["requiresApproval"], b  # class from the model, not from the plan
        s, b = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "d1", "deployments": [
            item("perception_guard", "1.1.0", "native", safetyClass="none")]})
        assert s == 422, b
    finally:
        srv.stop()


def test_publish_cli_uploads_and_stub_checks_class(tmp_path, classes, capsys):
    srv = CloudServer(tmp_path / "cloud", 0, "t", model_dirs=[MODEL]).start()
    try:
        (tmp_path / "tok").write_text("t")
        app = tmp_path / "app"
        app.mkdir()
        (app / "app.py").write_text("print('hello lab')\n")
        rc = publish_main(["--cloud", srv.url, "--admin-token-file", str(tmp_path / "tok"), "--model", str(MODEL),
                           "--module", "detection_logger", "--version", "3.0.0", "--profile", "native",
                           "--native-dir", str(app), "--release-key", str(tmp_path / "release.key")])
        assert rc == 0
        assert srv.state.layout.resolve("flexnodes/detection_logger:3.0.0")
        out = capsys.readouterr().out
        assert "detection_logger:3.0.0 (none)" in out
        audit = srv.state.s["audit"]
        assert audit[-1]["action"] == "release.uploaded"
        # a forged layout with a wrong class is refused as a whole
        layout = Layout(tmp_path / "forged")
        _forged_release(layout, crypto.generate(), "gige_camera", "9.9.9", "none", classes)
        from flexengine2.testing.publish import layout_tar
        s, b = _admin(srv, "POST", "/api/v1/admin/releases", raw=layout_tar(layout.root), ctype="application/x-tar")
        assert s == 422 and "differs from the model" in b["title"], b
        assert not srv.state.layout.resolve("flexnodes/gige_camera:9.9.9")
    finally:
        srv.stop()


# ---- backend verifies approvals (P6 U1) -----------------------------------------------------------

POLICY_RULES = {"standard": {"count": 1, "roles": ["release-manager", "safety-engineer"]},
                "safety": {"count": 2, "roles": ["release-manager", "safety-engineer"],
                           "requireRoles": ["safety-engineer"]},
                "requesterMayApprove": False}


def _approval(key, h, approver, roles, ttl=600, decision="approve"):
    body = {"planHash": h, "approver": approver, "roles": sorted(roles), "decision": decision, "at": iso(),
            "expiresAtTs": time.time() + ttl, "comment": ""}
    return {**body, "sig": crypto.sign(key, canonical_json(body))}


def test_backend_verifies_signed_approvals(tmp_path, classes):
    console = crypto.generate()
    rogue = crypto.generate()
    key = crypto.generate()
    layout = Layout(tmp_path / "cloud" / "oci")
    for mod in ("detection_logger", "perception_guard"):
        make_release(layout, tmp_path / "w", key, model_dir=MODEL, module=mod, version="1.0.0", profile="native",
                     hashes=module_hashes(MODEL), classes=classes)
    pol = {"keys": [crypto.public_b64(console)], **POLICY_RULES}
    srv = CloudServer(tmp_path / "cloud", 0, "t", model_dirs=[MODEL], approval_policy=pol).start()
    try:
        _onboard(srv, "d1")
        actor = "agent:agent-dev|obo:alice"
        s, p = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "d1", "deployments": [
            item("detection_logger", "1.0.0", "native")]}, user=actor)
        h = p["planHash"]
        apply = f"/api/v1/admin/plans/{h}/apply"
        # the admin token alone is not enough any more
        assert _admin(srv, "POST", apply, {})[0] == 403
        # forged (wrong key), bound to another plan, expired, requester's own
        bad = [_approval(rogue, h, "bob", ["release-manager"]),
               _approval(console, "sha256:" + "0" * 64, "bob", ["release-manager"]),
               _approval(console, h, "bob", ["release-manager"], ttl=-1),
               _approval(console, h, "alice", ["release-manager"]),
               _approval(console, h, "olga", ["operator"])]
        for a in bad:
            s, b = _admin(srv, "POST", apply, {"approvals": [a]})
            assert s == 403, (a["approver"], b)
        tampered = _approval(console, h, "bob", ["release-manager"])
        tampered["approver"] = "mallory"
        assert _admin(srv, "POST", apply, {"approvals": [tampered]})[0] == 403
        s, b = _admin(srv, "POST", apply, {"approvals": [_approval(console, h, "bob", ["release-manager"])]})
        assert s == 200, b
        ev = srv.state.s["audit"][-1]["detail"]["verifiedApprovals"]
        assert ev["approvers"] == ["bob"] and ev["rule"] == "standard"

        # safety plan: two distinct humans incl. a safety engineer
        s, p = _admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": "d1", "deployments": [
            item("detection_logger", "1.0.0", "native"), item("perception_guard", "1.0.0", "native")]}, user=actor)
        h2 = p["planHash"]
        apply2 = f"/api/v1/admin/plans/{h2}/apply"
        one = [_approval(console, h2, "bob", ["release-manager"])]
        assert _admin(srv, "POST", apply2, {"approvals": one})[0] == 403
        no_se = one + [_approval(console, h2, "carol", ["release-manager"])]
        assert _admin(srv, "POST", apply2, {"approvals": no_se})[0] == 403
        assert _admin(srv, "POST", apply2, {"preauthorized": "rollback-known-good"})[0] == 403
        ok = one + [_approval(console, h2, "erin", ["safety-engineer"])]
        s, b = _admin(srv, "POST", apply2, {"approvals": ok})
        assert s == 200, b
    finally:
        srv.stop()


def test_preauthorized_rollback_needs_known_good(tmp_path, classes):
    console, key = crypto.generate(), crypto.generate()
    layout = Layout(tmp_path / "cloud" / "oci")
    for v in ("1.0.0", "1.1.0"):
        make_release(layout, tmp_path / "w", key, model_dir=MODEL, module="detection_logger", version=v,
                     profile="native", hashes=module_hashes(MODEL), classes=classes)
    srv = CloudServer(tmp_path / "cloud", 0, "t", model_dirs=[MODEL],
                      approval_policy={"keys": [crypto.public_b64(console)], **POLICY_RULES}).start()
    try:
        _onboard(srv, "d1")
        for v in ("1.0.0", "1.1.0"):
            s, p = _admin(srv, "POST", "/api/v1/admin/plans",
                          {"deviceId": "d1", "deployments": [item("detection_logger", v, "native")]})
            s, b = _admin(srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply",
                          {"approvals": [_approval(console, p["planHash"], "bob", ["release-manager"])]})
            assert s == 200, b
        s, p = _admin(srv, "POST", "/api/v1/admin/plans",
                      {"deviceId": "d1", "deployments": [item("detection_logger", "1.0.0", "native")]})
        roll = f"/api/v1/admin/plans/{p['planHash']}/apply"
        assert _admin(srv, "POST", roll, {"preauthorized": "rollback-known-good"})[0] == 403  # never reported
        srv.state.s["statusLog"].append({"deviceId": "d1", "deploymentId": "detection-logger", "state": "Installed",
                                         "version": "1.0.0", "health": "healthy"})
        assert _admin(srv, "POST", roll, {"preauthorized": "rollback-known-good"})[0] == 200
    finally:
        srv.stop()


# ---- container runOptions allow-list --------------------------------------------------------------

def test_run_options_allow_list():
    allowed = {"devices": ["/dev/dri", "nvidia.com/gpu=all"], "gpus": ["all"]}
    assert run_option_args({"devices": ["nvidia.com/gpu=all"], "gpus": "all"}, allowed) == [
        "--device", "nvidia.com/gpu=all", "--gpus", "all"]
    assert run_option_args({}, {}) == []
    for bad in ({"devices": ["/dev/mem"]}, {"privileged": True}, {"gpus": "all"} | {"devices": [1]}):
        with pytest.raises(DriverError):
            run_option_args(bad, allowed)
    with pytest.raises(DriverError):  # nothing is allowed unless the device config says so
        run_option_args({"devices": ["/dev/dri"]}, {})


def test_run_options_are_in_the_signed_descriptor(classes):
    d = descriptor_from_module(MODEL, "perception_guard", "9.0.0", profile="container", health={}, restart={},
                               hashes=module_hashes(MODEL), classes=classes,
                               run_options={"devices": ["nvidia.com/gpu=all"]})
    assert d["runOptions"] == {"devices": ["nvidia.com/gpu=all"]}

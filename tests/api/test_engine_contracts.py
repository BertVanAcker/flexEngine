"""The engine and the reference fake of its other side (testing/fakecloud.py) against CP-1/CP-2/CP-3/CP-4.

* Engine.capabilities() for every role and in-tree driver fragment validates (CP-2).
* Every status record a real engine run puts into its outbox validates and its reason has a code (CP-2).
* Engine <-> fakecloud over HTTP: every request the engine sends and every response the fake returns validates;
  the desired state the fake serves for a plan validates (CP-1), including a child device and MCU parameters;
  the fake's offline bundle follows bundle.v1alpha1 (CP-3).
* Recorded desired states / status / release descriptors from earlier runs validate (when present).
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import shutil

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flexengine2 import __version__, contracts
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import DriverError
from flexengine2.drivers.process import ProcessDriver
from flexengine2.drivers.plc.verify import PlcVerifyDriver
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.util import canonical_json

from _api_helpers import VECTORS, FakeDriver, bundle_layout_problems, recorded


def vkey(vectors, role) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(vectors["keys"][role]["private"]))


# ---- CP-2 capabilities --------------------------------------------------------------------------------------------

MCU_INFO = {"arch": "armv7m", "float_abi": "soft", "abi_major": 1, "abi_minor": 0, "caps": 3, "sockets": 2,
            "slot_size": 131072, "llext_heap": 65536, "image": "1.0.0+0", "confirmed": True, "board": "mps2/an385"}


class FakeLink:
    def __init__(self, info):
        self.info = info

    def flexmod(self, cmd, **kw):
        assert cmd == "info"
        return dict(self.info)


class FakeLinks:
    """Stands in for drivers.mcu.driver.LinkPool (one SMP link per MCU)."""

    def __init__(self, info: dict | None):
        self.info = info

    def call(self, fn, retries: int = 1):
        if self.info is None:
            raise DriverError("MCU unreachable: [Errno 111] Connection refused")
        return fn(FakeLink(self.info))


def mcu_engine(engine_cfg, info):
    from flexengine2.drivers.mcu import flxm
    from flexengine2.drivers.mcu.driver import McuFirmwareDriver, McuModuleDriver
    from cryptography.hazmat.primitives.asymmetric import ec

    links = FakeLinks(info)
    cfg = engine_cfg(deviceId="panda.portenta")
    pub = flxm.raw_public(ec.generate_private_key(ec.SECP256R1()))
    mods = McuModuleDriver(links, cfg["dataDir"], pub, "panda.portenta")
    drivers = {"mcu-module": mods, "mcu-firmware": McuFirmwareDriver(links, cfg["dataDir"], "panda.portenta", mods)}
    return Engine(cfg, drivers, role="child-device", parent="panda", mcu=info or {})


def test_capabilities_standalone_fake_and_native(engine_cfg, tmp_path):
    cfg = engine_cfg()
    eng = Engine(cfg, {"native": ProcessDriver(cfg["deviceId"], tmp_path / "data")})
    doc = eng.capabilities()
    contracts.validate("capabilities", doc)
    assert doc["x-flex"]["engine"] == {"impl": "flexengine2", "version": __version__, "spi": "1.0"}
    contracts.validate("capabilities", Engine(engine_cfg(), {"native": FakeDriver("native")}).capabilities())
    contracts.validate("capabilities", Engine(engine_cfg(), {}).capabilities())


def test_capabilities_gateway_with_children_plc_and_applab(engine_cfg, tmp_path):
    from flexengine2.drivers.applab.driver import ApplabDriver

    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": False}))
    cfg = engine_cfg(deviceId="panda", safeStateFile=str(safe))
    drivers = {"plc-verify": PlcVerifyDriver("panda", tmp_path / "d", {"plcs": {
                   "panda/cx": {"via": "ads", "amsNetId": "5.1.2.3.1.1", "ip": "10.0.0.5"},
                   "opta": {"via": "modbus", "host": "10.0.0.6"}}}),
               "applab": ApplabDriver("panda", tmp_path / "d", {"cli": "false"})}
    kids = [{"deviceId": "panda.portenta", "mode": "see-thru", "link": "smp/uart", "kind": "mcu"}]
    doc = Engine(cfg, drivers, role="gateway", children=kids).capabilities()
    contracts.validate("capabilities", doc)
    assert doc["x-flex"]["safeStateSignals"] == ["machine_parked", "operator_confirmed"]
    assert doc["x-flex"]["drivers"]["applab"]["exclusive"] is True


@pytest.mark.docker
def test_capabilities_container_docker(engine_cfg):
    from flexengine2.drivers.container import CliContainerDriver

    contracts.validate("capabilities", Engine(engine_cfg(), {"container": CliContainerDriver("vec-dev")}).capabilities())


def test_capabilities_child_device_mcu(engine_cfg):
    doc = mcu_engine(engine_cfg, MCU_INFO).capabilities()
    contracts.validate("capabilities", doc)
    assert doc["x-flex"]["parent"] == {"deviceId": "panda", "mode": "see-thru"}


def test_capabilities_child_device_mcu_unreachable(engine_cfg):
    doc = mcu_engine(engine_cfg, None).capabilities()
    contracts.validate("capabilities", doc)
    frag = doc["x-flex"]["drivers"]["mcu-module"]
    assert frag["reachable"] is False and "moduleAbi" not in frag and "arch" not in frag, frag
    assert doc["x-flex"]["drivers"]["mcu-firmware"]["reachable"] is False


def test_capabilities_missing_cli_never_raises(engine_cfg, tmp_path, monkeypatch):
    """A missing executor CLI is reported as `available: false`, never a FileNotFoundError (CP-2/CP-5)."""
    from flexengine2.drivers.applab.driver import ApplabDriver
    from flexengine2.drivers.container import AnkaiosDriver, CliContainerDriver

    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    drivers = {"container": CliContainerDriver("vec-dev", "docker"),
               "applab": ApplabDriver("vec-dev", tmp_path / "d", {"cli": str(tmp_path / "no-such-cli")})}
    doc = Engine(engine_cfg(), drivers).capabilities()
    contracts.validate("capabilities", doc)
    for p in drivers:
        assert doc["x-flex"]["drivers"][p]["available"] is False, doc["x-flex"]["drivers"][p]
    doc = Engine(engine_cfg(), {"container": AnkaiosDriver("vec-dev", "ank", "http://127.0.0.1:1", "a")}).capabilities()
    contracts.validate("capabilities", doc)
    assert doc["x-flex"]["drivers"]["container"]["available"] is False


# ---- CP-2 status: a real engine run, offline from the vector bundle ---------------------------------------------

def _outbox_checked(eng) -> list[dict]:
    recs = list(eng.state["outbox"])
    for r in recs:
        contracts.validate("status", r)
        for field in ("reason", "failure"):
            if r.get(field):
                assert contracts.reason_code(r[field], field), (field, r[field])
    return recs


def test_engine_run_status_records_validate(engine_cfg, vectors, tmp_path):
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"machine_parked": False, "operator_confirmed": False}))
    drv = FakeDriver("native")
    eng = Engine(engine_cfg(cloudPublicKey=vectors["manifest"]["cloudPublicKey"], onboardingToken="t",
                            releasePublicKeys=vectors["release"]["trustedReleaseKeys"], safeStateFile=str(safe)),
                 {"native": drv})
    eng.import_bundle(str(VECTORS / "bundle" / "bundle.tar"))
    eng.tick()                                  # cloud unreachable (Offline); reconcile from the bundle
    states = {(r["dep"], r["state"]) for r in _outbox_checked(eng)}
    assert states == {("native-app", "Pending"), ("safety-app", "Pending")}
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": False}))
    eng.tick()
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    eng.tick()
    recs = _outbox_checked(eng)
    assert [r["state"] for r in recs if r["dep"] == "native-app"] == ["Pending", "Installing", "Installed"]
    assert [r["state"] for r in recs if r["dep"] == "safety-app"][-2:] == ["Installing", "Installed"]
    assert all(r["manifestVersion"] == 3 for r in recs)

    # next manifest version: native-app removed, safety-app fails fast and is rolled back
    doc = json.loads((VECTORS / "manifest" / "desired.json").read_bytes())
    doc["manifestVersion"] = 4
    doc["deployments"] = [doc["deployments"][1]]
    doc["deployments"][0]["spec"]["parameters"] = {"mode": "fast"}
    body = canonical_json(doc)
    contracts.validate("desired-state", doc)
    assert eng.accept_manifest(body, crypto.sign(vkey(vectors, "cloud"), body), None)
    drv.healthy, drv.fail_reason = False, "process exited (Restart=never)"
    eng.tick()
    recs = _outbox_checked(eng)
    tail = [(r["dep"], r["state"]) for r in recs if r["manifestVersion"] == 4]
    assert ("native-app", "Removed") in tail and ("safety-app", "Failed") in tail
    failed = [r for r in recs if r["state"] == "Failed"][-1]
    assert failed["failFast"] and failed["rolledBackTo"] == "1.0.0" and failed["reason"] == "health check failed"


def test_engine_run_no_driver_and_rejection_records(engine_cfg, vectors, tmp_path):
    eng = Engine(engine_cfg(cloudPublicKey=vectors["manifest"]["cloudPublicKey"], onboardingToken="t"), {})
    eng.import_bundle(str(VECTORS / "bundle" / "bundle.tar"))
    eng.tick()
    assert [r["reason"] for r in _outbox_checked(eng)] == ["no driver for native"]
    # no trusted release key: the manifest version is rejected, nothing reaches the driver
    drv = FakeDriver("native")
    eng2 = Engine(engine_cfg(dataDir=str(tmp_path / "d2"), cloudPublicKey=vectors["manifest"]["cloudPublicKey"],
                             onboardingToken="t", releasePublicKeys=[]), {"native": drv})
    eng2.import_bundle(str(VECTORS / "bundle" / "bundle.tar"))
    eng2.tick()
    recs = _outbox_checked(eng2)
    assert recs[-1]["state"] == "Failed" and recs[-1]["keptPrevious"] is False
    e = contracts.reason_code(recs[-1]["reason"], "reason")
    assert e["code"] == "FE-REC-002"
    inner = re.fullmatch(e["pattern"], recs[-1]["reason"]).group(1)
    assert contracts.reason_code(inner, "detail")["code"] == "FE-ART-SIG-002"
    assert drv.calls == []


# ---- engine <-> reference fake over HTTP ------------------------------------------------------------------------

class Recorder:
    def __init__(self, client):
        self.orig, self.calls = client.request, []
        client.request = self

    def __call__(self, method, path, body=b"", headers=None, signed=True):
        status, hdrs, data = self.orig(method, path, body, headers, signed)
        self.calls.append({"method": method, "path": path, "body": body, "status": status, "headers": hdrs,
                           "data": data})
        return status, hdrs, data


def admin(srv, method, path, body=None, user="carol"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
              headers={"Authorization": "Bearer admin-secret", "X-Flex-User": user, "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").endswith("json") else data)


@pytest.fixture
def fake_cloud(tmp_path, vectors):
    d = tmp_path / "cloud"
    d.mkdir()
    shutil.copytree(VECTORS / "release" / "oci", d / "oci")
    srv = CloudServer(d, 0, "admin-secret", vkey(vectors, "cloud")).start()
    yield srv
    srv.stop()


def plan_apply(srv, dev, items, approver=None, unknown=False):
    s, p = admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items, "allowUnknownDevice": unknown})
    assert s == 201, p
    if p["requiresApproval"]:
        assert admin(srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, approver or "dave")[0] == 200
    s, b = admin(srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 200, b
    return b


def check_call(c):
    """Validate one recorded engine request/response against the southbound contract."""
    p, m = c["path"], c["method"]
    if c["body"]:
        body = json.loads(c["body"])
    if p == "/api/v1/onboarding":
        contracts.validate("onboarding#OnboardingRequest", body)
        contracts.validate("onboarding#OnboardingResponse", json.loads(c["data"]))
        return "onboarding"
    if p.endswith("/capabilities"):
        assert m == "PUT"
        contracts.validate("capabilities", body)
        return "capabilities"
    if re.fullmatch(r"/api/v1/devices/[^/]+/deployments/[^/]+/status", p):
        contracts.validate("status", body)
        assert p.split("/")[-2] == body["dep"]
        return "status"
    if p.endswith("/deployments"):
        if c["status"] == 304:
            return "desired-304"
        contracts.validate("desired-state", json.loads(c["data"]))
        assert re.fullmatch(r'"sha256:[0-9a-f]{64}"', c["headers"]["etag"])
        contracts.validate("trust#Ed25519Signature", c["headers"]["x-flex-manifest-signature"])
        return "desired"
    if "/referrers/" in p:
        contracts.validate("oci-profile#ReferrersIndex", json.loads(c["data"]))
        return "referrers"
    if "/manifests/" in p:
        man = json.loads(c["data"])
        contracts.validate("oci-profile#SignatureManifest" if man.get("subject") else "oci-profile#ReleaseManifest", man)
        return "manifest"
    if "/blobs/" in p:
        return "blob"
    raise AssertionError(f"request outside the southbound contract: {m} {p}")


def test_engine_against_reference_fake(fake_cloud, engine_cfg, vectors, tmp_path):
    srv = fake_cloud
    safe = tmp_path / "safe.json"
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    s, tok = admin(srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": "vec-dev"})
    eng = Engine(engine_cfg(apiUrl=srv.url, onboardingToken=tok["token"], cloudPublicKey=vectors["keys"]["cloud"]["public"],
                            releasePublicKeys=vectors["release"]["trustedReleaseKeys"], safeStateFile=str(safe),
                            pollInterval=0.05), {"native": FakeDriver("native")})
    rec = Recorder(eng.client)
    eng.tick()                                  # onboarding, capabilities, empty desired state (manifestVersion 0)
    plan_apply(srv, "vec-dev", [
        {"id": "native-app", "module": "vec_native", "version": "1.0.0", "driver": "native",
         "parameters": {"mode": "fast", "rate_hz": 10, "verbose": True}, "activation": {"requires": ["machine_parked"],
                                                                                   "window": {"start": "00:00", "end": "23:59"}}},
        {"id": "safety-app", "module": "vec_safety", "version": "1.0.0", "driver": "native"}])
    for _ in range(4):
        eng.tick()
    kinds = {check_call(c) for c in rec.calls}
    assert kinds >= {"onboarding", "capabilities", "desired", "status", "manifest", "referrers", "blob"}
    s, dev = admin(srv, "GET", "/api/v1/admin/devices/vec-dev")
    assert {k: v["state"] for k, v in dev["status"].items()} == {"native-app": "Installed", "safety-app": "Installed"}
    contracts.validate("desired-state", dev["desired"])
    for r in dev["statusLog"]:   # what the fake stored = what the engine sent + receivedAt/deploymentId/deviceId
        contracts.validate("status", {k: v for k, v in r.items() if k not in ("receivedAt", "deploymentId", "deviceId")})
    contracts.validate("capabilities", dev["device"]["capabilities"])

    # the fake's offline bundle for this device follows bundle.v1alpha1 and imports into a fresh engine
    st, hdrs, data = eng.client.request("GET", "/api/v1/devices/vec-dev/bundle")
    assert bundle_layout_problems(data) == []
    b = tmp_path / "b.tar"
    b.write_bytes(data)
    eng2 = Engine(engine_cfg(dataDir=str(tmp_path / "data2"), cloudPublicKey=vectors["keys"]["cloud"]["public"]), {})
    eng2.import_bundle(str(b))
    assert eng2.state["desired"] == dev["desired"]


def test_reference_fake_desired_state_for_child_device_and_all_profiles(fake_cloud):
    """Child-device targeting (<gw>.<child>) and MCU parameters as the fake resolves them from plan items."""
    srv = fake_cloud
    iface = "FLEX01_" + "ab" * 32
    plan_apply(srv, "gw1.mcu0", [{"id": "servo", "module": "vec_native", "version": "1.0.0", "driver": "mcu-module",
                                  "parameters": {"_interfaceHash": iface, "max_speed_dps": 90}}], unknown=True)
    plan_apply(srv, "panda", [
        {"id": "cx-bridge", "module": "vec_native", "version": "1.0.0", "driver": "plc-verify", "parameters": {"plc": "panda/cx"}},
        {"id": "panel", "module": "vec_native", "version": "1.0.0", "driver": "applab", "parameters": {"panel.brightness": 3}},
        {"id": "cell", "module": "vec_native", "version": "1.0.0", "driver": "container",
         "activation": {"requires": [f"plc:cx-bridge@{iface}"]}},
        {"id": "fw", "module": "vec_native", "version": "1.0.0", "driver": "mcu-firmware"}], unknown=True)
    for dev in ("gw1.mcu0", "panda"):
        s, d = admin(srv, "GET", f"/api/v1/admin/devices/{dev}")
        contracts.validate("desired-state", d["desired"])
        assert all(x["metadata"]["deviceId"] == dev for x in d["desired"]["deployments"])


def test_onboarding_refuses_other_cloud_key(fake_cloud, engine_cfg, vectors):
    s, tok = admin(fake_cloud, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": "vec-dev"})
    eng = Engine(engine_cfg(apiUrl=fake_cloud.url, onboardingToken=tok["token"],
                            cloudPublicKey=vectors["keys"]["rogue"]["public"]), {})
    with pytest.raises(RuntimeError) as ei:
        eng.onboard()
    assert contracts.reason_code(str(ei.value))["code"] == "FE-ONB-001" and not eng.state["onboarded"]


# ---- recordings of earlier runs (work dirs; skipped on a fresh checkout) ----------------------------------------

def _states():
    for f in recorded("demo/**/cloud-state.json") + recorded("probes/**/cloud-state.json"):
        yield f, json.loads(f.read_text())


def test_recorded_desired_states_and_status_validate():
    n = 0
    for f, s in _states():
        for dev, d in s["desired"].items():
            contracts.validate("desired-state", {"apiVersion": "flex.dev/v1alpha1", "kind": "DesiredState", "deviceId": dev,
                                                 "manifestVersion": d["manifestVersion"], "deployments": d["deployments"]})
            n += 1
        for r in s["statusLog"]:
            contracts.validate("status", {k: v for k, v in r.items() if k not in ("receivedAt", "deploymentId", "deviceId")})
    if not n:
        pytest.skip("no recorded cloud-state.json in work dirs")


def test_recorded_releases_validate():
    """Release manifests, descriptors and signatures in every OCI layout written by `flex release` / the probe
    release tools (demo/edge/work/tests/*/rel is `flex release` output for examples/models/demo-pick)."""
    n = 0
    for idx in recorded("demo/**/oci/index.json") + recorded("probes/**/oci/index.json") + recorded("tests/work/**/oci/index.json"):
        root = idx.parent
        for e in json.loads(idx.read_text())["manifests"]:
            p = root / "blobs" / "sha256" / e["digest"][7:]
            if not p.exists():
                continue
            man = json.loads(p.read_bytes())
            if man.get("artifactType") == "application/vnd.flex.signature.v1+json":
                contracts.validate("oci-profile#SignatureManifest", man)
                env = json.loads((root / "blobs" / "sha256" / man["layers"][0]["digest"][7:]).read_bytes())
                contracts.validate("trust#SignatureEnvelope", env)
                contracts.validate("trust#SignedPayload", json.loads(base64.b64decode(env["payload"])))
                continue
            contracts.validate("oci-profile#ReleaseManifest", man)
            cfg = root / "blobs" / "sha256" / man["config"]["digest"][7:]
            if cfg.exists():
                contracts.validate("descriptor", json.loads(cfg.read_bytes()))
                n += 1
    if not n:
        pytest.skip("no recorded OCI layouts in work dirs")

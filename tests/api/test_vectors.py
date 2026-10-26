"""CP-3/CP-4 test vectors (api/vectors/): deterministic, and the engine's verification code agrees with
every expected outcome."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flexengine2 import contracts
from flexengine2.artefacts.oci import AT_SIGNATURE, Layout
from flexengine2.artefacts.store import ArtefactStore, VerificationError
from flexengine2.core.reconciler import Engine
from flexengine2.identity import crypto
from flexengine2.util import sha256_digest

from _api_helpers import VECTORS, bundle_layout_problems, tool


def test_vectors_regenerate_byte_identical(tmp_path):
    out = tool("gen_vectors").main(tmp_path / "v")
    have = {p.relative_to(VECTORS): p.read_bytes() for p in VECTORS.rglob("*") if p.is_file()}
    new = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert set(have) == set(new)
    assert [k for k in have if have[k] != new[k]] == []


def test_keys_are_the_documented_seeds(vectors):
    for role, k in vectors["keys"].items():
        if role.startswith("$"):
            continue
        priv = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(k["seed"].encode()).digest())
        assert crypto.b64(crypto.private_bytes(priv)) == k["private"]
        assert crypto.public_b64(priv) == k["public"] and crypto.key_id(k["public"]) == k["keyId"]


# ---- manifest signature (X-Flex-Manifest-Signature / desired.sig) ----------------------------------------------

@pytest.mark.parametrize("name", ["good", "tampered", "wrong-key", "missing"])
def test_manifest_signature_cases(name, vectors, engine_cfg):
    mc = vectors["manifest"]
    case = next(c for c in mc["cases"] if c["name"] == name)
    body = (VECTORS / "manifest" / case["body"]).read_bytes()
    contracts.validate("desired-state", json.loads(body))
    eng = Engine(engine_cfg(cloudPublicKey=mc["cloudPublicKey"]), drivers={})
    accepted = eng.accept_manifest(body, case["signature"], mc["etag"])
    assert accepted is (case["expect"] == "accept")
    if accepted:
        assert eng.state["desired"] == json.loads(body) and eng.state["etag"] == mc["etag"]
        assert mc["etag"] == '"' + sha256_digest(body) + '"'
    else:
        assert eng.state["desired"] is None
        assert contracts.reason_code("desired state with bad/missing cloud signature ignored")["code"] == case["reasonCode"]


# ---- device request signatures ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["good-get", "good-post", "tampered-body", "wrong-key"])
def test_request_signature_cases(name, vectors):
    rc = vectors["request"]
    c = next(x for x in rc["cases"] if x["name"] == name)
    h = c["headers"]
    contracts.validate("trust#RequestSignatureHeaders", h)
    sts = crypto.request_string(c["method"], c["path"], h["X-Flex-Date"], h["X-Flex-Nonce"], c["body"].encode())
    assert sts.decode() == c["stringToSign"]
    assert crypto.verify(rc["devicePublicKey"], sts, h["X-Flex-Signature"]) is (c["expect"] == "accept")
    if c["expect"] == "accept":  # Ed25519 is deterministic: the engine's signer reproduces the vector
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(vectors["keys"]["device"]["private"]))
        assert crypto.sign(key, sts) == h["X-Flex-Signature"]
        if c["body"]:
            contracts.validate("status", json.loads(c["body"]))


# ---- release verification (CP-3 + CP-4) -------------------------------------------------------------------------

class VectorRegistry:
    """CP-3 OCI subset served from the vector layout (the shape artefacts/store.py uses from CloudClient)."""

    def __init__(self, layout: Layout, serve: dict | None = None):
        self.layout, self.serve, self.paths = layout, serve or {}, []

    def request(self, method, path, body=b"", headers=None, signed=True):
        self.paths.append(path)
        m = re.fullmatch(r"/v2/(.+)/(manifests|blobs|referrers)/(sha256:[0-9a-f]{64})", path)
        assert method == "GET" and m, path
        if m[2] == "referrers":
            return 200, {}, json.dumps(self.layout.referrers(m[3])).encode()
        if m[3] in self.serve:
            return 200, {}, (VECTORS / "release" / self.serve[m[3]]).read_bytes()
        return 200, {}, self.layout.get_blob(m[3])


@pytest.fixture(scope="module")
def vlayout():
    return Layout(VECTORS / "release" / "oci")


def _release_cases():
    return json.loads((VECTORS / "release" / "cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _release_cases(), ids=lambda c: c["name"])
def test_release_cases(case, vectors, vlayout, tmp_path):
    reg = VectorRegistry(vlayout, case.get("serve"))
    store = ArtefactStore(tmp_path, reg, vectors["release"]["trustedReleaseKeys"])
    cls = case.get("safetyClassOverride", case["safetyClass"])
    if case["expect"] == "accept":
        rel = store.verify_release(case["repository"], case["digest"], case["profile"], cls)
        assert rel.digest == case["digest"]
        contracts.validate("descriptor", rel.descriptor)
        contracts.validate("trust#SignedPayload", rel.signature)
        assert all(p.startswith(f"/v2/{case['repository']}/") for p in reg.paths)
        return
    with pytest.raises(VerificationError) as ei:
        store.verify_release(case["repository"], case["digest"], case["profile"], cls)
    msg = str(ei.value)
    assert re.search(case["reasonPattern"], msg), msg
    assert contracts.reason_code(msg, "detail")["code"] == case["reasonCode"], msg
    assert contracts.reason_code(f"artefact rejected: {msg}", "reason")["code"] == "FE-REC-002"


def test_vector_layout_documents_validate(vlayout):
    idx = json.loads((VECTORS / "release" / "oci" / "index.json").read_text())
    kinds = set()
    for e in idx["manifests"]:
        man = vlayout.get_manifest(e["digest"])
        if man.get("artifactType") == AT_SIGNATURE:
            kinds.add("sig")
            contracts.validate("oci-profile#SignatureManifest", man)
            env = json.loads(vlayout.get_blob(man["layers"][0]["digest"]))
            contracts.validate("trust#SignatureEnvelope", env)
            contracts.validate("trust#SignedPayload", json.loads(base64.b64decode(env["payload"])))
            contracts.validate("oci-profile#ReferrersIndex", vlayout.referrers(man["subject"]["digest"]))
        else:
            kinds.add("rel")
            contracts.validate("oci-profile#ReleaseManifest", man)
            contracts.validate("descriptor", json.loads(vlayout.get_blob(man["config"]["digest"])))
    assert kinds == {"sig", "rel"}


# ---- offline bundle -----------------------------------------------------------------------------------------------

def test_bundle_vector_layout():
    assert bundle_layout_problems((VECTORS / "bundle" / "bundle.tar").read_bytes()) == []


def test_bundle_import_then_offline_verify(vectors, engine_cfg):
    mc = vectors["manifest"]
    eng = Engine(engine_cfg(cloudPublicKey=mc["cloudPublicKey"],
                            releasePublicKeys=vectors["release"]["trustedReleaseKeys"]), drivers={})
    eng.import_bundle(str(VECTORS / "bundle" / "bundle.tar"))
    assert eng.state["desired"]["manifestVersion"] == 3
    eng.store.client = None  # offline: everything must come from the bundle
    for d in eng.state["desired"]["deployments"]:
        p = d["spec"]["deploymentProfile"]["components"][0]["properties"]
        repo = p["repository"].removeprefix("oci://").split("/", 1)[1]
        rel = eng.store.verify_release(repo, p["digest"], d["x-flex-driver"], d["x-flex-safetyClass"])
        assert rel.digest == p["digest"]


def test_tampered_bundle_refused(engine_cfg, vectors):
    eng = Engine(engine_cfg(cloudPublicKey=vectors["manifest"]["cloudPublicKey"]), drivers={})
    with pytest.raises(VerificationError) as ei:
        eng.import_bundle(str(VECTORS / "bundle" / "bundle-tampered.tar"))
    assert contracts.reason_code(str(ei.value), "detail")["code"] == "FE-ART-DIG-001"
    assert eng.state["desired"] is None

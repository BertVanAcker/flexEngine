"""Generate the CP-3/CP-4 test vectors under flexengine/api/vectors/ (deterministic: fixed keys, fixed times).

    flexengine/.venv/bin/python flexengine/api/tools/gen_vectors.py [OUT_DIR]

TEST KEYS ONLY: every private key is derived from a public seed string and published in vectors/keys.json.
Never configure them as trust anchors outside tests.

Output (default flexengine/api/vectors/):
  keys.json                      cloud / release / rogue / device test keys (private, public, keyid)
  manifest/desired.json          exact desired-state bytes (canonical JSON) signed by the cloud key
  manifest/desired-tampered.json same document with manifestVersion changed (signature of the original)
  manifest/cases.json            X-Flex-Manifest-Signature cases: good, tampered, wrong key, missing
  request/cases.json             device request signatures (string to sign, headers): good, tampered, wrong key
  release/oci/                   OCI image layout: releases + signature referrers (CP-3)
  release/tampered/<hex>         bytes served instead of blob <hex> in the "tampered" case
  release/cases.json             release verification cases: good, tampered, unsigned, wrong key, bad signature,
                                 safety two approvers, safety single approver
  bundle/bundle.tar              offline bundle for device vec-dev (bundle.v1alpha1)
  bundle/bundle-tampered.tar     same, one blob altered
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flexengine2.artefacts.oci import AT_SIGNATURE, EMPTY_JSON, MT_EMPTY, MT_MANIFEST, MT_PAYLOAD_NATIVE, Layout
from flexengine2.identity import crypto
from flexengine2.testing import release as reltool
from flexengine2.util import canonical_json, sha256_digest

OUT = Path(__file__).resolve().parents[1] / "vectors"
DEVICE = "vec-dev"
REGISTRY = "registry.vec.invalid"
SIGNED_AT = "2026-10-17T00:00:00.000Z"
DATE_MS = "1792195200000"            # 2026-10-17T00:00:00Z
NONCE = "000102030405060708090a0b"


def key(role: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(f"flexengine2 test vector key: {role}".encode()).digest())


KEYS = {r: key(r) for r in ("cloud", "release", "rogue", "device")}


def pub(role: str) -> str:
    return crypto.public_b64(KEYS[role])


def write(p: Path, data: bytes | str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)


def jdump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def native_payload(module: str, version: str) -> bytes:
    files = {"app.py": f"# {module} {version} (test vector payload)\nprint('ok')\n".encode(),
             "release.json": canonical_json({"app": module, "version": version})}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid = len(files[name]), 0, 0o644, 0, 0
            tf.addfile(ti, io.BytesIO(files[name]))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def descriptor(module: str, version: str, safety: str) -> dict:
    return {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module, "moduleVersion": "0.1.0",
            "releaseVersion": version, "interfaceHash": "FLEX01_" + hashlib.sha256(module.encode()).hexdigest(),
            "language": "python", "profiles": ["native"],
            "health": {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.25,
                       "successThreshold": 2, "timeoutSeconds": 8},
            "restartPolicy": {"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5},
            "safetyClass": safety, "safetyClassSource": "test vector"}


def publish(layout: Layout, module: str, version: str, safety: str = "none") -> dict:
    repo = f"flexnodes/{module}"
    m = reltool.publish_release(layout, repo, descriptor(module, version, safety),
                                [(MT_PAYLOAD_NATIVE, native_payload(module, version),
                                  {"dev.flex.profile": "native", "dev.flex.platform": "linux/any+python3"})],
                                reltool.sbom_stub(module, version, "native"))
    return {"repository": repo, "module": module, "version": version, "safetyClass": safety, **m}


def sign(layout: Layout, rel: dict, role: str, approvers: list[str] | None = None, tamper: bool = False):
    approval = {"approved": True, "approvers": approvers, "role": "safety-engineer"} if approvers else None
    if not tamper:
        return reltool.sign_release(layout, KEYS[role], rel["repository"], rel, rel["version"], approval)
    # bad signature: the envelope carries a payload other than the signed one (same trusted keyid)
    payload = {"critical": {"type": "flex release signature v1", "identity": {"repository": rel["repository"]},
                            "image": {"manifest-digest": rel["digest"]}},
               "optional": {"version": rel["version"], "signedAt": SIGNED_AT,
                            "safetyApproval": {"approved": False, "approvers": []}}}
    signed = canonical_json(payload)
    payload["optional"]["version"] = rel["version"] + "-altered"
    env = {"payload": crypto.b64(canonical_json(payload)), "signature": crypto.sign(KEYS[role], signed),
           "keyid": crypto.key_id(pub(role))}
    layer = layout.put_blob(canonical_json(env))
    empty = layout.put_blob(EMPTY_JSON)
    return layout.put_manifest({"schemaVersion": 2, "mediaType": MT_MANIFEST, "artifactType": AT_SIGNATURE,
                                "config": {"mediaType": MT_EMPTY, **empty},
                                "layers": [{"mediaType": AT_SIGNATURE, **layer}],
                                "subject": {"mediaType": rel["mediaType"], "digest": rel["digest"], "size": rel["size"]}})


def deployment(rel: dict, dep_id: str, **extra) -> dict:
    d = {"apiVersion": "application.margo.org/v1alpha1", "kind": "ApplicationDeployment",
         "metadata": {"id": dep_id, "name": dep_id, "namespace": "default", "deviceId": DEVICE},
         "spec": {"applicationId": rel["module"],
                  "deploymentProfile": {"type": "custom", "components": [{"name": rel["module"], "properties": {
                      "repository": f"oci://{REGISTRY}/{rel['repository']}", "revision": rel["version"],
                      "digest": rel["digest"], "size": rel["size"]}}]},
                  "parameters": {}},
         "x-flex-driver": "native", "x-flex-safetyClass": rel["safetyClass"]}
    d.update(extra)
    return d


def bundle(layout: Layout, desired: bytes, sig: str, tamper: str | None = None) -> bytes:
    """Same member order and content as the reference fake (testing/fakecloud.py _bundle)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        def add(name, data):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime = len(data), 0
            tf.addfile(ti, io.BytesIO(data))
        add("desired.json", desired)
        add("desired.sig", sig.encode())
        seen = set()
        for dep in json.loads(desired)["deployments"]:
            md = dep["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]
            for d in [md] + [r["digest"] for r in layout.referrers(md)["manifests"]]:
                man = layout.get_manifest(d)
                for b in [d, man["config"]["digest"]] + [l["digest"] for l in man["layers"]]:
                    if b not in seen:
                        seen.add(b)
                        data = layout.get_blob(b)
                        if b == tamper:
                            data = data.replace(b"print('ok')", b"print('xx')") if b"print" in data else data + b" "
                        add(f"blobs/{b.replace(':', '/')}", data)
    return buf.getvalue()


def main(out: Path = OUT) -> Path:
    reltool.iso = lambda t=None: SIGNED_AT   # fixed signedAt in release signatures
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    write(out / "keys.json", jdump({r: {"private": crypto.b64(crypto.private_bytes(k)), "public": pub(r),
                                        "keyId": crypto.key_id(pub(r)), "seed": f"flexengine2 test vector key: {r}"}
                                    for r, k in KEYS.items()} | {"$comment": "TEST KEYS ONLY (derived from public seeds)."}))

    # ---- releases (CP-3 + CP-4) ----
    layout = Layout(out / "release" / "oci")
    good = publish(layout, "vec_native", "1.0.0")
    sign(layout, good, "release")
    unsigned = publish(layout, "vec_unsigned", "1.0.0")
    rogue = publish(layout, "vec_rogue", "1.0.0")
    sign(layout, rogue, "rogue")
    badsig = publish(layout, "vec_badsig", "1.0.0")
    sign(layout, badsig, "release", tamper=True)
    safe2 = publish(layout, "vec_safety", "1.0.0", "safety-relevant")
    sign(layout, safe2, "release", approvers=["alice", "bob"])
    safe1 = publish(layout, "vec_safety", "1.0.1", "safety-relevant")
    sign(layout, safe1, "release", approvers=["alice", "alice"])
    good_cfg = layout.get_manifest(good["digest"])["config"]["digest"]
    tampered_bytes = layout.get_blob(good_cfg).replace(b'"safetyClass":"none"', b'"safetyClass":"nonE"')
    write(out / "release" / "tampered" / good_cfg.split(":")[1], tampered_bytes)

    def case(name, rel, expect, code=None, pattern=None, served=None, **kw):
        c = {"name": name, "repository": rel["repository"], "digest": rel["digest"], "profile": "native",
             "safetyClass": rel["safetyClass"], "expect": expect}
        if code:
            c.update(reasonCode=code, reasonPattern=pattern)
        if served:
            c["serve"] = served
        return c | kw
    tid = crypto.key_id(pub("rogue"))
    write(out / "release" / "cases.json", jdump({
        "trustedReleaseKeys": [pub("release")],
        "cases": [
            case("good", good, "accept"),
            case("tampered", good, "reject", "FE-ART-DIG-002", "^digest mismatch: ",
                 served={good_cfg: f"tampered/{good_cfg.split(':')[1]}"}),
            case("unsigned", unsigned, "reject", "FE-ART-SIG-001", r"^release not signed by a trusted key \(no signature\)$"),
            case("wrong-key", rogue, "reject", "FE-ART-SIG-002", rf"^release not signed by a trusted key \(untrusted key {tid}\)$"),
            case("bad-signature", badsig, "reject", "FE-ART-SIG-002", r"^release not signed by a trusted key \(bad signature\)$"),
            case("safety-two-approvers", safe2, "accept"),
            case("safety-single-approver", safe1, "reject", "FE-ART-SAF-001",
                 "^safety-relevant release lacks a two-person approval in its signed payload$"),
            case("safety-class-mismatch", good, "reject", "FE-ART-SAF-002", "^safety class mismatch: ",
                 safetyClassOverride="safety-relevant"),
        ]}))

    # ---- desired state + manifest signature (CP-1 + CP-4) ----
    doc = {"apiVersion": "flex.dev/v1alpha1", "kind": "DesiredState", "deviceId": DEVICE, "manifestVersion": 3,
           "deployments": [deployment(good, "native-app", **{"x-flex-activation": {"requires": ["machine_parked"]}}),
                           deployment(safe2, "safety-app")]}
    body = canonical_json(doc)
    tdoc = dict(doc, manifestVersion=4)
    tbody = canonical_json(tdoc)
    write(out / "manifest" / "desired.json", body)
    write(out / "manifest" / "desired-tampered.json", tbody)
    sig = crypto.sign(KEYS["cloud"], body)
    write(out / "manifest" / "cases.json", jdump({
        "cloudPublicKey": pub("cloud"), "etag": '"' + sha256_digest(body) + '"',
        "cases": [
            {"name": "good", "body": "desired.json", "signature": sig, "expect": "accept"},
            {"name": "tampered", "body": "desired-tampered.json", "signature": sig, "expect": "reject", "reasonCode": "FE-DS-001"},
            {"name": "wrong-key", "body": "desired.json", "signature": crypto.sign(KEYS["rogue"], body), "expect": "reject", "reasonCode": "FE-DS-001"},
            {"name": "missing", "body": "desired.json", "signature": "", "expect": "reject", "reasonCode": "FE-DS-001"},
        ]}))

    # ---- device request signatures (CP-4) ----
    def req(method, path, body_bytes, role="device"):
        sts = crypto.request_string(method, path, DATE_MS, NONCE, body_bytes)
        return {"method": method, "path": path, "body": body_bytes.decode(), "stringToSign": sts.decode(),
                "headers": {"X-Flex-Device": DEVICE, "X-Flex-Date": DATE_MS, "X-Flex-Nonce": NONCE,
                            "X-Flex-Signature": crypto.sign(KEYS[role], sts)}}
    status = canonical_json({"dep": "native-app", "seq": 1, "state": "Pending", "deviceTime": 1792195200.0,
                             "manifestVersion": 3, "reason": "awaiting safe state: machine_parked", "version": "1.0.0"})
    g1 = req("GET", f"/api/v1/devices/{DEVICE}/deployments", b"")
    g2 = req("POST", f"/api/v1/devices/{DEVICE}/deployments/native-app/status", status)
    tam = dict(g2, body=g2["body"].replace('"seq":1', '"seq":2'))
    tam["stringToSign"] = crypto.request_string("POST", g2["path"], DATE_MS, NONCE, tam["body"].encode()).decode()
    write(out / "request" / "cases.json", jdump({
        "devicePublicKey": pub("device"),
        "cases": [{"name": "good-get", "expect": "accept", **g1}, {"name": "good-post", "expect": "accept", **g2},
                  {"name": "tampered-body", "expect": "reject", **tam},
                  {"name": "wrong-key", "expect": "reject", **req("GET", g1["path"], b"", "rogue")}]}))

    # ---- offline bundle (CP-3) ----
    write(out / "bundle" / "bundle.tar", bundle(layout, body, sig))
    write(out / "bundle" / "bundle-tampered.tar", bundle(layout, body, sig, tamper=good_cfg))
    return out


if __name__ == "__main__":
    print(main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))

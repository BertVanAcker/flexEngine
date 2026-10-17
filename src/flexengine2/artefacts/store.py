"""Artefact fetch + verification into a local content-addressed store (CAS).

verify_release() is the supply-chain gate (WS3 §1.5, WS9 FC-S1/FC-S2, synthesis E2):
  1. release manifest bytes hash to the digest named in the (cloud-signed) desired state
  2. a referrer signature manifest exists whose payload names exactly that digest and repository,
     signed by a trusted release key  -> otherwise "unsigned" / "bad signature"
  3. safety-relevant deployments: the signed payload must carry an approval by >= 2 distinct people
  4. config (descriptor) and the payload layer for the requested profile hash to their digests
Blobs already in the CAS (verified earlier, or imported from an offline bundle) are not re-downloaded.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from flexengine2.identity import crypto
from flexengine2.util import atomic_write_bytes, sha256_digest
from flexengine2.artefacts.oci import AT_SIGNATURE, MT_DESCRIPTOR, MT_SBOM
from flexengine2.cloud.client import CloudClient


class VerificationError(Exception):
    pass


@dataclass
class Release:
    digest: str
    descriptor: dict
    payload_path: str
    payload_annotations: dict
    signature: dict


class ArtefactStore:
    def __init__(self, root: Path, client: CloudClient | None, release_keys: list[str]):
        self.root = Path(root) / "cas" / "sha256"
        self.root.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.trusted = {crypto.key_id(k): k for k in release_keys}
        self.downloads = 0

    def path(self, digest: str) -> Path:
        return self.root / digest.split(":", 1)[1]

    def put(self, digest: str, data: bytes):
        if sha256_digest(data) != digest:
            raise VerificationError(f"digest mismatch for {digest}")
        atomic_write_bytes(self.path(digest), data)

    def get(self, repo: str, digest: str, kind: str = "blobs") -> bytes:
        p = self.path(digest)
        if p.exists():
            data = p.read_bytes()
            if sha256_digest(data) == digest:
                return data
            p.unlink()  # local corruption: refetch
        if self.client is None:
            raise VerificationError(f"{digest} not in local store and no cloud client (offline bundle incomplete)")
        _, _, data = self.client.request("GET", f"/v2/{repo}/{kind}/{digest}")
        self.downloads += 1
        if sha256_digest(data) != digest:
            raise VerificationError(f"digest mismatch: {repo} {kind}/{digest} hashes to {sha256_digest(data)}")
        atomic_write_bytes(p, data)
        return data

    def referrers(self, repo: str, digest: str) -> list[dict]:
        idx_path = self.root.parent / "referrers" / digest.split(":", 1)[1]
        if idx_path.exists():
            return json.loads(idx_path.read_bytes())["manifests"]
        if self.client is None:
            raise VerificationError("no referrers cached and no cloud client")
        _, _, data = self.client.request("GET", f"/v2/{repo}/referrers/{digest}")
        found = json.loads(data)["manifests"]
        if found:  # never cache "no signature yet"
            atomic_write_bytes(idx_path, data)
        return found

    def verify_release(self, repo: str, digest: str, profile: str, safety_class: str = "none",
                       platform: dict | None = None) -> Release:
        man = json.loads(self.get(repo, digest, "manifests"))
        # --- signature (referrers) ---
        sig_ok, sig_payload, reasons = False, None, []
        for ref in self.referrers(repo, digest):
            if ref.get("artifactType") != AT_SIGNATURE:
                continue
            sman = json.loads(self.get(repo, ref["digest"], "manifests"))
            if (sman.get("subject") or {}).get("digest") != digest:
                reasons.append("signature subject mismatch")
                continue
            doc = json.loads(self.get(repo, sman["layers"][0]["digest"]))
            pub = self.trusted.get(doc.get("keyid"))
            payload_bytes = crypto.unb64(doc["payload"])
            if not pub:
                reasons.append(f"untrusted key {doc.get('keyid')}")
                continue
            if not crypto.verify(pub, payload_bytes, doc["signature"]):
                reasons.append("bad signature")
                continue
            payload = json.loads(payload_bytes)
            crit = payload.get("critical", {})
            if crit.get("image", {}).get("manifest-digest") != digest or crit.get("identity", {}).get("repository") != repo:
                reasons.append("signed payload names a different artefact")
                continue
            sig_ok, sig_payload = True, payload
            break
        if not sig_ok:
            raise VerificationError("release not signed by a trusted key" + (f" ({'; '.join(reasons)})" if reasons else " (no signature)"))
        if safety_class != "none":
            appr = sig_payload.get("optional", {}).get("safetyApproval", {})
            if not appr.get("approved") or len(set(appr.get("approvers", []))) < 2:
                raise VerificationError("safety-relevant release lacks a two-person approval in its signed payload")
        # --- descriptor + payload ---
        if man["config"]["mediaType"] != MT_DESCRIPTOR:
            raise VerificationError("not a flexNode release")
        descriptor = json.loads(self.get(repo, man["config"]["digest"]))
        # S4: the class in the signed descriptor (derived from the model by the release tool) must match the
        # class the desired state gates this deployment with; otherwise the interlock could be skipped.
        if descriptor.get("safetyClass", "none") != safety_class:
            raise VerificationError(f"safety class mismatch: release descriptor says "
                                    f"{descriptor.get('safetyClass', 'none')!r}, desired state says {safety_class!r}")
        layers = [l for l in man["layers"] if (l.get("annotations") or {}).get("dev.flex.profile") == profile]
        if platform:
            # multi-platform release (D-PICK: one FLXM layer per MCU profile): pick the layer whose platform
            # annotations match the executor; a single-platform release without annotations still fits
            fit = [l for l in layers if all((l.get("annotations") or {}).get(k) == v for k, v in platform.items())]
            if not fit and len(layers) > 1:
                have = [{k: (l.get("annotations") or {}).get(k) for k in platform} for l in layers]
                raise VerificationError(f"release has no {profile!r} payload for platform {platform} (has {have})")
            layers = fit or layers
        layer = layers[0] if layers else None
        if layer is None:
            raise VerificationError(f"release has no payload for profile {profile!r}")
        self.get(repo, layer["digest"])
        for l in man["layers"]:
            if l["mediaType"] == MT_SBOM:
                self.get(repo, l["digest"])  # SBOM must be present and intact (CRA evidence)
        return Release(digest=digest, descriptor=descriptor, payload_path=str(self.path(layer["digest"])),
                       payload_annotations=layer.get("annotations") or {}, signature=sig_payload)

    def import_bundle(self, data: bytes) -> tuple[bytes, str]:
        """Load an offline bundle: every blob is hash-checked on the way in. Returns (desired, signature)."""
        desired, sig = None, None
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                b = tf.extractfile(m).read()
                if m.name == "desired.json":
                    desired = b
                elif m.name == "desired.sig":
                    sig = b.decode()
                elif m.name.startswith("blobs/sha256/"):
                    self.put("sha256:" + m.name.rsplit("/", 1)[1], b)
        # rebuild referrer indexes from the signature manifests found in the bundle
        for p in self.root.iterdir():
            try:
                man = json.loads(p.read_bytes())
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(man, dict) and man.get("artifactType") == AT_SIGNATURE and man.get("subject"):
                idx_path = self.root.parent / "referrers" / man["subject"]["digest"].split(":", 1)[1]
                entry = {"mediaType": man["mediaType"], "digest": "sha256:" + p.name, "artifactType": AT_SIGNATURE}
                atomic_write_bytes(idx_path, json.dumps({"manifests": [entry]}).encode())
        if desired is None or sig is None:
            raise VerificationError("bundle without desired state")
        return desired, sig

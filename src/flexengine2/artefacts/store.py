"""Artefact fetch + verification into a local content-addressed store (CAS).

verify_release() is the supply-chain gate (WS3 §1.5, WS9 FC-S1/FC-S2, synthesis E2):
  1. release manifest bytes hash to the digest named in the (cloud-signed) desired state
  2. a referrer signature manifest exists whose payload names exactly that digest and repository,
     signed by a trusted release key  -> otherwise "unsigned" / "bad signature"
  3. safety-relevant deployments: the signed payload must carry an approval by >= 2 distinct people
  4. config (descriptor) and the payload layer for the requested profile hash to their digests
FE-4: a driver that names its `media_types` (the compose driver) gets only layers of those types; every other driver
never gets a compose or a source layer. With `source=True` (x-flex-build) the source layer of the profile is fetched
too (`Release.source_path`, None when the release has none) and the binary payload becomes optional.
Blobs already in the CAS (verified earlier, or imported from an offline bundle) are not re-downloaded.
"""

from __future__ import annotations

import io
import itertools
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from flexengine2.identity import crypto
from flexengine2.util import atomic_write_bytes, sha256_digest
from flexengine2.artefacts.oci import AT_SIGNATURE, MT_DESCRIPTOR, MT_PAYLOAD_COMPOSE, MT_PAYLOAD_SOURCE, MT_SBOM
from flexengine2.cloud.client import CloudClient, Offline, Rejected


class VerificationError(Exception):
    pass


# profiles whose payload layers must carry these platform annotations (DA-15: MCU module layers stay strict)
STRICT_PLATFORM = {"mcu-module": ("dev.flex.arch", "dev.flex.floatAbi")}


@dataclass
class Release:
    digest: str
    descriptor: dict
    payload_path: str
    payload_annotations: dict
    signature: dict
    source_path: str | None = None        # FE-4: verified source payload (x-flex-build), when asked for


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

    def referrers(self, repo: str, digest: str, refresh: bool = False) -> list[dict]:
        idx_path = self.root.parent / "referrers" / digest.split(":", 1)[1]
        if idx_path.exists() and not (refresh and self.client is not None):
            return json.loads(idx_path.read_bytes())["manifests"]
        if self.client is None:
            raise VerificationError("no referrers cached and no cloud client")
        _, _, data = self.client.request("GET", f"/v2/{repo}/referrers/{digest}")
        found = json.loads(data)["manifests"]
        if found:  # never cache "no signature yet"
            atomic_write_bytes(idx_path, data)
        return found

    def _fresh_referrers(self, repo: str, digest: str, have: list[dict]):
        """Lazily yields cloud referrers not in `have` (only consulted when the cached ones did not verify)."""
        seen = {r.get("digest") for r in have}
        try:
            for r in self.referrers(repo, digest, refresh=True):
                if r.get("digest") not in seen:
                    yield r
        except (Offline, Rejected):
            return

    def verify_release(self, repo: str, digest: str, profile: str, safety_class: str = "none",
                       platform: dict | None = None, media_types: tuple | None = None,
                       source: bool = False) -> Release:
        man = json.loads(self.get(repo, digest, "manifests"))
        # --- signature (referrers) ---
        sig_ok, sig_payload, reasons = False, None, []
        cached = (self.root.parent / "referrers" / digest.split(":", 1)[1]).exists()
        refs = self.referrers(repo, digest)
        if cached and self.client is not None:
            # a cached index that verifies nothing is fetched once more from the cloud (a stale or foreign index
            # must not block a release the cloud has properly signed)
            refs = itertools.chain(refs, self._fresh_referrers(repo, digest, refs))   # lazy: only after a miss
        for ref in refs:
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
        mine = [l for l in man["layers"] if (l.get("annotations") or {}).get("dev.flex.profile") == profile]
        src = next((l for l in mine if l.get("mediaType") == MT_PAYLOAD_SOURCE), None)
        if media_types:
            layers = [l for l in mine if l.get("mediaType") in media_types]
        else:
            layers = [l for l in mine if l.get("mediaType") not in (MT_PAYLOAD_SOURCE, MT_PAYLOAD_COMPOSE)]
        strict = STRICT_PLATFORM.get(profile)
        if strict and layers:
            # DA-15: the permissive match below stays for native / container layers, but an MCU module layer is
            # selectable only when it names its CPU profile and float ABI (a module for another core or float ABI
            # must never be picked by an "absent key fits any value" rule)
            named = [l for l in layers if all((l.get("annotations") or {}).get(k) for k in strict)]
            if not named:
                missing = sorted({k for l in layers for k in strict if not (l.get("annotations") or {}).get(k)})
                raise VerificationError(f"{profile} layer without {', '.join(missing)} cannot be selected "
                                        f"(DA-15: {profile} layers must name {', '.join(strict)})")
            layers = named
        if platform:
            # multi-platform release (D-PICK: one FLXM layer per MCU profile; EV-37: one native layer per CPU /
            # Python; EV-40: one container image per CPU): pick the layer whose platform annotations do not
            # contradict the executor's (an absent key fits any value: a Rust binary layer names only dev.flex.arch),
            # the most specific one first; a single-platform release without annotations still fits (a single
            # layer for another platform is the driver preflight's refusal)
            def _score(l):
                ann = l.get("annotations") or {}
                if any(k in ann and ann[k] != v for k, v in platform.items()):
                    return -1
                return sum(1 for k in platform if k in ann)
            fit = sorted((l for l in layers if _score(l) >= 0), key=_score, reverse=True)
            if not fit and len(layers) > 1:
                have = [{k: (l.get("annotations") or {}).get(k) for k in platform} for l in layers]
                raise VerificationError(f"release has no {profile!r} payload for platform {platform} (has {have})")
            layers = fit or layers
        layer = layers[0] if layers else None
        if layer is None and not (source and src is not None):
            raise VerificationError(f"release has no payload for profile {profile!r}")
        if layer is not None:
            self.get(repo, layer["digest"])
        if source and src is not None:
            self.get(repo, src["digest"])
        for l in man["layers"]:
            if l["mediaType"] == MT_SBOM:
                self.get(repo, l["digest"])  # SBOM must be present and intact (CRA evidence)
        return Release(digest=digest, descriptor=descriptor,
                       payload_path=str(self.path(layer["digest"])) if layer is not None else "",
                       payload_annotations=(layer.get("annotations") or {}) if layer is not None else {},
                       signature=sig_payload,
                       source_path=str(self.path(src["digest"])) if source and src is not None else None)

    def import_bundle(self, data: bytes, accept=None) -> tuple[bytes, str]:
        """Load an offline bundle. Returns (desired, signature).

        Nothing is stored until the whole bundle has been read, every blob hash-checked, and -- when `accept` is given
        (the engine passes a check of the desired state's signature against its cloud key) -- the bundle's desired
        state is authenticated. A refused bundle therefore leaves the content-addressed store and the referrer
        indexes untouched (EV-12 finding: a forged signature manifest in a refused bundle used to block later online
        installs). Referrer indexes are extended only with the bundle's own signature manifests, merged with what
        is cached."""
        desired, sig, blobs = None, None, {}
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
                    digest = "sha256:" + m.name.rsplit("/", 1)[1]
                    if sha256_digest(b) != digest:
                        raise VerificationError(f"digest mismatch for {digest}")
                    blobs[digest] = b
        if desired is None or sig is None:
            raise VerificationError("bundle without desired state")
        if accept is not None and not accept(desired, sig):
            raise VerificationError("bundle desired state is not signed by the cloud key")
        for digest, b in blobs.items():
            self.put(digest, b)
        signed = set()
        for digest, b in blobs.items():
            try:
                man = json.loads(b)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(man, dict) and man.get("artifactType") == AT_SIGNATURE and man.get("subject"):
                signed.add(man["subject"]["digest"])
                idx_path = self.root.parent / "referrers" / man["subject"]["digest"].split(":", 1)[1]
                try:
                    have = json.loads(idx_path.read_bytes())["manifests"]
                except (FileNotFoundError, ValueError, KeyError):
                    have = []
                if all(e.get("digest") != digest for e in have):
                    have.append({"mediaType": man["mediaType"], "digest": digest, "artifactType": AT_SIGNATURE})
                    atomic_write_bytes(idx_path, json.dumps({"manifests": have}).encode())
        # A release the (accepted) desired state names without a signature in the bundle gets an empty referrer
        # index (EV-13): the bundle is the complete offline answer, so verification fails "no signature"
        # (FE-ART-SIG-001) at once instead of asking an unreachable cloud forever. verify_release still asks the cloud
        # once more when a cached index verifies nothing, so a signature published later is found online.
        for digest in _release_digests(desired):
            if digest in signed:
                continue
            idx_path = self.root.parent / "referrers" / digest.split(":", 1)[1]
            if not idx_path.exists():
                atomic_write_bytes(idx_path, json.dumps({"manifests": []}).encode())
        return desired, sig


def _release_digests(desired: bytes) -> list[str]:
    """sha256 release digests named by the components of a desired-state document (malformed parts skipped)."""
    try:
        doc = json.loads(desired)
    except (ValueError, UnicodeDecodeError):
        return []
    out = []
    for d in (doc.get("deployments") or []) if isinstance(doc, dict) else []:
        try:
            comps = d["spec"]["deploymentProfile"]["components"]
        except (KeyError, TypeError):
            continue
        for c in comps if isinstance(comps, list) else []:
            dg = ((c or {}).get("properties") or {}).get("digest") if isinstance(c, dict) else None
            if isinstance(dg, str) and dg.startswith("sha256:") and len(dg) == 71:
                out.append(dg)
    return out

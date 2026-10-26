"""A minimal OCI image layout on disk (blobs/sha256/<hex>, index.json, oci-layout).

It plays the role of the flexCloud OCI registry: the release tool writes into it, the cloud stub
serves it read-only through an OCI-distribution-shaped subset (`/v2/<repo>/manifests|blobs|referrers`).
No registry image is available offline in the probe container, so a layout on disk replaces it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from flexengine2.util import atomic_write_bytes, atomic_write_json, canonical_json, read_json, sha256_digest

MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_INDEX = "application/vnd.oci.image.index.v1+json"
MT_EMPTY = "application/vnd.oci.empty.v1+json"
AT_NODE = "application/vnd.flex.node.v1+json"
MT_DESCRIPTOR = "application/vnd.flex.node.descriptor.v1+json"
MT_PAYLOAD_IMAGE = "application/vnd.flex.payload.image.docker-archive.v1.tar"
MT_PAYLOAD_NATIVE = "application/vnd.flex.payload.native.v1.tar+gzip"
MT_SBOM = "application/spdx+json"
AT_SIGNATURE = "application/vnd.flex.signature.v1+json"
EMPTY_JSON = b"{}"


class Layout:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        (self.root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
        if not (self.root / "oci-layout").exists():
            atomic_write_json(self.root / "oci-layout", {"imageLayoutVersion": "1.0.0"})
        if not (self.root / "index.json").exists():
            atomic_write_json(self.root / "index.json",
                              {"schemaVersion": 2, "mediaType": MT_INDEX, "manifests": []})

    # ---- blobs ------------------------------------------------------------------------------
    def blob_path(self, digest: str) -> Path:
        algo, _, hexd = digest.partition(":")
        if algo != "sha256" or len(hexd) != 64 or any(c not in "0123456789abcdef" for c in hexd):
            raise ValueError(f"bad digest {digest!r}")
        return self.root / "blobs" / "sha256" / hexd

    def put_blob(self, data: bytes) -> dict:
        d = sha256_digest(data)
        p = self.blob_path(d)
        if not p.exists():
            atomic_write_bytes(p, data)
        return {"digest": d, "size": len(data)}

    def get_blob(self, digest: str) -> bytes:
        return self.blob_path(digest).read_bytes()

    def has_blob(self, digest: str) -> bool:
        try:
            return self.blob_path(digest).exists()
        except ValueError:
            return False

    # ---- manifests --------------------------------------------------------------------------
    def put_manifest(self, manifest: dict, ref_name: str | None = None) -> dict:
        data = canonical_json(manifest)
        desc = {"mediaType": manifest.get("mediaType", MT_MANIFEST), **self.put_blob(data)}
        if manifest.get("artifactType"):
            desc["artifactType"] = manifest["artifactType"]
        ann = {}
        if ref_name:
            ann["org.opencontainers.image.ref.name"] = ref_name
        if manifest.get("subject"):
            ann["dev.flex.subject"] = manifest["subject"]["digest"]
        if ann:
            desc["annotations"] = ann
        with self._lock:
            idx = read_json(self.root / "index.json")
            idx["manifests"] = [m for m in idx["manifests"] if m["digest"] != desc["digest"]]
            idx["manifests"].append(desc)
            atomic_write_json(self.root / "index.json", idx)
        return desc

    def referrers(self, digest: str) -> dict:
        idx = read_json(self.root / "index.json")
        found = [{k: v for k, v in m.items() if k != "annotations"} | {"annotations": m.get("annotations", {})}
                 for m in idx["manifests"] if m.get("annotations", {}).get("dev.flex.subject") == digest]
        return {"schemaVersion": 2, "mediaType": MT_INDEX, "manifests": found}

    def resolve(self, ref_name: str) -> dict | None:
        idx = read_json(self.root / "index.json")
        for m in reversed(idx["manifests"]):
            if m.get("annotations", {}).get("org.opencontainers.image.ref.name") == ref_name:
                return m
        return None

    def get_manifest(self, digest: str) -> dict:
        return json.loads(self.get_blob(digest))

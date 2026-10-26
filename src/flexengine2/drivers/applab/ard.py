"""App Lab release archives (.ard): the read-side checks the engine runs before installing (CP-6).

Moved from probe P4 (`flexp4/ardrelease.py`: `read_ard`, `contract_of`). The archive layout follows arduino-app-cli
(pkg/release/release.go); building archives stays in the release tooling, not in the engine.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import yaml

MT_PAYLOAD_APPLAB = "application/vnd.flex.payload.applab-release.v1.tar+gzip"
SCHEMA = 1
BOARDS = {"unoq": "arduino:zephyr:unoq", "ventunoq": "arduino:zephyr:ventunoq"}


class ReleaseError(ValueError):
    pass


def read_ard(path: Path) -> dict:
    """Read and check an archive the way arduino-app-cli does (ReadReleaseManifest + install checks)."""
    path = Path(path)
    if path.suffix != ".ard":
        raise ReleaseError(f"{path.name} is not a release archive: expected '.ard'")
    try:
        tf = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise ReleaseError(f"{path.name} is not a release archive: {e}")
    root, manifest, files = None, None, {}
    with tf:
        for m in tf.getmembers():
            parts = Path(m.name).parts
            if not parts or parts[0] in ("", ".", ".."):
                raise ReleaseError(f"{path.name} is not rooted at a single release folder")
            root = root or parts[0]
            if parts[0] != root:
                raise ReleaseError(f"{path.name} is not rooted at a single release folder")
            inner = "/".join(parts[1:])
            if m.isfile():
                files[inner] = m
                if inner == "release.yaml":
                    manifest = yaml.safe_load(tf.extractfile(m).read())
        if manifest is None:
            raise ReleaseError(f"{path.name} no release manifest, it is not a release")
        if not manifest.get("name") or not manifest.get("target"):
            raise ReleaseError("its release manifest states no name or no target")
        if manifest.get("schema", 0) > SCHEMA:
            raise ReleaseError(f"{path.name} is schema {manifest['schema']} and needs a newer cli")
        src = {k[4:]: tf.extractfile(v).read() for k, v in files.items() if k.startswith("src/") and k.endswith(
            ("flex_bridge.py", "flex_bridge.h", "contract.json", "app-meta.json", "app.yaml"))}
    return {"releaseName": root, "manifest": manifest, "files": sorted(files), "src": src}


def contract_of(info: dict) -> dict:
    """Contract hashes found in the archive's sources (static pre-install check)."""
    out = {}
    py = info["src"].get("python/flex_bridge.py", b"").decode()
    h = info["src"].get("sketch/flex_bridge.h", b"").decode()
    for line in py.splitlines():
        if line.startswith("CONTRACT_HASH = "):
            out["python"] = line.split('"')[1]
    for line in h.splitlines():
        if "CONTRACT_HASH[] = " in line:
            out["sketch"] = line.split('"')[1]
    if "flex/contract.json" in info["src"]:
        out["contract"] = json.loads(info["src"]["flex/contract.json"])["contractHash"]
    return out

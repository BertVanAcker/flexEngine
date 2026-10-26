"""App Lab release archives (.ard) and their wrapping as P2 release artefacts (probe P4).

Release archive format, as read from arduino-app-cli source (pkg/release/release.go,
internal/orchestrator/release.go and install.go, HEAD 2026-10-15):

  <name>-<label>-<board>.ard            gzip'd tar, rooted at ONE folder named like the file
    <rel>/release.yaml                  schema (1), name, release_label, icon, target, created_at, notes,
                                        libraries; written FIRST (then the two lists), so the manifest is
                                        readable without decompressing the whole archive
    <rel>/prebuild/bricks-list.yaml     bricks: [...]
    <rel>/prebuild/models-list.yaml     models: [...]
    <rel>/src/...                       the app as authored, app.yaml re-written with secrets blanked
    <rel>/prebuild/...                  python venv + resolved compose files + sketch.fw  (-> .cache/ on install)
    <rel>/data/                         only with --include-data
    <rel>/custom-ei/<model>/            Edge Impulse models the board does not hold
  modes normalised (dirs 0755, files 0644) except under prebuild/; uid/gid 0; install refuses a release
  for another board, a newer schema, or a name already installed.

build_ard() reproduces this layout from a generated app folder. It does NOT reproduce the content of the
real prebuild/ (venv built in the python-apps-base container, frozen compose files): it carries
prebuild/flex-host-mcu (the host-built sketch for the simulated board) and, when arduino-cli is available,
prebuild/sketch.fw compiled for the target. On a lab board the archive comes from the real
`arduino-app-cli app build`, and wrap() below is used unchanged.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .appgen import source_hashes

PROBE = Path(__file__).resolve().parents[1]
P2 = PROBE.parent / "p2-reconciling-engine"
if str(P2) not in sys.path:
    sys.path.insert(0, str(P2))

MT_PAYLOAD_APPLAB = "application/vnd.flex.payload.applab-release.v1.tar+gzip"
SCHEMA = 1
BOARDS = {"unoq": "arduino:zephyr:unoq", "ventunoq": "arduino:zephyr:ventunoq"}


def slug(s: str) -> str:
    """gosimple/slug.Make for ASCII input: lower case, runs of other characters -> '-'."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def release_name(app_name: str, label: str, board: str) -> str:
    return f"{slug(app_name)}-{slug(label)}-{board}" if slug(label) else f"{slug(app_name)}-{board}"


class ReleaseError(ValueError):
    pass


def build_ard(app_dir: Path, out_dir: Path, *, board: str, label: str, notes: str = "",
              prebuild: dict[str, Path] | None = None, created_at: datetime | None = None) -> Path:
    """Build `<release>.ard` from an app folder. `prebuild` maps names under prebuild/ to files."""
    if board not in BOARDS:
        raise ReleaseError(f"unknown target board {board!r}: expected one of {', '.join(BOARDS)}")
    app_yaml = yaml.safe_load((app_dir / "app.yaml").read_text())
    if not (app_dir / "python" / "main.py").exists():
        raise ReleaseError("an app needs python/main.py")
    rel = release_name(app_yaml["name"], label, board)
    created = (created_at or datetime.now(timezone.utc)).replace(microsecond=0)
    manifest = {"schema": SCHEMA, "name": app_yaml["name"], "release_label": label,
                "icon": app_yaml.get("icon", ""), "target": board,
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), **({"notes": notes} if notes else {})}
    entries: list[tuple[str, bytes | None, int]] = []   # (name, data or None for dir, mode)

    def add_dir(name):
        entries.append((f"{rel}/{name}".rstrip("/") + "/", None, 0o755))

    def add_file(name, data, mode=0o644):
        entries.append((f"{rel}/{name}", data, mode))

    entries.append((f"{rel}/", None, 0o755))
    add_file("release.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode())
    add_dir("prebuild")
    add_file("prebuild/bricks-list.yaml", b"bricks: []\n" if not app_yaml.get("bricks") else
             yaml.safe_dump({"bricks": [{"id": next(iter(b)) if isinstance(b, dict) else b}
                                        for b in app_yaml["bricks"]]}).encode())
    add_file("prebuild/models-list.yaml", b"models: []\n")
    add_dir("src")
    for p in sorted(app_dir.rglob("*")):
        relp = p.relative_to(app_dir).as_posix()
        if relp.split("/")[0] in (".cache", "data") or "__pycache__" in relp:
            continue
        if p.is_dir():
            add_dir(f"src/{relp}")
        else:
            add_file(f"src/{relp}", p.read_bytes())
    for name, src in sorted((prebuild or {}).items()):
        add_file(f"prebuild/{name}", Path(src).read_bytes(), 0o755)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{rel}.ard"
    raw = io.BytesIO()
    mtime = int(created.timestamp())
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data, mode in entries:
            ti = tarfile.TarInfo(name.rstrip("/") if data is None else name)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode = mtime, 0, 0, "", "", mode
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime) as gz:
        gz.write(raw.getvalue())
    out.write_bytes(buf.getvalue())
    return out


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


# ---- P2 release artefact ------------------------------------------------------------------------

def wrap(layout, release_key, ard: Path, *, model_dir: Path, version: str, approval: dict | None = None,
         sign: bool = True, contract_gate: bool = True, health_timeout: float = 45.0) -> dict:
    """Wrap an .ard as a signed flexNode release (P2 OCI layout): descriptor (composite app, FLEX01 Bridge
    contract hash, model-derived safety class), payload layer (the .ard), SBOM stub, signature referrer."""
    from flexp2.release import model_safety, publish_release, sign_release

    info = read_ard(ard)
    hashes = contract_of(info)
    if contract_gate:
        if not hashes.get("python") or hashes.get("python") != hashes.get("sketch") or \
                hashes.get("contract") != hashes.get("python"):
            raise ReleaseError(f"contract gate: the two sides of {info['releaseName']} disagree: {hashes}")
    contract = json.loads(info["src"]["flex/contract.json"])
    classes = model_safety(model_dir)["modules"]
    members = {**contract["mcu"]["instances"], **contract["mpu"]["instances"]}
    order = {"none": 0, "safety-relevant": 1}
    cls = max((classes.get(m["module"], {}).get("class", "none") for m in members.values()), key=order.get)
    m = info["manifest"]
    system = contract["system"]
    descriptor = {
        "apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor",
        "module": system, "moduleVersion": contract["systemVersion"], "releaseVersion": version,
        "interfaceHash": hashes.get("python"),
        "bridgeContractHash": hashes.get("python"),
        "language": "applab (python + arduino sketch)",
        "profiles": ["applab"],
        "applab": {"board": m["target"], "releaseName": info["releaseName"], "appName": m["name"],
                   "releaseLabel": m.get("release_label"), "createdAt": m.get("created_at"),
                   "prebuild": sorted(f[len("prebuild/"):] for f in info["files"] if f.startswith("prebuild/"))},
        "composite": {side: {i: {k: d[k] for k in ("module", "version", "interfaceHash")}
                             for i, d in contract[side]["instances"].items()} for side in ("mcu", "mpu")},
        "ports": [{"name": x["name"], "pattern": x["kind"], "provider": x["provider"]} for x in contract["methods"]],
        "health": {"type": "applab-handshake", "periodSeconds": 0.5, "successThreshold": 2,
                   "timeoutSeconds": health_timeout},
        "restartPolicy": {"policy": "on-failure", "maxRetries": 3},
        "safetyClass": cls, "safetyClassSource": "model (flex safety; max over the App's modules)",
        "source": {"model": str(model_dir)},
    }
    ann = {"dev.flex.profile": "applab", "dev.flex.platform": f"arduino:{m['target']}",
           "dev.flex.board": m["target"], "dev.flex.releaseName": info["releaseName"],
           "dev.flex.contractHash": hashes.get("python") or "",
           "org.opencontainers.image.title": f"{info['releaseName']}.ard"}
    sbom = {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"{system}-{version}", "documentNamespace": f"https://flex.example/spdx/{system}/{version}",
            "creationInfo": {"created": m.get("created_at"), "creators": ["Tool: flexp4-wrap (stub)"]},
            "packages": [{"SPDXID": "SPDXRef-app", "name": system, "versionInfo": version,
                          "downloadLocation": "NOASSERTION", "licenseConcluded": "NOASSERTION"},
                         {"SPDXID": "SPDXRef-bridge", "name": "Arduino_RouterBridge", "versionInfo": "0.4.3",
                          "downloadLocation": "https://github.com/arduino-libraries/Arduino_RouterBridge",
                          "licenseConcluded": "MPL-2.0"},
                         {"SPDXID": "SPDXRef-appbricks", "name": "arduino-app-bricks", "versionInfo": "board",
                          "downloadLocation": "https://github.com/arduino/app-bricks-py",
                          "licenseConcluded": "MPL-2.0"}],
            "relationships": []}
    repo = f"flexnodes/{system}"
    mdesc = publish_release(layout, repo, descriptor, [(MT_PAYLOAD_APPLAB, Path(ard), ann)], sbom)
    if sign:
        sign_release(layout, release_key, repo, mdesc, version, approval)
    return {"repository": repo, "module": system, "digest": mdesc["digest"], "size": mdesc["size"],
            "version": version, "profile": "applab", "interfaceHash": descriptor["interfaceHash"],
            "safetyClass": cls, "releaseName": info["releaseName"]}

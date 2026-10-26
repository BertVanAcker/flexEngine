"""Release tooling: flexModel module -> flexNode release bundle (OCI artifact) -> signed.

A release bundle is one OCI image manifest (artifactType application/vnd.flex.node.v1+json):

  config  : flexNode descriptor (JSON) derived from the flexModel module + FLEX01 interface hash
  layers  : per-target payloads (docker-archive image for the container profile, tar.gz for native)
            + SPDX SBOM stub
  referrer: signature manifest (subject = release manifest) carrying an Ed25519 signature over
            {repository, manifest digest, version, safety approval}; cosign "simple signing"-shaped.

The device verifies: manifest sha256 == digest in desired state, a signature by the trusted release
key over exactly that digest, each payload's sha256, and (for safety-relevant deployments) that the
signed approval names two distinct approvers (WS9 FC-S2).
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

from flexengine2.identity import crypto
from flexengine2.util import canonical_json, iso, sha256_digest
from flexengine2.artefacts.oci import (AT_NODE, AT_SIGNATURE, EMPTY_JSON, MT_DESCRIPTOR, MT_EMPTY, MT_MANIFEST,
                  MT_PAYLOAD_IMAGE, MT_PAYLOAD_NATIVE, MT_SBOM, Layout)

# This checkout (src/flexengine2/testing/release.py -> repository root) and the framework files vendored into its
# tests (tests/vectors/framework, origin and commit in SOURCES.json). Installed from a wheel, neither exists.
CHECKOUT = Path(__file__).resolve().parents[3]
VECTORS = CHECKOUT / "tests" / "vectors" / "framework"
# A modular-interoperable-framework checkout (probes, demo/, examples/models, flexmodel): $FLEX_REPO, optional. The
# cases that need the framework's code (probe stand-ins, demo stacks, `flex release`) skip without it.
FRAMEWORK = Path(os.environ["FLEX_REPO"]).resolve() if os.environ.get("FLEX_REPO") else None
REPO = FRAMEWORK or CHECKOUT
# Probe trees (read-only stand-ins, build outputs, caches): $FLEX_PROBES_DIR, default <framework>/probes. A git
# worktree has the probe sources but not their gitignored build outputs; point this at the main checkout's probes/.
PROBES = Path(os.environ.get("FLEX_PROBES_DIR") or REPO / "probes").resolve()
# Stand-in flexNode apps (container: alpine + busybox nc health endpoint; native: Python): $FLEX_TEST_APPS, else probe
# P2's apps, vendored as package data (testing/apps; SOURCES in tests/vectors/framework), so a wheel install has them.
APPS = Path(os.environ.get("FLEX_TEST_APPS") or Path(__file__).resolve().parent / "apps")


def flex_cli() -> Path:
    """The flexmodel `flex` CLI: $FLEX_CLI, the framework's venv, this interpreter's venv, PATH."""
    cands = [os.environ.get("FLEX_CLI"), FRAMEWORK / ".venv" / "bin" / "flex" if FRAMEWORK else None,
             Path(sys.prefix) / "bin" / "flex", shutil.which("flex")]
    for c in cands:
        if c and Path(c).exists() and _is_python_script(Path(c)):
            return Path(c)
    raise RuntimeError("flex CLI not found: install flexmodel (pip install -e <framework>/flexmodel) or set FLEX_CLI")


def _is_python_script(p: Path) -> bool:
    """flexmodel's `flex` is a Python console script; GNU flex (the lexer generator, often on PATH) is not."""
    try:
        with open(p, "rb") as f:
            head = f.readline(256)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in head


def _recorded(model_dir: Path, kind: str) -> dict | None:
    """`flex <kind>` output recorded next to a vendored model (flex-<kind>.recorded.json), used without flexmodel."""
    p = Path(model_dir) / f"flex-{kind}.recorded.json"
    return json.loads(p.read_text()) if p.is_file() else None


# ---- flexModel -> descriptor ---------------------------------------------------------------------

def module_hashes(model_dir: Path) -> dict:
    """FLEX01 interface hashes from the P0/P1 `flex hash` tool (read-only use of flexmodel/)."""
    try:
        flex = flex_cli()
    except RuntimeError:
        if (rec := _recorded(model_dir, "hash")) is not None:
            return rec["modules"]
        raise
    out = subprocess.run([str(flex), "hash", str(model_dir)], check=True, capture_output=True, text=True)
    return json.loads(out.stdout)["modules"]


def model_safety(*model_dirs: Path) -> dict:
    """Model-derived safety classification from `flex safety --json` (flexmodel/safety.py, S4): the one rule
    shared by `flex check`, this release tool, the flexCloud stub and the P6 gateway.
    Returns {"modules": {name: {"class", "why", ...}}, "interferencePaths": [...]}."""
    try:
        flex = flex_cli()
    except RuntimeError:
        if len(model_dirs) == 1 and (rec := _recorded(model_dirs[0], "safety")) is not None:
            return rec
        raise
    out = subprocess.run([str(flex), "safety", *map(str, model_dirs), "--json"], check=True, capture_output=True,
                         text=True)
    return json.loads(out.stdout)


def safety_class_of(model_dir: Path, module: str, classes: dict | None = None) -> str:
    classes = classes if classes is not None else model_safety(model_dir)
    return classes["modules"].get(module, {}).get("class", "none")


class SafetyClassMismatch(ValueError):
    pass


def load_module(model_dir: Path, name: str) -> dict:
    for p in sorted((model_dir / "modules").glob("*.yaml")):
        doc = yaml.safe_load(p.read_text())
        if doc.get("kind") == "Module" and doc["metadata"]["name"] == name:
            return doc
    raise KeyError(name)


def descriptor_from_module(model_dir: Path, module: str, release_version: str, *, profile: str,
                           health: dict, restart: dict, safety_class: str | None = None,
                           hashes: dict | None = None, classes: dict | None = None,
                           run_options: dict | None = None) -> dict:
    """`safety_class` is taken from the model (S4). A caller may still pass the class it expects; a declaration
    that differs from the model-derived class is refused (P6 report section 10.3)."""
    doc = load_module(model_dir, module)
    derived = safety_class_of(model_dir, module, classes)
    if safety_class is not None and safety_class != derived:
        raise SafetyClassMismatch(f"{module}: declared safety class {safety_class!r} differs from the "
                                  f"model-derived class {derived!r}")
    hashes = hashes if hashes is not None else module_hashes(model_dir)
    mver = doc["metadata"]["version"]
    spec = doc["spec"]
    return {
        "apiVersion": "flex.dev/v1alpha1",
        "kind": "FlexNodeDescriptor",
        "module": module,
        "moduleVersion": mver,
        "releaseVersion": release_version,
        "interfaceHash": hashes.get(f"{module}@{mver}"),
        "language": spec.get("language"),
        "ports": [{k: p.get(k) for k in ("name", "direction", "pattern", "type", "request", "response")
                   if p.get(k) is not None} for p in spec.get("ports", [])],
        "parameters": spec.get("parameters", []),
        "lifecycle": spec.get("lifecycle", {}),
        "resources": spec.get("resources", {}),
        "requires": spec.get("requires", {}),
        "profiles": [profile],
        "health": health,
        "restartPolicy": restart,
        "safetyClass": derived,
        "safetyClassSource": "model (flex safety)",
        **({"runOptions": run_options} if run_options else {}),
        "source": {"model": str(model_dir.relative_to(REPO)) if model_dir.is_relative_to(REPO) else str(model_dir)},
    }


# ---- payloads -------------------------------------------------------------------------------------

def build_container_payload(work: Path, app: str, version: str, *, fail_health: bool = False,
                            runtime: str = "docker", image: str | None = None) -> tuple[Path, str]:
    """docker build from apps/container (or re-tag an existing local `image`, e.g. the real pg_node image),
    then docker save -> image archive tar. Returns (tar, image digest)."""
    tag = f"flexnode/{app}:{version}"
    tar = work / "payloads" / f"{app}-{version}.image.tar"
    tar.parent.mkdir(parents=True, exist_ok=True)
    if image is not None:
        subprocess.run([runtime, "tag", image, tag], check=True, capture_output=True, text=True)
        subprocess.run([runtime, "save", "-o", str(tar), tag], check=True, capture_output=True, text=True)
    elif not tar.exists():
        subprocess.run([runtime, "build", "-q", "-t", tag,
                        "--build-arg", f"FLEX_APP={app}", "--build-arg", f"FLEX_VERSION={version}",
                        "--build-arg", f"FLEX_FAIL_HEALTH={1 if fail_health else 0}",
                        str(APPS / "container")], check=True, capture_output=True, text=True)
        subprocess.run([runtime, "save", "-o", str(tar), tag], check=True, capture_output=True, text=True)
    with tarfile.open(tar) as tf:  # digest of the image manifest/index inside the archive
        idx = json.load(tf.extractfile("index.json"))
    return tar, idx["manifests"][0]["digest"]


def build_native_payload(app: str, version: str, *, fail_health: bool = False,
                         native_dir: Path | None = None) -> bytes:
    """Deterministic tar.gz: app.py + release.json + unit.json (systemd-like unit description). With
    `native_dir`, every regular file of that directory is packed instead (it must contain app.py, or a
    unit.json whose ExecStart names its entry point)."""
    files = {
        "app.py": (APPS / "native" / "app.py").read_bytes(),
        "release.json": canonical_json({"app": app, "version": version, "fail_health": fail_health}),
        "unit.json": canonical_json({"ExecStart": ["{python}", "app.py"], "Restart": "on-failure",
                                     "RestartSec": 0.5, "StartLimitBurst": 5}),
    }
    if native_dir is not None:
        base = Path(native_dir)
        own = {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}
        if "app.py" not in own and "unit.json" not in own:
            raise ValueError(f"{base}: needs app.py or unit.json")
        files = {"release.json": files["release.json"], "unit.json": files["unit.json"], **own}
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


def sbom_stub(app: str, version: str, profile: str, base: str = "alpine:3.20") -> dict:
    pkgs = [{"SPDXID": "SPDXRef-flexnode", "name": app, "versionInfo": version, "downloadLocation": "NOASSERTION",
             "supplier": "Organization: flex* project (probe)", "licenseConcluded": "NOASSERTION"}]
    if profile == "container":
        pkgs.append({"SPDXID": "SPDXRef-base", "name": base.split(":")[0], "versionInfo": base.split(":")[1],
                     "downloadLocation": "NOASSERTION", "licenseConcluded": "NOASSERTION"})
    else:
        pkgs.append({"SPDXID": "SPDXRef-python", "name": "cpython", "versionInfo": "3.x",
                     "downloadLocation": "NOASSERTION", "licenseConcluded": "PSF-2.0"})
    return {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"{app}-{version}", "documentNamespace": f"https://flex.example/spdx/{app}/{version}",
            "creationInfo": {"created": "2026-10-15T00:00:00Z", "creators": ["Tool: flexp2-release (stub)"]},
            "packages": pkgs,
            "relationships": [{"spdxElementId": "SPDXRef-flexnode", "relationshipType": "DEPENDS_ON",
                               "relatedSpdxElement": pkgs[1]["SPDXID"]}]}


# ---- bundle + signature -------------------------------------------------------------------------

def publish_release(layout: Layout, repo: str, descriptor: dict, payloads: list[tuple[str, bytes | Path, dict]],
                    sbom: dict) -> dict:
    """Write the release manifest + blobs into the layout. Returns the manifest descriptor."""
    cfg = layout.put_blob(canonical_json(descriptor))
    layers = []
    for media_type, data, ann in payloads:
        if isinstance(data, Path):
            data = data.read_bytes()
        layers.append({"mediaType": media_type, **layout.put_blob(data), "annotations": ann})
    sb = layout.put_blob(canonical_json(sbom))
    layers.append({"mediaType": MT_SBOM, **sb, "annotations": {"dev.flex.role": "sbom"}})
    manifest = {
        "schemaVersion": 2, "mediaType": MT_MANIFEST, "artifactType": AT_NODE,
        "config": {"mediaType": MT_DESCRIPTOR, **cfg},
        "layers": layers,
        "annotations": {"org.opencontainers.image.title": descriptor["module"],
                        "org.opencontainers.image.version": descriptor["releaseVersion"],
                        "dev.flex.interfaceHash": descriptor.get("interfaceHash") or "",
                        "dev.flex.safetyClass": descriptor.get("safetyClass", "none")},
    }
    return layout.put_manifest(manifest, ref_name=f"{repo}:{descriptor['releaseVersion']}")


def sign_release(layout: Layout, key, repo: str, subject: dict, version: str,
                 approval: dict | None = None) -> dict:
    payload = {"critical": {"type": "flex release signature v1", "identity": {"repository": repo},
                            "image": {"manifest-digest": subject["digest"]}},
               "optional": {"version": version, "signedAt": iso(),
                            "safetyApproval": approval or {"approved": False, "approvers": []}}}
    pb = canonical_json(payload)
    sig_doc = {"payload": crypto.b64(pb), "signature": crypto.sign(key, pb),
               "keyid": crypto.key_id(crypto.public_b64(key))}
    layer = layout.put_blob(canonical_json(sig_doc))
    empty = layout.put_blob(EMPTY_JSON)
    manifest = {"schemaVersion": 2, "mediaType": MT_MANIFEST, "artifactType": AT_SIGNATURE,
                "config": {"mediaType": MT_EMPTY, **empty},
                "layers": [{"mediaType": AT_SIGNATURE, **layer}],
                "subject": {"mediaType": subject["mediaType"], "digest": subject["digest"], "size": subject["size"]}}
    return layout.put_manifest(manifest)


def make_release(layout: Layout, work: Path, release_key, *, model_dir: Path, module: str, version: str,
                 profile: str, fail_health: bool = False, safety_class: str | None = None,
                 approval: dict | None = None, sign: bool = True, hashes: dict | None = None,
                 runtime: str = "docker", classes: dict | None = None, run_options: dict | None = None,
                 image: str | None = None, native_dir: Path | None = None) -> dict:
    """End to end: descriptor + payload + SBOM -> OCI manifest -> signature. Returns info for desired state."""
    repo = f"flexnodes/{module}"
    if profile == "container":
        health = {"type": "http", "port": 8080, "path": "/healthz", "periodSeconds": 0.25,
                  "successThreshold": 2, "timeoutSeconds": 8}
        restart = {"policy": "on-failure", "maxRetries": 5}
        tar, image_id = build_container_payload(work, module, version, fail_health=fail_health, runtime=runtime,
                                                image=image)
        payloads = [(MT_PAYLOAD_IMAGE, tar, {"dev.flex.profile": "container", "dev.flex.platform": "linux/amd64",
                                             "dev.flex.imageDigest": image_id,
                                             "dev.flex.imageRef": f"docker.io/flexnode/{module}@{image_id}",
                                             "dev.flex.imageTag": f"flexnode/{module}:{version}"})]
    else:
        health = {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.25,
                  "successThreshold": 2, "timeoutSeconds": 8}
        restart = {"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5}
        payloads = [(MT_PAYLOAD_NATIVE, build_native_payload(module, version, fail_health=fail_health, native_dir=native_dir),
                     {"dev.flex.profile": "native", "dev.flex.platform": "linux/any+python3"})]
    desc = descriptor_from_module(model_dir, module, version, profile=profile, health=health, restart=restart,
                                  safety_class=safety_class, hashes=hashes, classes=classes, run_options=run_options)
    mdesc = publish_release(layout, repo, desc, payloads, sbom_stub(module, version, profile))
    if sign:
        sign_release(layout, release_key, repo, mdesc, version, approval)
    return {"repository": repo, "digest": mdesc["digest"], "size": mdesc["size"], "version": version,
            "profile": profile, "interfaceHash": desc["interfaceHash"], "safetyClass": desc["safetyClass"]}

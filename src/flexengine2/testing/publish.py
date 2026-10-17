"""Publish a real release bundle to a running flexCloud stub (S4; lab use).

    python -m flexp2.publish --cloud http://10.0.0.2:8700 --admin-token-file secrets/admin-token \
        --model ../../examples/models/uc1 --module perception_guard --version 2.1.0 \
        --profile container --image pg_node:lab --release-key secrets/release.key \
        [--approver alice --approver bob] [--device /dev/dri --gpus all] [--dry-run]

Builds the same artefact as the tests (descriptor from the flexModel module with the FLEX01 interface hash
and the model-derived safety class, payload, SBOM stub, Ed25519 signature referrer) into a temporary OCI
layout, then uploads it to `POST /api/v1/admin/releases`. The stub refuses releases whose safety class
differs from its model. Payloads: `--image` re-tags and saves an existing local container image (e.g. the
real pg_node image), `--native-dir` packs a directory (app.py or unit.json); without either, the probe's
stub app is used. `--approver` (repeatable) puts a two-person safety approval into the signed payload;
it is required for safety-relevant modules (the device refuses them otherwise).
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from flexengine2.identity import crypto
from flexengine2.artefacts.oci import Layout
from flexengine2.testing.release import make_release, model_safety, module_hashes


def layout_tar(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(root)))
    return buf.getvalue()


def upload(cloud: str, token: str, data: bytes, user: str) -> dict:
    u = urlsplit(cloud)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    c = conn_cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=600)
    c.request("POST", "/api/v1/admin/releases", body=data,
              headers={"Authorization": f"Bearer {token}", "X-Flex-User": user, "Content-Type": "application/x-tar",
                       "Content-Length": str(len(data)), "Connection": "close"})
    r = c.getresponse()
    body = r.read()
    c.close()
    out = json.loads(body) if body else {}
    if r.status >= 300:
        raise SystemExit(f"upload failed: HTTP {r.status} {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cloud", help="flexCloud stub base URL (omit with --dry-run)")
    ap.add_argument("--admin-token-file")
    ap.add_argument("--user", default="release-tool", help="actor recorded in the stub's audit log")
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--module", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--profile", choices=["container", "native"], default="container")
    ap.add_argument("--image", help="existing local container image to release (container profile)")
    ap.add_argument("--native-dir", type=Path, help="directory to release (native profile)")
    ap.add_argument("--runtime", default="docker", help="docker or podman (for --image)")
    ap.add_argument("--release-key", required=True, type=Path, help="Ed25519 release key (created if missing)")
    ap.add_argument("--approver", action="append", default=[], help="safety approver (repeat for two-person rule)")
    ap.add_argument("--device", action="append", default=[], help="runOptions.devices (must be on the device's "
                                                                  "allow-list, e.g. /dev/dri, nvidia.com/gpu=all)")
    ap.add_argument("--gpus", help="runOptions.gpus (e.g. all)")
    ap.add_argument("--work", type=Path, help="work dir for payload caches (default: temporary)")
    ap.add_argument("--dry-run", action="store_true", help="build and print, do not upload")
    a = ap.parse_args(argv)

    classes = model_safety(a.model)
    cls = classes["modules"].get(a.module, {}).get("class", "none")
    if cls != "none" and len(set(a.approver)) < 2:
        print(f"warning: {a.module} is {cls} (model); without two --approver the device will refuse it",
              file=sys.stderr)
    run_options = {}
    if a.device:
        run_options["devices"] = a.device
    if a.gpus:
        run_options["gpus"] = a.gpus
    approval = {"approved": True, "approvers": a.approver, "role": "safety-engineer"} if a.approver else None
    key = crypto.load_or_create(a.release_key)
    with tempfile.TemporaryDirectory() as tmp:
        work = a.work or Path(tmp) / "work"
        layout = Layout(Path(tmp) / "oci")
        info = make_release(layout, work, key, model_dir=a.model.resolve(), module=a.module, version=a.version,
                            profile=a.profile, approval=approval, hashes=module_hashes(a.model), classes=classes,
                            run_options=run_options or None, runtime=a.runtime, image=a.image,
                            native_dir=a.native_dir)
        print(json.dumps({"release": info, "releaseKeyId": crypto.key_id(crypto.public_b64(key)),
                          "releasePublicKey": crypto.public_b64(key)}, indent=1))
        if a.dry_run:
            return 0
        if not (a.cloud and a.admin_token_file):
            raise SystemExit("--cloud and --admin-token-file are required unless --dry-run")
        token = Path(a.admin_token_file).read_text().strip()
        print(json.dumps(upload(a.cloud, token, layout_tar(layout.root), a.user), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

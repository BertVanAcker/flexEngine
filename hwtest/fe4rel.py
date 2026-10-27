"""Release builders for EV-HW-12 (FE-4: runtimes, run state, on-device build), used by deskctl.py's build kinds.

Every builder writes a signed release into an OCI layout (the same shape `testing.release.make_release` writes) and
returns {digest, interfaceHash}:

  compose_release         runtime docker-compose: a compose.yaml plus the stand-in app's image (`docker save` of
                          testing/apps/container, built on this PC) under images/; the service's healthcheck is
                          the image's /app/health.sh
  source_release          on-device build: only a source payload (build.sh copies the native stand-in app into
                          out/, or exits 2: FE-BLD-001) and a descriptor `build` spec
  fake_container_release  simulated desk only: an image archive of the contract suite's fake runtime (fakeoci,
                          tests/contract/spi_harness/fakes) whose container answers /healthz on `port`
  fake_compose_release    simulated desk only: the same as a compose payload for fakecompose

The fake kinds exist because the dry run has no dockerd: the engine drives the contract suite's fake docker /
compose CLIs, so the run states, ordering, health gates and events are the engine's own, the containers are not.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

from flexengine2.artefacts.oci import MT_PAYLOAD_COMPOSE, MT_PAYLOAD_IMAGE, MT_PAYLOAD_SOURCE, Layout
from flexengine2.testing.release import (APPS, build_container_payload, descriptor_from_module, module_hashes,
                                         model_safety, publish_release, sbom_stub, sign_release)
from flexengine2.util import canonical_json

CONTAINER_RESTART = {"policy": "on-failure", "maxRetries": 5}
NATIVE_RESTART = {"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5}


def targz(files: dict[str, bytes]) -> bytes:
    """Deterministic tar.gz (sorted names, mtime 0; *.sh executable)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.uid, ti.gid = len(files[name]), 0, 0, 0
            ti.mode = 0o755 if name.endswith(".sh") else 0o644
            tf.addfile(ti, io.BytesIO(files[name]))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _http_health(port) -> dict:
    return {"type": "http", "port": port, "path": "/healthz", "periodSeconds": 0.25, "successThreshold": 2,
            "timeoutSeconds": 8}


def _publish(layout: Layout, key, model: Path, module: str, version: str, profile: str, health: dict,
             restart: dict, payloads: list, approval: dict | None, sign: bool = True, extra: dict | None = None,
             run_options: dict | None = None) -> dict:
    desc = descriptor_from_module(model, module, version, profile=profile, health=health, restart=restart,
                                  hashes=module_hashes(model), classes=model_safety(model), run_options=run_options)
    desc.update(extra or {})
    repo = f"flexnodes/{module}"
    mdesc = publish_release(layout, repo, desc, payloads, sbom_stub(module, version, profile))
    if sign:
        sign_release(layout, key, repo, mdesc, version, approval)
    return {"digest": mdesc["digest"], "interfaceHash": desc["interfaceHash"]}


# ---- desk (real runtimes) -----------------------------------------------------------------------------------------
def compose_release(layout: Layout, key, work: Path, *, model: Path, module: str, version: str,
                    fail_health: bool = False, runtime: str = "docker", approval: dict | None = None) -> dict:
    """A compose project of one service running the stand-in app image (built and saved on this PC)."""
    tar, _image_id = build_container_payload(work, module, f"{version}-compose", fail_health=fail_health,
                                             runtime=runtime)
    tag = f"flexnode/{module}:{version}-compose"
    compose = (f"services:\n  app:\n    image: {tag}\n    restart: on-failure:5\n"
               f"    healthcheck:\n      test: [\"CMD\", \"/app/health.sh\"]\n      interval: 2s\n"
               f"      timeout: 2s\n      retries: 3\n").encode()
    data = targz({"compose.yaml": compose, "images/app.tar": Path(tar).read_bytes()})
    ann = {"dev.flex.profile": "container", "dev.flex.platform": "linux/amd64"}
    return _publish(layout, key, model, module, version, "container", _http_health(8080), CONTAINER_RESTART,
                    [(MT_PAYLOAD_COMPOSE, data, ann)], approval)


def source_release(layout: Layout, key, *, model: Path, module: str, version: str, fail: bool = False,
                   approval: dict | None = None) -> dict:
    """A native release with only a source payload: the engine builds it on the device (x-flex-build)."""
    script = (b"echo compiling\necho flex-progress 0.5\necho 'error: missing dependency' >&2\nexit 2\n" if fail else
              b"echo compiling\necho flex-progress 0.5\nmkdir -p out\ncp app.py release.json out/\necho done\n")
    src = targz({"build.sh": script, "app.py": (APPS / "native" / "app.py").read_bytes(),
                 "release.json": canonical_json({"app": module, "version": version, "fail_health": False})})
    build = {"build": {"command": ["sh", "build.sh"], "output": "out", "timeoutSeconds": 120}}
    return _publish(layout, key, model, module, version, "native", _http_health("${FLEX_HEALTH_PORT}"),
                    NATIVE_RESTART, [(MT_PAYLOAD_SOURCE, src, {"dev.flex.profile": "native"})], approval, extra=build)


# ---- simulated desk (fake runtimes of the contract suite) ---------------------------------------------------------
def _fake_image(module: str, version: str, port: int, fail_health: bool) -> tuple[str, bytes]:
    img = {"port": int(port), "ready": not fail_health}
    digest = "sha256:" + hashlib.sha256(json.dumps([module, version, img], sort_keys=True).encode()).hexdigest()
    ref = f"docker.io/flexnode/{module}@{digest}"
    return ref, json.dumps({"fakeoci": 1, "images": {ref: img}}).encode()


def fake_container_release(layout: Layout, key, *, model: Path, module: str, version: str, port: int,
                           fail_health: bool = False, approval: dict | None = None) -> dict:
    ref, archive = _fake_image(module, version, port, fail_health)
    ann = {"dev.flex.profile": "container", "dev.flex.platform": "linux/amd64",
           "dev.flex.imageDigest": ref.rsplit("@", 1)[1], "dev.flex.imageRef": ref,
           "dev.flex.imageTag": f"flexnode/{module}:{version}"}
    return _publish(layout, key, model, module, version, "container", _http_health(int(port)), CONTAINER_RESTART,
                    [(MT_PAYLOAD_IMAGE, archive, ann)], approval)


def fake_compose_release(layout: Layout, key, *, model: Path, module: str, version: str, port: int,
                         fail_health: bool = False, approval: dict | None = None) -> dict:
    ref, archive = _fake_image(module, version, port, fail_health)
    compose = (f"services:\n  app:\n    image: {ref}\n    restart: \"on-failure:5\"\n"
               f"    healthcheck:\n      test: [\"CMD\", \"true\"]\n").encode()
    data = targz({"compose.yaml": compose, "images/app.tar": archive})
    ann = {"dev.flex.profile": "container", "dev.flex.platform": "linux/amd64"}
    return _publish(layout, key, model, module, version, "container", _http_health(int(port)), CONTAINER_RESTART,
                    [(MT_PAYLOAD_COMPOSE, data, ann)], approval)

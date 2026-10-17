"""Test releases for the os-rauc driver (EV-17): tiny RAUC bundles wrapped as signed flexNode releases.

  build_real_bundle()   a real, CMS-signed RAUC 1.11 bundle, built with `rauc bundle` in an Ubuntu 24.04 container
                        (test signing cert + key made with openssl once per pki dir; compatible "flex-test";
                        format plain: the nightly container has loop devices but no dm-verity)
  build_fake_bundle()   the push tier's stand-in for tests/contract/spi_harness/fakes/fakerauc.py
  make_rauc_release()   descriptor + bundle layer (application/vnd.flex.payload.os-rauc.v1+raucb, annotations
                        dev.flex.profile=os-rauc, dev.flex.compatible) + SBOM stub -> OCI manifest -> Ed25519
                        release signature, as testing/release.py does for the other profiles

The single image of both kinds starts with the boot simulator's header line `FLEXSIM bootable=<0|1> healthy=<0|1>`
(tests/contract/spi_harness/fakes/bootsim.py): bootable=0 is "a rootfs without the engine" (the slot never comes
up, the bootloader falls back), healthy=0 an image whose device-side health check fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from flexengine2.artefacts.oci import Layout
from flexengine2.drivers.os_rauc import ANN_COMPATIBLE, MT_PAYLOAD_RAUC
from flexengine2.testing.release import publish_release, sbom_stub, sign_release

IMAGE = "flexengine-rauctest:ubuntu24.04"
DOCKERFILE = ("FROM ubuntu:24.04\n"
              "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
              "rauc rauc-service openssl squashfs-tools dbus python3 ca-certificates procps "
              "&& rm -rf /var/lib/apt/lists/*\n")
COMPATIBLE = "flex-test"
FAKE_MAGIC = b"FAKERAUCB1\n"
FAKE_SIGNER = "flex-test-signer"


def image_text(version: str, *, bootable: bool = True, healthy: bool = True, size: int = 0) -> bytes:
    head = f"FLEXSIM bootable={int(bootable)} healthy={int(healthy)} version={version}\n".encode()
    return head + b"\0" * max(0, size - len(head))


# ---- push tier ---------------------------------------------------------------------------------------------------
def build_fake_bundle(out: Path, version: str, *, compatible: str = COMPATIBLE, signer: str = FAKE_SIGNER,
                      bootable: bool = True, healthy: bool = True) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"compatible": compatible, "version": version, "signer": signer}, sort_keys=True).encode()
    out.write_bytes(FAKE_MAGIC + meta + b"\n" + image_text(version, bootable=bootable, healthy=healthy))
    return out


# ---- nightly tier (real rauc in a container) ----------------------------------------------------------------------
def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_image(image: str = IMAGE) -> str:
    """Build the Ubuntu 24.04 + rauc 1.11.3 (+ rauc-service, dbus, openssl, squashfs-tools) test image once."""
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        subprocess.run(["docker", "build", "-q", "-t", image, "-"], input=DOCKERFILE.encode(), check=True,
                       capture_output=True, timeout=1200)
    return image


def _in_container(work: Path, script: str, image: str = IMAGE, privileged: bool = False) -> str:
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    sp = work / f".run-{os.getpid()}.sh"
    sp.write_text(script)
    cmd = ["docker", "run", "--rm", "-v", f"{work}:{work}"] + (["--privileged"] if privileged else []) + \
          [image, "sh", "-e", str(sp)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    sp.unlink(missing_ok=True)
    if p.returncode != 0:
        raise RuntimeError(f"container step failed (exit {p.returncode}): {p.stderr.strip()[-2000:]}")
    return p.stdout


def make_pki(pki: Path, cn: str = "flex-test signer", image: str = IMAGE) -> dict:
    """Self-signed signing cert + key (RSA 2048) for `rauc bundle`; cert.pem doubles as the device keyring."""
    pki = Path(pki).resolve()
    cert, key = pki / "cert.pem", pki / "key.pem"
    if not (cert.exists() and key.exists()):
        ensure_image(image)
        _in_container(pki, f'cd {pki}\nopenssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem '
                           f'-days 3650 -subj "/O=flex-test/CN={cn}" 2>/dev/null\nchmod 644 key.pem cert.pem\n', image)
    return {"cert": cert, "key": key, "keyring": cert}


def build_real_bundle(out: Path, version: str, pki: dict, *, compatible: str = COMPATIBLE, bootable: bool = True,
                      healthy: bool = True, image: str = IMAGE) -> Path:
    out = Path(out).resolve()
    if out.exists():
        return out
    ensure_image(image)
    content = out.parent / f".content-{out.stem}"
    shutil.rmtree(content, ignore_errors=True)
    content.mkdir(parents=True)
    (content / "rootfs.img").write_bytes(image_text(version, bootable=bootable, healthy=healthy, size=4096))
    (content / "manifest.raucm").write_text(f"[update]\ncompatible={compatible}\nversion={version}\n\n"
                                            "[bundle]\nformat=plain\n\n[image.rootfs]\nfilename=rootfs.img\n")
    cert, key = Path(pki["cert"]).resolve(), Path(pki["key"]).resolve()
    work = Path(os.path.commonpath([out.parent, cert.parent]))
    _in_container(work, f"rauc bundle --cert={cert} --key={key} {content} {out} >/dev/null 2>&1\nchmod 644 {out}\n",
                  image)
    shutil.rmtree(content, ignore_errors=True)
    return out


# ---- flex release ---------------------------------------------------------------------------------------------------
def descriptor(module: str, version: str, health: dict | None = None) -> dict:
    return {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module,
            "moduleVersion": version, "releaseVersion": version, "interfaceHash": None,
            "profiles": ["os-rauc"],
            "health": health or {"type": "os-rauc", "periodSeconds": 0.25, "successThreshold": 2,
                                 "timeoutSeconds": 20},
            "safetyClass": "none", "safetyClassSource": "release tool (OS image, no model module)"}


def make_rauc_release(layout: Layout, release_key, bundle: Path, *, module: str = "os_image", version: str,
                      compatible: str = COMPATIBLE, health: dict | None = None, sign: bool = True,
                      extra_annotations: dict | None = None) -> dict:
    """Wrap a RAUC bundle as a signed flexNode release. Returns the info item a desired state needs."""
    repo = f"flexnodes/{module}"
    ann = {"dev.flex.profile": "os-rauc", ANN_COMPATIBLE: compatible, **(extra_annotations or {})}
    desc = descriptor(module, version, health)
    mdesc = publish_release(layout, repo, desc, [(MT_PAYLOAD_RAUC, Path(bundle), ann)],
                            sbom_stub(module, version, "os-rauc"))
    if sign:
        sign_release(layout, release_key, repo, mdesc, version)
    return {"repository": repo, "digest": mdesc["digest"], "size": mdesc["size"], "version": version,
            "profile": "os-rauc", "interfaceHash": None, "safetyClass": "none"}

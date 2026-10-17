"""Test support for the MCU suites: MCU releases in the flexEngineV2 OCI release format (port of probe P3's
`flexp3/release_mcu.py` onto `flexengine2.testing.release`, same descriptor, layers and health gates).

  module release   : config = flexNode descriptor (+ x-flex-mcu: the FLXM header facts),
                     layer  = FLXM container (application/vnd.flex.payload.mcu-module.v1+flxm),
                     SBOM stub, Ed25519 signature referrer
  firmware release : layer = MCUboot-signed base image (application/vnd.flex.payload.mcu-firmware.v1+mcuboot)

The FLXM containers and base images themselves come from the P3 build (`probes/p3-mcu-hotload/build`, read-only).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from flexengine2.artefacts.oci import Layout
from flexengine2.drivers.mcu import flxm
from flexengine2.testing.release import publish_release, sbom_stub, sign_release

MT_MCU_MODULE = "application/vnd.flex.payload.mcu-module.v1+flxm"
MT_MCU_FIRMWARE = "application/vnd.flex.payload.mcu-firmware.v1+mcuboot"
# 2 probes at 0.1 s (one SMP status round trip each); faults end the gate early (fail-fast); timeout = confirm window
MODULE_HEALTH = {"type": "mcu-heartbeat", "periodSeconds": 0.1, "successThreshold": 2, "timeoutSeconds": 4}
FIRMWARE_HEALTH = {"type": "mcu-base", "periodSeconds": 0.3, "successThreshold": 3, "timeoutSeconds": 8}


def module_health(test_window_ms: int) -> dict:
    """Health gate of a module release: soak = the module's confirm window, timeout = window + 2 s (4 s without)."""
    h = dict(MODULE_HEALTH)
    if test_window_ms:
        h["minSeconds"] = round(test_window_ms / 1000.0, 3)
        h["timeoutSeconds"] = round(test_window_ms / 1000.0 + 2.0, 3)
    return h


def make_module_release(layout: Layout, key, flxm_path: Path, gen_meta: dict, version: str, *, sign: bool = True,
                        descriptor_hash: str | None = None) -> dict:
    data = Path(flxm_path).read_bytes()
    hdr = flxm.parse(data).header
    module = gen_meta["module"]
    desc = {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module,
            "moduleVersion": gen_meta["moduleVersion"], "releaseVersion": version,
            "interfaceHash": descriptor_hash or gen_meta["interfaceHash"], "language": gen_meta["language"],
            "ports": gen_meta["ports"], "parameters": gen_meta["parameters"], "profiles": ["mcu-module"],
            "requires": {"target_classes": ["T4"], "features": gen_meta["caps"]},
            "health": module_health(hdr.test_window_ms), "restartPolicy": {}, "safetyClass": "none",
            "x-flex-mcu": hdr.to_dict()}
    ann = {"dev.flex.profile": "mcu-module", "dev.flex.arch": desc["x-flex-mcu"]["arch"],
           "dev.flex.floatAbi": desc["x-flex-mcu"]["floatAbi"], "dev.flex.moduleAbi": desc["x-flex-mcu"]["abi"]}
    repo = f"flexnodes/{module}"
    sb = sbom_stub(module, version, "native")
    sb["packages"][1] = {"SPDXID": "SPDXRef-python", "name": "flexmcu-module-abi",
                         "versionInfo": desc["x-flex-mcu"]["abi"], "downloadLocation": "NOASSERTION",
                         "licenseConcluded": "NOASSERTION"}
    m = publish_release(layout, repo, desc, [(MT_MCU_MODULE, data, ann)], sb)
    if sign:
        sign_release(layout, key, repo, m, version)
    return {"repository": repo, "digest": m["digest"], "version": version, "interfaceHash": desc["interfaceHash"]}


def make_firmware_release(layout: Layout, key, image: Path, version: str, *, sign: bool = True) -> dict:
    module = "flexmcu_base"
    desc = {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module,
            "moduleVersion": version, "releaseVersion": version, "interfaceHash": None, "language": "c",
            "ports": [], "parameters": [], "profiles": ["mcu-firmware"], "health": FIRMWARE_HEALTH,
            "restartPolicy": {}, "safetyClass": "none",
            "x-flex-mcu": {"bootloader": "mcuboot", "board": "mps2/an385", "moduleAbi": "1.0"}}
    repo = f"flexnodes/{module}"
    m = publish_release(layout, repo, desc, [(MT_MCU_FIRMWARE, Path(image).read_bytes(),
                                              {"dev.flex.profile": "mcu-firmware"})],
                        sbom_stub(module, version, "native"))
    if sign:
        sign_release(layout, key, repo, m, version)
    return {"repository": repo, "digest": m["digest"], "version": version}


def gen_meta(gen_dir: Path) -> dict:
    return json.loads((Path(gen_dir) / "gen" / "module.json").read_text())


def tamper_payload(layout: Layout, manifest_digest: str, media_suffix: str = "+flxm", offset: int = 400) -> bool:
    """Flip one byte of a release's payload blob after signing (manifest + signature stay valid)."""
    man = json.loads(layout.blob_path(manifest_digest).read_bytes())
    done = False
    for layer in man["layers"]:
        if layer["mediaType"].endswith(media_suffix):
            p = layout.blob_path(layer["digest"])
            b = bytearray(p.read_bytes())
            b[offset] ^= 0x01
            p.chmod(0o644)
            p.write_bytes(bytes(b))
            done = True
    return done


def region_sha(flash: Path, start: int, size: int) -> str:
    with open(flash, "rb") as f:
        f.seek(start)
        return hashlib.sha256(f.read(size)).hexdigest()

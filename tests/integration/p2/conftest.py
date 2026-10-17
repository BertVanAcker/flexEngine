"""P2 lab fixtures (port of probes/p2-reconciling-engine/tests/conftest.py): one shared OCI layout with all releases,
a fresh cloud stub per test, engines as real subprocesses (`python -m flexengine2.testing.single`), real
Docker/Podman/Ankaios underneath."""

from __future__ import annotations

import json
import shutil
import time
import uuid

import pytest

from flexengine2.artefacts.oci import Layout
from flexengine2.identity import crypto
from flexengine2.testing.netshim import NetShim
from flexengine2.testing.release import make_release, model_safety, module_hashes
from flexengine2.util import read_json

from .p2lab import METRICS, MODEL, PROBE, RESULTS, WORK, Cloud, EngineProc, metric

METRICS_FILE = RESULTS / "metrics-p2-port.json"


def pytest_sessionfinish(session, exitstatus):
    if METRICS:
        RESULTS.mkdir(exist_ok=True)
        old = read_json(METRICS_FILE, {})
        old.update(METRICS)
        METRICS_FILE.write_text(json.dumps(old, indent=2, sort_keys=True) + "\n")


# ---- releases --------------------------------------------------------------------------------

# Safety class is NOT declared here (S4): the release tool derives it from the model. In UC1, perception_guard and
# gige_camera feed the safety gateway, so their releases are safety-relevant and carry a signed two-person approval
# (FC-S2); 2.0.1 has only one approver and is refused on the device.
TWO = ["alice", "bob"]
RELEASES = [
    # (module, version, profile, fail_health, sign, key, approvers)
    ("perception_guard", "1.0.0", "container", False, True, "release", TWO),
    ("perception_guard", "1.1.0", "container", False, True, "release", TWO),
    ("perception_guard", "1.2.0", "container", True, True, "release", TWO),      # fails health
    ("perception_guard", "1.3.0", "container", False, True, "release", TWO),     # gets tampered
    ("perception_guard", "1.4.0", "container", False, False, None, TWO),         # unsigned
    ("perception_guard", "1.5.0", "container", False, True, "rogue", TWO),       # wrong key
    ("perception_guard", "2.0.0", "container", False, True, "release", TWO),
    ("perception_guard", "2.0.1", "container", False, True, "release", ["alice"]),  # one approver only
    ("detection_logger", "1.0.0", "native", False, True, "release", None),
    ("detection_logger", "1.1.0", "native", False, True, "release", None),
    ("detection_logger", "1.2.0", "native", True, True, "release", None),          # fails health
    ("gige_camera", "1.0.0", "container", False, True, "release", TWO),
]


@pytest.fixture(scope="session")
def keys():
    kd = WORK / "keys"
    return {"release": crypto.load_or_create(kd / "release.key"), "cloud": crypto.load_or_create(kd / "cloud.key"),
            "rogue": crypto.load_or_create(kd / "rogue.key")}


def _seed_payloads():
    """Reuse the probe's cached container payloads (docker-save archives of apps/container, same build args):
    copied, the probe stays untouched."""
    src, dst = PROBE / "work" / "payloads", WORK / "payloads"
    dst.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        for tar in src.glob("*.image.tar"):
            if not (dst / tar.name).exists():
                shutil.copy2(tar, dst / tar.name)


@pytest.fixture(scope="session")
def registry(keys):
    """Build every release once (docker build + save unless cached, descriptor from flexModel, SBOM, signature)."""
    oci = WORK / "oci"
    shutil.rmtree(oci, ignore_errors=True)
    cold = not any((WORK / "payloads").glob("*.tar")) if (WORK / "payloads").exists() else True
    _seed_payloads()
    layout = Layout(oci)
    hashes = module_hashes(MODEL)
    classes = model_safety(MODEL)
    info, t0 = {}, time.time()
    for module, version, profile, fail, sign, key, approvers in RELEASES:
        approval = {"approved": True, "approvers": approvers, "role": "safety-engineer"} if approvers else None
        info[(module, version)] = make_release(
            layout, WORK, keys[key] if key else None, model_dir=MODEL, module=module, version=version,
            profile=profile, fail_health=fail, approval=approval, sign=sign, hashes=hashes, classes=classes)
    metric(f"release_build_{len(RELEASES)}_releases_seconds_{'cold' if cold else 'warm'}", round(time.time() - t0, 2))
    return {"dir": oci, "layout": layout, "info": info}


@pytest.fixture
def env(tmp_path, keys, registry):
    """Returns a factory for engines; tears everything down afterwards."""
    cloud = Cloud(tmp_path, keys, registry)
    engines: list[EngineProc] = []
    shims: list[NetShim] = []

    class Env:
        def __init__(self):
            self.cloud = cloud
            self.keys = keys
            self.registry = registry
            self.tmp = tmp_path

        def engine(self, prefix: str, via_shim: bool = False, dev: str | None = None, **kw) -> EngineProc:
            dev = dev or f"{prefix}-{uuid.uuid4().hex[:6]}"
            api = None
            if via_shim:
                shim = NetShim(cloud.srv.port)
                shims.append(shim)
                api = shim.url
                kw.setdefault("extra", {})
            e = EngineProc(tmp_path, cloud, keys, dev, api_url=api, **kw)
            e.shim = shims[-1] if via_shim else None
            engines.append(e)
            return e

    yield Env()
    for e in engines:
        e.cleanup(runtime=(e.cfg["drivers"]["container"] or {}).get("runtime", "docker"))
    for s in shims:
        s.close()
    cloud.stop()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Every P2 test that uses the lab (`env`/`registry`) needs Docker: image builds/loads and the container driver."""
    for it in items:
        if "integration/p2/" in it.nodeid.replace("\\", "/") and {"env", "registry"} & set(getattr(it, "fixturenames", ())):
            it.add_marker(pytest.mark.docker)

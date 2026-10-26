"""Shared helpers of the EV-07 local API / CLI tests: one real device engine (fake native driver) fed from the CP-3
vector bundle, the engine config it was built from, and the API in front of it."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import yaml

from flexengine2.drivers.base import Driver, Observed

FLEXENGINE = Path(__file__).resolve().parents[2]
VECTORS = FLEXENGINE / "api" / "vectors"
BUNDLE = VECTORS / "bundle" / "bundle.tar"
TAMPERED = VECTORS / "bundle" / "bundle-tampered.tar"
DEVICE = "vec-dev"


def vectors() -> dict:
    return {"keys": json.loads((VECTORS / "keys.json").read_text()),
            "manifest": json.loads((VECTORS / "manifest" / "cases.json").read_text()),
            "release": json.loads((VECTORS / "release" / "cases.json").read_text())}


def cloud_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(vectors()["keys"]["cloud"]["private"]))


class FakeDriver(Driver):
    profile, name = "native", "fake"

    def __init__(self, healthy: bool = True):
        self.running: dict[str, dict] = {}
        self.healthy = healthy
        self.fail_reason = None

    def plan(self, current, desired):
        return [f"fake apply {desired.version}"] if desired else [f"fake remove {current.dep_id}"]

    def apply(self, current, desired):
        self.running[desired.dep_id] = {"digest": desired.digest, "version": desired.version}

    def remove(self, dep_id):
        self.running.pop(dep_id, None)

    def status(self, dep_id):
        r = self.running.get(dep_id)
        return Observed(present=bool(r), running=bool(r), digest=(r or {}).get("digest"),
                        version=(r or {}).get("version"))

    def list(self):
        return {k: self.status(k) for k in self.running}

    def health(self, dep_id, probe):
        return self.healthy and dep_id in self.running

    def failed(self, dep_id):
        return None if self.healthy else self.fail_reason


def write_config(tmp: Path, *, socket: bool = True, parked: bool = True, sock_dir: Path | None = None,
                 **local) -> tuple[dict, Path]:
    """Engine config under `tmp`; its local API socket in `sock_dir` (the `sock_dir` fixture: tests that bind it)."""
    v = vectors()
    safe = tmp / "safe.json"
    safe.write_text(json.dumps({"machine_parked": parked, "operator_confirmed": parked}))
    cfg = {"cloud": {"apiUrl": "http://127.0.0.1:9", "cloudPublicKey": v["manifest"]["cloudPublicKey"],
                     "releasePublicKeys": v["release"]["trustedReleaseKeys"], "httpTimeout": 0.3,
                     "pollInterval": 0.2},
           "dataDir": str(tmp / "data"),
           "devices": [{"deviceId": DEVICE, "onboardingToken": "t", "safeStateFile": str(safe),
                        "drivers": {"native": True}}]}
    if socket or local:
        cfg["local"] = {**({"socket": str((sock_dir or tmp) / "api.sock")} if socket else {}), **local}
    p = tmp / "engine.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return cfg, p


def make_engine(cfg: dict, driver: FakeDriver | None = None):
    from flexengine2.core.reconciler import Engine
    from flexengine2.host import device_cfg

    return Engine(device_cfg(cfg, cfg["devices"][0]), {"native": driver or FakeDriver()})


def installed_engine(cfg: dict, driver: FakeDriver | None = None):
    """Engine with the vector bundle's two deployments Installed (cloud unreachable)."""
    eng = make_engine(cfg, driver)
    eng.import_bundle(str(BUNDLE))
    eng.tick()
    return eng

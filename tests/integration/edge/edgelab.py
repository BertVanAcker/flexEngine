"""demo/edge lab on flexEngineV2 (port of demo/edge/tests/conftest.py).

Reference cloud fake (in process, model-checked) + releases from `flex release` (flexmodel) + PLC stand-ins (pyads test
server, pymodbus server; demo/edge/sim, read-only) + optionally the P3 QEMU MCU; the engine under test runs as a real
subprocess (`flexengine run`), exactly as on the LattePanda. The engine never sees the demo/probe code (checked).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import yaml

from flexengine2.testing.release import PROBES, REPO

FLEXENGINE = Path(__file__).resolve().parents[3]
EDGE = REPO / "demo" / "edge"
DP = REPO / "examples" / "models" / "demo-pick"
P3 = PROBES / "p3-mcu-hotload"
P3B = P3 / "build"
WORK = FLEXENGINE / "tests" / "work" / "edge"
RESULTS = Path(os.environ.get("FLEX_RESULTS_DIR") or FLEXENGINE / "results" / "latest")   # per run, gitignored
METRICS: dict = {}

import pytest  # noqa: E402

if not EDGE.is_dir():   # stand-ins from the framework (probes, demo/), not vendored: they import flexmodel
    pytest.skip("needs a modular-interoperable-framework checkout with demo/edge: set FLEX_REPO",
                allow_module_level=True)
pytest.importorskip("flexmodel", reason="flexmodel not importable (not on PyPI): set FLEXMODEL_SRC")
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))  # PLC stand-ins only (`sim` package); flexedge itself is never imported

from flexmodel.release import Release  # noqa: E402

from flexengine2.artefacts.oci import Layout  # noqa: E402
from flexengine2.identity import crypto  # noqa: E402
from flexengine2.testing.edge_lab import HostProc, admin as _admin  # noqa: E402
from flexengine2.testing.fakecloud import CloudServer  # noqa: E402


def metric(name: str, value) -> None:
    METRICS[name] = value


def admin(cloud, method: str, path: str, body=None, user: str = "carol"):
    return _admin(cloud.port, method, path, body, user)


class Lab:
    def __init__(self, name: str):
        self.dir = WORK / name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.cloud_key = crypto.generate()
        self.cloud = CloudServer(self.dir / "cloud", 0, "admin-secret", self.cloud_key, model_dirs=[DP]).start()
        (self.dir / "token").write_text("admin-secret")
        self.host: HostProc | None = None
        self.hosts: list[HostProc] = []
        self.release_cfg = {"deployment": "d_pick_lab", "out": str(self.dir / "rel"),
                            "release_key": str(self.dir / "release.key"), "targets": [],
                            "mcu": {"module_key": str(P3 / "work" / "keys" / "module-ec-p256.pem"),
                                    "edk": {"cortex-m7-hard": str(P3B / "edk-arduino_portenta_h7_m7" / "flexmcu-edk"),
                                            "cortex-m3": str(P3B / "edk" / "flexmcu-edk")}}}

    # ---- releases (flex release, uploaded to the cloud fake) --------------------------------------------
    def release(self, targets: list[dict], model: Path = DP, force: bool = False, approvers=("alice", "bob")) -> dict:
        cfg = {**self.release_cfg, "targets": targets}
        rel = Release(model, cfg, Path(cfg["out"]), approvers=list(approvers), force=force, log=lambda m: None)
        res = rel.run(cloud=self.cloud.url, token="admin-secret")
        bad = [t for t in res["targets"] if t["status"] == "failed"]
        assert not bad, bad
        self.release_pub = res["releasePublicKey"]
        return {t["name"]: t for t in res["targets"]}

    def native_release(self, module: str, version: str) -> None:
        """A GP stand-in workload (P2 native stub app) for the hold-gate tests (no container runtime needed)."""
        from flexengine2.testing.publish import layout_tar, upload
        from flexengine2.testing.release import make_release

        rel = Release(DP, {**self.release_cfg, "targets": []}, Path(self.release_cfg["out"]), log=lambda m: None)
        lay = Layout(self.dir / "native-oci" / f"{module}-{version}")
        make_release(lay, self.dir / "native-work", rel.key, model_dir=DP, module=module, version=version,
                     profile="native", hashes=rel.hashes, classes=rel.classes)
        upload(self.cloud.url, "admin-secret", layout_tar(lay.root), "release-tool")

    # ---- control plane ----------------------------------------------------------------------------------
    def token(self, dev: str) -> str:
        s, b = admin(self.cloud, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def deploy(self, dev: str, items: list[dict], expect: int = 201) -> float:
        self.wait(lambda: dev in [d["deviceId"] for d in admin(self.cloud, "GET", "/api/v1/admin/devices")[1]["devices"]],
                  30, what=f"{dev} onboarded")
        s, p = admin(self.cloud, "POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items})
        assert s == expect, p
        if s != 201:
            return 0.0
        s, b = admin(self.cloud, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        return time.time()

    def status(self, dev: str) -> dict:
        return admin(self.cloud, "GET", f"/api/v1/admin/devices/{dev}")[1]["status"]

    def device(self, dev: str) -> dict:
        return admin(self.cloud, "GET", f"/api/v1/admin/devices/{dev}")[1]

    def wait(self, pred, timeout: float = 60, period: float = 0.1, what: str = ""):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(period)
        log = (self.dir / "edge.log").read_text()[-4000:] if (self.dir / "edge.log").exists() else ""
        raise AssertionError(f"timeout waiting for {what}\n--- edge log tail ---\n{log}")

    def wait_state(self, dev: str, dep: str, state: str, version: str | None = None, timeout: float = 60,
                   after: float = 0):
        def ok():
            r = self.status(dev).get(dep)
            if r and r["state"] == state and (version is None or r.get("version") == version) \
                    and r.get("receivedAt", 0) >= after:
                return r
            return None
        return self.wait(ok, timeout, what=f"{dev}/{dep} -> {state} {version or ''}")

    # ---- engine process (flexengine run) ------------------------------------------------------------------
    def edge_config(self, devices: list[dict]) -> Path:
        cfg = {"cloud": {"apiUrl": self.cloud.url, "cloudPublicKey": crypto.public_b64(self.cloud_key),
                         "releasePublicKeys": [self.release_pub], "pollInterval": 0.3, "backoff": [0.2, 1.0],
                         "httpTimeout": 5},
               "dataDir": str(self.dir / "data"), "model": str(DP), "deployment": "d_pick_lab", "devices": devices}
        p = self.dir / "edge.yaml"
        p.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return p

    def start_edge(self, cfg: Path):
        self.host = HostProc(cfg, self.dir / "edge.log", cwd=self.dir)
        self.hosts.append(self.host)
        self.host.start()

    def stop_edge(self):
        if self.host is not None:
            self.host.stop()

    def actions(self, dev: str) -> list[dict]:
        p = self.dir / "data" / dev / "actions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def close(self):
        self.stop_edge()
        self.cloud.stop()

    def check_clean(self):
        for h in self.hosts:
            h.check_clean()

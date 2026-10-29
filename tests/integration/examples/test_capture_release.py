"""docs/capture-release.md: the worked capture (data-collection) release stays correct.

* its documents satisfy flexEngine's own contracts (descriptor schema, desired-state schema + CP-1 rules) and the
  parameters the deployment sets are the ones the descriptor declares and the payload's collector.json knows;
* the release built and published with the commands of the guide (tools/native_release.py py-payload, release
  --descriptor) runs on a real engine (reconciler + artefact store + process driver, against the reference flexCloud
  fake over HTTP): the http health gate passes, capture files rotate in FLEX_DATA_DIR, retention holds, the payload
  tree stays untouched, a killed workload is restarted (restart policy), and a removal closes the open file.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import signal
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.artefacts.oci import Layout
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import wait_until
from flexengine2.drivers.process import ProcessDriver
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer

FE = Path(__file__).resolve().parents[3]
EXAMPLE = FE / "docs" / "examples" / "capture_release"
DEV = "edge-capture"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="process driver: POSIX sessions and signals")


def _json(name: str) -> dict:
    return json.loads((EXAMPLE / name).read_text())


def test_example_documents_satisfy_the_contracts():
    desc, ds = _json("descriptor.json"), _json("desired-state.json")
    contracts.validate("descriptor", desc)
    contracts.validate("desired-state", ds)
    dep = ds["deployments"][0]
    assert dep["x-flex-driver"] == "native" and dep["spec"]["applicationId"] == desc["module"]
    assert desc["profiles"] == ["native"] and desc["health"]["port"] == "${FLEX_HEALTH_PORT}"
    declared = {p["name"] for p in desc["parameters"]}
    defaults = set(_json("payload/collector.json")["defaults"])
    assert declared == defaults | {"output_dir"}, "descriptor parameters and collector.json defaults differ"
    assert set(dep["spec"]["parameters"]) <= declared
    for runtime in ("process", "systemd"):                       # both native runtimes are valid for the example
        doc = json.loads(json.dumps(ds))
        doc["deployments"][0]["x-flex-runtime"] = runtime
        assert contracts.errors("desired-state", doc) == []


def admin(srv, method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
              headers={"Authorization": "Bearer admin-secret", "X-Flex-User": "carol",
                       "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").endswith("json") else data)


def _cli():
    spec = importlib.util.spec_from_file_location("native_release_cli_capture", FE / "tools" / "native_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree_digest(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file() and p.name not in ("app.log", "app.stderr.log")}


class Lab:
    """flexCloud fake + one engine (in-process) with the real process driver."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cloud_key = crypto.generate()
        self.key_file = tmp / "release.key"
        self.rel_key = crypto.load_or_create(self.key_file)
        (tmp / "cloud").mkdir()
        self.oci = tmp / "cloud" / "oci"
        Layout(self.oci)
        self.srv = CloudServer(tmp / "cloud", 0, "admin-secret", self.cloud_key).start()
        safe = tmp / "safe.json"
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        s, tok = admin(self.srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": DEV})
        assert s == 201, tok
        self.drv = ProcessDriver(DEV, tmp / "data")
        self.eng = Engine({"deviceId": DEV, "dataDir": str(tmp / "data"), "apiUrl": self.srv.url,
                           "onboardingToken": tok["token"], "cloudPublicKey": crypto.public_b64(self.cloud_key),
                           "releasePublicKeys": [crypto.public_b64(self.rel_key)], "safeStateFile": str(safe),
                           "pollInterval": 0.05, "httpTimeout": 5}, {"native": self.drv})
        self.eng.tick()

    def deploy(self, items: list[dict], ticks: int = 3) -> dict:
        s, p = admin(self.srv, "POST", "/api/v1/admin/plans", {"deviceId": DEV, "deployments": items})
        assert s == 201, p
        s, b = admin(self.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        for _ in range(ticks):
            self.eng.tick()
        return admin(self.srv, "GET", f"/api/v1/admin/devices/{DEV}")[1]

    def close(self):
        for dep in list(self.drv.units):
            self.drv.remove(dep)
        self.srv.stop()


@pytest.fixture
def lab(tmp_path):
    lb = Lab(tmp_path)
    yield lb
    lb.close()


def test_capture_release_runs_on_the_engine(lab, tmp_path):
    payload = tmp_path / "payload"
    shutil.copytree(EXAMPLE / "payload", payload)
    if not Path("/proc/loadavg").exists():                    # macOS dev host: the example's synthetic source
        cfg = json.loads((payload / "collector.json").read_text())
        cfg["source"] = {"kind": "synthetic"}
        (payload / "collector.json").write_text(json.dumps(cfg))
    cli, tar = _cli(), tmp_path / "out" / "capture.tar.gz"
    assert cli.main(["py-payload", str(payload), "-o", str(tar)]) == 0
    assert cli.main(["release", "--oci", str(lab.oci), "--key", str(lab.key_file), "--module", "capture_logger",
                     "--version", "1.0.0", "--descriptor", str(EXAMPLE / "descriptor.json"), str(tar)]) == 0
    lay = Layout(lab.oci)
    man = json.loads(lay.get_blob(lay.resolve("flexnodes/capture_logger:1.0.0")["digest"]))
    desc = json.loads(lay.get_blob(man["config"]["digest"]))
    assert desc["health"] == _json("descriptor.json")["health"] and desc["resources"]["cpu_cores"] == 0.1
    contracts.validate("descriptor", desc)

    params = {"prefix": "t", "sample_period_s": 0.05, "rotate_s": 0.4, "keep_files": 3, "max_mb": 1}
    dev = lab.deploy([{"id": "capture", "module": "capture_logger", "version": "1.0.0", "driver": "native",
                       "runtime": "process", "parameters": params}])
    assert dev["status"]["capture"]["state"] == "Installed", dev["status"]   # the http health gate passed

    data = lab.drv.data_dir("capture")
    caps = data / "captures"
    tree = Path(lab.drv.units["capture"]["dir"])
    before = _tree_digest(tree)
    assert wait_until(lambda: len(list(caps.glob("t-*.jsonl"))) >= 3, 15, 0.1), list(caps.iterdir())
    assert wait_until(lambda: len(list(caps.glob("t-*.jsonl"))) <= 3, 5, 0.1)      # retention: keep_files
    done = sorted(caps.glob("t-*.jsonl"))
    rec = json.loads(done[-1].read_text().splitlines()[0])
    assert "t" in rec and "value" in rec
    assert _tree_digest(tree) == before, "the workload wrote into its (read-only) payload tree"

    pid = lab.drv.units["capture"]["pid"]                      # restart policy: always
    os.killpg(pid, signal.SIGKILL)
    assert wait_until(lambda: (lab.eng.tick() or True) and lab.drv.units["capture"].get("pid") not in (None, pid)
                      and lab.drv.health("capture", desc["health"]), 20, 0.1)
    assert lab.drv.units["capture"]["restarts"] >= 1

    dev = lab.deploy([])                                       # removal: SIGTERM closes the open file
    assert dev["status"]["capture"]["state"] == "Removed", dev["status"]
    assert not list(caps.glob("*.part")) and list(caps.glob("t-*.jsonl"))

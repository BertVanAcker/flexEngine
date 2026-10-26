"""FE-4 end to end with the native `process` runtime: the reference cloud (fakecloud) serves signed desired states,
an in-process engine pulls them and runs real processes from real signed releases.

  * Push / Run / Stop: staged -> running -> stopped -> running for two deployments, x-flex-dependsOn order on start
    and the reverse on stop, the release stays installed (no second unpack), Staged / Stopped reported
  * the health gate runs only on the transition to running (a release that fails it stages fine)
  * a release that failed its gate is retried when the cloud publishes the same deployments again (feature retry)
  * the on-device build: BuildStarted / BuildProgress / BuildLog / BuildSucceeded and BuildFailed (FE-BLD-001) as
    device events, the built payload runs
  * refusals: a runtime the device does not allow (FE-DS-008, engine side; the reference cloud refuses the plan
    already), and the FE-4 keys on an engine whose drivers cannot carry them (FE-DS-007)
"""

from __future__ import annotations

import io
import json
import tarfile
import threading
import time
from pathlib import Path

import pytest

from flexengine2.artefacts.oci import MT_PAYLOAD_SOURCE, Layout
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import Driver
from flexengine2.drivers.process import ProcessDriver
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing.release import (APPS, VECTORS, descriptor_from_module, make_release, publish_release,
                                         sbom_stub, sign_release)
from flexengine2.util import canonical_json

MODEL = VECTORS / "models" / "uc1"
MODULE = "detection_logger"          # native, safety class none in the UC1 model


def _targz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in sorted(files.items()):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), 0o755 if name.endswith(".sh") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def source_release(layout: Layout, key, version: str, *, fail: bool = False) -> dict:
    """A release with only a source payload: build.sh copies the app into out/ (or fails)."""
    script = (b"echo compiling\necho flex-progress 0.5\nexit 2\n" if fail else
              b"echo compiling\necho flex-progress 0.5\nmkdir -p out\ncp app.py release.json out/\necho done\n")
    src = _targz({"build.sh": script, "app.py": (APPS / "native" / "app.py").read_bytes(),
                  "release.json": canonical_json({"app": MODULE, "version": version, "fail_health": False})})
    health = {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.25,
              "successThreshold": 2, "timeoutSeconds": 8}
    desc = descriptor_from_module(MODEL, MODULE, version, profile="native", health=health,
                                  restart={"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5})
    desc["build"] = {"command": ["sh", "build.sh"], "output": "out", "timeoutSeconds": 60}
    repo = f"flexnodes/{MODULE}"
    mdesc = publish_release(layout, repo, desc, [(MT_PAYLOAD_SOURCE, src, {"dev.flex.profile": "native"})],
                            sbom_stub(MODULE, version, "native"))
    sign_release(layout, key, repo, mdesc, version)
    return {"digest": mdesc["digest"]}


class NoRunState(ProcessDriver):
    """A native driver without the FE-4 run states (as every driver was before FE-4)."""
    stage, start, stop, build = Driver.stage, Driver.start, Driver.stop, Driver.build


class Lab:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.release_key, self.cloud_key = crypto.generate(), crypto.generate()
        (tmp / "cloud").mkdir()
        self.layout = Layout(tmp / "cloud" / "oci")
        for v, fail in (("1.0.0", False), ("1.1.0", True)):
            make_release(self.layout, tmp, self.release_key, model_dir=MODEL, module=MODULE, version=v,
                         profile="native", fail_health=fail)
        source_release(self.layout, self.release_key, "2.0.0")
        source_release(self.layout, self.release_key, "2.1.0", fail=True)
        self.srv = CloudServer(tmp / "cloud", 0, "admin-secret", self.cloud_key, model_dirs=[MODEL]).start()
        self.engines: list[Engine] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- cloud -------------------------------------------------------------------------------------------------
    def admin(self, method, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
                  headers={"Authorization": "Bearer admin-secret", "X-Flex-User": "carol",
                           "Content-Type": "application/json", "Connection": "close"})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out else None)

    def deploy(self, dev: str, items: list[dict]) -> int:
        s, p = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items})
        assert s == 201, p
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        return b["manifestVersion"]

    def publish_raw(self, dev: str, deployments: list[dict]) -> int:
        """Bypass the reference cloud's plan checks (what an older or careless control plane might serve)."""
        st = self.srv.state
        with st.lock:
            mv = st.desired_doc(dev)["manifestVersion"] + 1
            st.s["desired"][dev] = {"manifestVersion": mv, "deployments": deployments}
            st.save()
        return mv

    def status(self, dev: str) -> dict:
        return self.admin("GET", f"/api/v1/admin/devices/{dev}")[1]["status"]

    def events(self, dev: str) -> list[dict]:
        return [e for e in self.srv.state.s.get("events", []) if e.get("deviceId") == dev]

    def wait(self, pred, timeout=30, what=""):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError(f"timeout waiting for {what}")

    def states(self, dev: str, expect: dict, mv: int, timeout=30):
        """Every deployment in `expect` reported that state for manifest `mv` or later (an unchanged one does not
        report again, so name only those the manifest changes)."""
        def ok():
            st = self.status(dev)
            return all((st.get(k) or {}).get("state") == v and (st[k].get("manifestVersion") or 0) >= mv
                       for k, v in expect.items()) and st
        return self.wait(ok, timeout, f"{dev}: {expect} at v{mv}: {self.status(dev)}")

    # ---- engine ------------------------------------------------------------------------------------------------
    def engine(self, dev: str, driver_cls=ProcessDriver) -> Engine:
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        data = self.tmp / dev
        cfg = {"deviceId": dev, "dataDir": str(data), "apiUrl": self.srv.url, "allowPlainHttp": True,
               "cloudPublicKey": crypto.public_b64(self.cloud_key),
               "releasePublicKeys": [crypto.public_b64(self.release_key)], "onboardingToken": b["token"],
               "pollInterval": 0.1, "backoff": [0.05, 0.2], "logFormat": "text"}
        eng = Engine(cfg, {"native": driver_cls(dev, data)})
        self.engines.append(eng)

        def loop():
            while not self._stop.is_set():
                eng.tick()
                time.sleep(0.05)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self._threads.append(t)
        self.wait(lambda: (self.srv.state.s["devices"].get(dev) or {}).get("capabilities"), 20, f"{dev} onboarded")
        return eng

    def close(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        for e in self.engines:
            drv = e.drivers["native"]
            for dep in list(drv.units):
                drv.remove(dep)
        self.srv.stop()


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    try:
        yield lab
    finally:
        lab.close()


def item(dep_id, version="1.0.0", run=None, after=(), **kw):
    it = {"id": dep_id, "module": MODULE, "version": version, "driver": "native", **kw}
    if run:
        it["runState"] = run
    if after:
        it["dependsOn"] = [{"deployment": a} for a in after]
    return it


def actions(eng, kind):
    return [(a["action"], a["dep"]) for a in map(json.loads, eng.actions_path.read_text().splitlines())
            if a["action"] == kind]


def test_push_run_stop_run_in_dependency_order(lab):
    eng = lab.engine("fe4a")
    caps = lab.srv.state.s["devices"]["fe4a"]["capabilities"]["x-flex"]
    assert caps["features"] == ["runState", "runtime", "build", "retry"] and caps["runtimes"] == ["process"]
    drv = eng.drivers["native"]
    mv = lab.deploy("fe4a", [item("b", run="staged", after=["a"]), item("a", run="staged")])     # Push
    st = lab.states("fe4a", {"a": "Staged", "b": "Staged"}, mv)
    assert "healthGateSeconds" not in st["a"], "staging runs no health gate"
    assert not any(drv._alive(u) for u in drv.units.values()), "nothing runs after a push"
    mv = lab.deploy("fe4a", [item("b", after=["a"]), item("a")])                                 # Run all
    st = lab.states("fe4a", {"a": "Installed", "b": "Installed"}, mv)
    assert st["a"]["health"] == "healthy" and "healthGateSeconds" in st["b"]
    assert actions(eng, "start") == [("start", "a"), ("start", "b")]
    assert all(drv._alive(u) for u in drv.units.values())
    mv = lab.deploy("fe4a", [item("b", run="stopped", after=["a"]), item("a", run="stopped")])  # Stop all
    lab.states("fe4a", {"a": "Stopped", "b": "Stopped"}, mv)
    assert actions(eng, "stop") == [("stop", "b"), ("stop", "a")]
    assert not any(drv._alive(u) for u in drv.units.values())
    time.sleep(1.5)                                                          # no restart policy brings them back
    assert not any(drv._alive(u) for u in drv.units.values())
    mv = lab.deploy("fe4a", [item("b", after=["a"]), item("a", run="stopped")])                 # Run one: b alone
    st = lab.states("fe4a", {"b": "Installed"}, mv)
    assert st["a"]["state"] == "Stopped" and actions(eng, "start")[-1] == ("start", "b")
    mv = lab.deploy("fe4a", [item("b", after=["a"]), item("a")])
    st = lab.states("fe4a", {"a": "Installed"}, mv)
    assert st["b"]["state"] == "Installed" and actions(eng, "start")[-1] == ("start", "a")
    assert len(actions(eng, "stage")) == 2 and not actions(eng, "apply"), "installed once, started and stopped"


def test_the_gate_runs_only_on_start_and_a_retry_tries_again(lab):
    eng = lab.engine("fe4b")
    mv = lab.deploy("fe4b", [item("a", "1.1.0", run="staged")])             # 1.1.0 fails its health check
    lab.states("fe4b", {"a": "Staged"}, mv)
    mv = lab.deploy("fe4b", [item("a", "1.1.0")])
    st = lab.states("fe4b", {"a": "Failed"}, mv)
    assert st["a"]["reasonCode"] == "FE-REC-004" and "rolledBackTo" not in st["a"]
    assert not eng.drivers["native"]._alive(eng.drivers["native"].units["a"]), "stopped again, kept installed"
    n = len(actions(eng, "start"))
    time.sleep(1.0)
    assert len(actions(eng, "start")) == n, "not started again on its own"
    mv = lab.deploy("fe4b", [item("a", "1.1.0")])                            # the same deployments again: retry
    lab.wait(lambda: len(actions(eng, "retry")) == 1 and len(actions(eng, "start")) == n + 1, 30, "retried start")
    lab.states("fe4b", {"a": "Failed"}, mv)


def test_build_on_the_device_reports_its_events(lab):
    eng = lab.engine("fe4c")
    mv = lab.deploy("fe4c", [item("a", "2.0.0", run="staged", build="b-1")])
    lab.states("fe4c", {"a": "Staged"}, mv)
    evs = [e for e in lab.events("fe4c") if e["event"].startswith("Build")]
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "BuildStarted" and kinds[-1] == "BuildSucceeded", kinds
    assert "BuildProgress" in kinds and {"compiling", "done"} <= {e.get("line") for e in evs}
    assert all(e["deployment"] == "a" and e["buildId"] == "b-1" for e in evs)
    assert evs[-1]["resultDigest"].startswith("sha256:")
    mv = lab.deploy("fe4c", [item("a", "2.0.0", build="b-1")])
    lab.states("fe4c", {"a": "Installed"}, mv)
    assert [a for a in actions(eng, "build")] == [("build", "a")], "built once per id"
    mv = lab.deploy("fe4c", [item("a", "2.1.0", build="b-2")])               # its build script fails
    lab.wait(lambda: any(e["event"] == "BuildFailed" for e in lab.events("fe4c")), 30, "BuildFailed")
    failed = [e for e in lab.events("fe4c") if e["event"] == "BuildFailed"][-1]
    assert failed["reasonCode"] == "FE-BLD-001" and failed["reason"] == "build failed: build command exited with status 2"
    time.sleep(0.5)
    assert lab.status("fe4c")["a"]["state"] == "Installed" and lab.status("fe4c")["a"]["version"] == "2.0.0"


def test_refusals_of_what_the_device_does_not_support(lab):
    lab.engine("fe4d")
    s, b = lab.admin("POST", "/api/v1/admin/plans", {"deviceId": "fe4d", "deployments": [item("a", runtime="systemd")]})
    assert s == 422 and b["title"] == "Runtime not available on the device"
    rel = lab.srv.state.resolve_deployment("fe4d", item("a"), "127.0.0.1")
    lab.publish_raw("fe4d", [{**rel, "x-flex-runtime": "systemd"}])
    ev = lab.wait(lambda: [e for e in lab.events("fe4d") if e["event"] == "DesiredStateRefused"], 20, "refusal")[-1]
    assert ev["reasonCode"] == "FE-DS-008"

    lab.engine("fe4e", NoRunState)
    caps = lab.srv.state.s["devices"]["fe4e"]["capabilities"]["x-flex"]
    assert "runState" not in caps["features"] and "build" not in caps["features"]
    s, b = lab.admin("POST", "/api/v1/admin/plans", {"deviceId": "fe4e", "deployments": [item("a", run="staged")]})
    assert s == 422 and b["title"] == "Not supported by the device"
    rel = lab.srv.state.resolve_deployment("fe4e", item("a"), "127.0.0.1")
    lab.publish_raw("fe4e", [{**rel, "x-flex-runState": "staged"}])
    ev = lab.wait(lambda: [e for e in lab.events("fe4e") if e["event"] == "DesiredStateRefused"], 20, "refusal")[-1]
    assert ev["reasonCode"] == "FE-DS-007"

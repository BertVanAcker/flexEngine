"""EV-35 end to end: flexIA packages -> `flex import-flexia` (build in "CI", sign, OCI layout, publish to the reference
fake cloud, plan) -> plan/approve/apply -> a real engine installs and runs them (container driver on real Docker,
native process driver), health OK.

Packages (packages/): flcpy-docker (flexCommunicator publisher, docker workflow, process health and a published port:
EV-39/41), compose-two (two services: api with a compose healthcheck -> exec probe, baked environment and a published
port; worker from an image with a named volume), appdesc-native (application descriptor: two native python runnables
+ one docker runnable), and the RoboSAPIENS example (v1 flexIA.ini verbatim, device `local`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.artefacts.oci import Layout
from flexengine2.identity import crypto

from .flexialab import PACKAGES, REPO, Cloud, Engine, container_ip, flcpy_wheels, http_get, import_flexia

ROBOSAPIENS = REPO / "flexmodel" / "tests" / "fixtures" / "flexia" / "robosapiens"
pytestmark = pytest.mark.docker


@pytest.fixture
def lab(tmp_path):
    cloud_key = crypto.generate()
    cloud = Cloud(tmp_path, cloud_key)
    key_file = tmp_path / "release.key"
    rkey = crypto.load_or_create(key_file)
    engines = []

    def engine(dev: str) -> Engine:
        e = Engine(tmp_path, cloud, cloud_key, crypto.public_b64(rkey), dev)
        engines.append(e)
        cloud.wait(lambda: dev in cloud.st.s["devices"], 30, f"{dev} onboarded")
        return e

    yield {"cloud": cloud, "key": key_file, "engine": engine, "tmp": tmp_path}
    for e in engines:
        e.stop()
    cloud.stop()
    # image archives are large: free the layouts, the cloud registry, the engines' stores and the imported images
    for p in tmp_path.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    ids = subprocess.run(["docker", "images", "-q", "--filter", "label=dev.flex.import=flexia"], capture_output=True,
                         text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rmi", "-f", *sorted(set(ids))], capture_output=True)


def check_layout(out: Path):
    """Every release in the importer's layout is a CP-3 release: descriptor + manifest schemas, SBOM, signature."""
    lay = Layout(out / "oci")
    idx = json.loads((out / "oci" / "index.json").read_text())
    n = 0
    for e in idx["manifests"]:
        man = lay.get_manifest(e["digest"])
        if man.get("artifactType") != "application/vnd.flex.node.v1+json":
            continue
        n += 1
        desc = json.loads(lay.get_blob(man["config"]["digest"]))
        assert contracts.errors("descriptor", desc) == [], (desc["module"], contracts.errors("descriptor", desc))
        assert any(l["mediaType"] == "application/spdx+json" for l in man["layers"])
        assert lay.referrers(e["digest"])["manifests"], f"{desc['module']} has no signature referrer"
    return n


def test_import_publish_plan_apply_run(lab):
    cloud, tmp = lab["cloud"], lab["tmp"]
    wheels = flcpy_wheels()
    if wheels is None:
        pytest.skip("flexCommunicator wheels not downloadable (no network): flcpy package not testable")
    pkgs = {}
    for name in ("flcpy-docker", "compose-two", "appdesc-native"):
        pkgs[name] = tmp / name
        shutil.copytree(PACKAGES / name, pkgs[name])
    shutil.copytree(wheels, pkgs["flcpy-docker"] / "wheels")
    eng = lab["engine"]("edge1")

    common = ["--key", str(lab["key"]), "--version", "1.0.0", "--publish", cloud.url,
              "--admin-token-file", str(cloud.token_file)]
    deployments, reports = [], {}
    for name, pkg in pkgs.items():
        extra = ["--plan"] if name == "compose-two" else []
        rc, doc, err = import_flexia(pkg, tmp / f"out-{name}", *common, *extra)
        assert rc == 0, (name, err[-3000:], json.dumps(doc.get("report", {}).get("notes"), indent=1)[:3000])
        rep, prop = doc["report"], doc["proposal"]
        reports[name] = rep
        assert rep["upload"]["releases"], rep["upload"]
        assert check_layout(tmp / f"out-{name}") == len(rep["units"])
        deployments += prop["devices"]["edge1"]["deployments"]

    # --plan created a plan from the proposal and evaluated the capability checks against the onboarded engine
    plan = reports["compose-two"]["plans"]["edge1"]
    assert plan["status"] == 201 and plan["planHash"].startswith("sha256:"), plan
    checks = {c["check"]: c["result"] for c in json.loads((tmp / "out-compose-two" / "proposal.json").read_text())
              ["devices"]["edge1"]["checks"]}
    assert checks["driver"] == "ok" and checks["cpu"] == "ok", checks
    # compose depends_on: worker after api (order note, V2 does not order)
    ids = [d["id"] for d in deployments]
    assert ids.index("api") < ids.index("worker")
    by = {d["id"]: d for d in deployments}
    assert by["flcpy-publisher"]["driver"] == "container" and by["counter"]["driver"] == "native"
    assert by["reporter"]["driver"] == "native" and by["web"]["driver"] == "container"

    cloud.deploy("edge1", deployments)
    expect = {d["id"]: d["version"] for d in deployments}
    st = cloud.wait(lambda: cloud.converged("edge1", expect), 240, f"edge1 -> {expect}")
    assert all(st[i]["health"] == "healthy" for i in expect)

    # containers run by digest, with the baked compose environment; the api's own health endpoint is the probe
    for i in ("flcpy-publisher", "api", "worker", "web"):
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}} {{.Config.Image}}", f"flex_edge1_{i}"],
                             capture_output=True, text=True, check=True).stdout.split()
        assert out[0] == "true" and "@sha256:" in out[1], (i, out)
    s, body = http_get(container_ip("flex_edge1_api"), 8080, "/greeting")
    assert (s, body) == (200, b"hello from compose")
    # EV-41: the compose port is published on the host, the worker's named volume exists and is mounted
    assert http_get("127.0.0.1", 38082, "/greeting") == (200, b"hello from compose")
    vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "label=dev.flex.device=edge1"],
                          capture_output=True, text=True).stdout.split()
    assert vols == ["flex_edge1_worker_worker-state"], vols
    cloud.wait(lambda: subprocess.run(["docker", "exec", "flex_edge1_worker", "test", "-f", "/state/started"],
                                      capture_output=True).returncode == 0, 20, "worker wrote its volume")
    # EV-39: health without a shim (exec probe for the compose healthcheck, process probe for the others)
    probes = {u["id"]: u["releases"][0]["health"]["type"] for r in reports.values() for u in r["units"]
              if u["kind"] == "container"}
    assert probes == {"flcpy-publisher": "process", "api": "exec", "worker": "process", "web": "process"}, probes
    logs = subprocess.run(["docker", "logs", "flex_edge1_flcpy-publisher"], capture_output=True, text=True).stdout
    assert "flexCommunicator DataMessage available" in logs
    # native: cwd-relative config.yaml, sibling module import and the manifest arguments work in the unpacked release
    units = eng.units()
    cnt = next(Path(u["dir"]) / "count.json" for k, u in units.items() if k.endswith("counter"))
    first = json.loads(cloud.wait(lambda: cnt.exists() and cnt.read_text(), 10, "count.json"))
    assert first["config"] == "name: counter" and first["label"] == "counter-lib"
    cloud.wait(lambda: json.loads(cnt.read_text())["count"] > first["count"] + 2, 10, "counter counts")


def test_robosapiens_example_local_runs(lab):
    """The v1 RoboSAPIENS flexIA.ini (verbatim) with its compose files: device `local` imports, verifies and runs."""
    cloud, tmp = lab["cloud"], lab["tmp"]
    pkg = tmp / "robosapiens"
    shutil.copytree(ROBOSAPIENS, pkg)
    lab["engine"]("local")
    rc, doc, err = import_flexia(pkg, tmp / "out-rs", "--device", "local", "--key", str(lab["key"]), "--version",
                                 "1.0.0", "--publish", cloud.url, "--admin-token-file", str(cloud.token_file))
    assert rc == 0, err[-3000:]
    deps = doc["proposal"]["devices"]["local"]["deployments"]
    assert [d["id"] for d in deps] == ["monitor", "plan"]
    cloud.deploy("local", deps)
    cloud.wait(lambda: cloud.converged("local", {d["id"]: "1.0.0" for d in deps}), 180, "local converged")
    env = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Env}}", "flex_local_monitor"],
                         capture_output=True, text=True, check=True).stdout
    assert "MQTT_BROKER=localhost" in env


def test_crash_fails_the_gate_and_rolls_back(lab):
    """The process probe (EV-39, no shim) and fail-fast: a crashing v1 node fails the install, nothing stays."""
    cloud, tmp = lab["cloud"], lab["tmp"]
    pkg = tmp / "crasher"
    pkg.mkdir()
    (pkg / "flexIA.ini").write_text("[PACKAGE]\nname = crasher\n[build:edge2]\nworkflow = docker\n"
                                    "[run:edge2]\nworkflow = docker\narguments = \"--restart no\"\n")
    (pkg / "Dockerfile").write_text('FROM mirror.gcr.io/library/python:3.13-alpine\n'
                                    'CMD ["python", "-c", "import time; time.sleep(0.5); raise SystemExit(3)"]\n')
    lab["engine"]("edge2")
    rc, doc, err = import_flexia(pkg, tmp / "out-c", "--key", str(lab["key"]), "--version", "1.0.0", "--publish",
                                 cloud.url, "--admin-token-file", str(cloud.token_file))
    assert rc == 0, err[-2000:]
    cloud.deploy("edge2", doc["proposal"]["devices"]["edge2"]["deployments"])
    cloud.wait(lambda: (cloud.status("edge2").get("crasher") or {}).get("state") == "Failed", 90, "crasher failed")
    r = cloud.status("edge2")["crasher"]
    assert "health" in (r.get("reason") or ""), r
    assert subprocess.run(["docker", "inspect", "flex_edge2_crasher"], capture_output=True).returncode != 0

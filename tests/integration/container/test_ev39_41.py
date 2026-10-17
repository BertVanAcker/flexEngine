"""EV-39/40/41 acceptance through the ENGINE (reconciler + artefact store + container driver, against the reference
flexCloud fake over HTTP), on the fake docker CLI (push tier) and on real Docker (marker docker):

* EV-40: one container release with an image layer per CPU (x86_64 + aarch64): the engine runs the image of its
  runtime's CPU; a release with only the other CPU's image is refused at preflight (FE-CTR-PRE-001), nothing is
  loaded and the previous version keeps running.
* EV-39: a release with an `exec` health probe (no HTTP endpoint) passes the health gate; a failing exec command
  fails it and the engine rolls back.
* EV-41: env / envFrom (secret file) / published port / named volume from the signed descriptor, allowed by the
  device config: installed; the volume survives an update and is deleted with the deployment. A run option the
  device does not allow is refused at preflight (previous version kept).
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from flexengine2.artefacts.oci import MT_PAYLOAD_IMAGE, Layout
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import http_probe, wait_until
from flexengine2.drivers.container import CliContainerDriver
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing.release import publish_release, sbom_stub, sign_release

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

from spi_harness.base import FAKES, digest_of, free_port, shim  # noqa: E402

DEV = "lab-ev40"
MODULE = "ev40_node"
OTHER = {"x86_64": "aarch64", "aarch64": "x86_64"}


def admin(srv, method, path, body=None, user="carol"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
              headers={"Authorization": "Bearer admin-secret", "X-Flex-User": user, "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").endswith("json") else data)


def descriptor(version: str, health: dict, run_options: dict | None = None) -> dict:
    d = {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": MODULE, "moduleVersion": version,
         "releaseVersion": version, "interfaceHash": None, "language": "container", "profiles": ["container"],
         "ports": [], "parameters": [], "health": health, "restartPolicy": {"policy": "on-failure", "maxRetries": 2},
         "safetyClass": "none", "safetyClassSource": "declared (EV-40 test)"}
    if run_options:
        d["runOptions"] = run_options
    return d


# ---- image sources: the fake CLI's JSON "archives", or real alpine images -------------------------------------------
class FakeImages:
    real = False

    def __init__(self, tmp: Path, mp):
        self.tmp = tmp
        bindir = tmp / "bin"
        shim(bindir, "docker", FAKES / "fakeoci.py", {"FAKE_OCI_STATE": tmp / "fakeoci", "FAKE_OCI_NAME": "docker"})
        mp.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        self.port = free_port()

    def image(self, version: str, layer: str, ready: bool = True) -> tuple[bytes, str]:
        img = {"port": self.port, "ready": ready, "layer": layer}
        ref = f"docker.io/flexnode/{MODULE}@{digest_of(json.dumps(img, sort_keys=True).encode() + version.encode())}"
        return json.dumps({"fakeoci": 1, "images": {ref: img}}).encode(), ref

    def http_health(self) -> dict:
        return {"type": "http", "port": self.port, "path": "/healthz"}

    def cleanup(self):
        pass


DOCKERFILE = """FROM alpine:3.20
LABEL flex.test.layer={layer}
COPY run.sh /run.sh
ENTRYPOINT ["/bin/sh", "/run.sh"]
"""
RUN_SH = """#!/bin/sh
trap 'exit 0' TERM INT
printf 'HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok' > /r
( while true; do nc -l -p 8080 < /r > /dev/null 2>&1; done ) &
while true; do sleep 1 & wait $!; done
"""


class DockerImages:
    """alpine:3.20 stand-ins (present locally, no pull); the layer name is an image label, so the test can tell
    which layer the engine loaded (both are host-CPU images: the engine selects by annotation, never by content)."""
    real = True

    def __init__(self, tmp: Path, mp):
        if subprocess.run(["docker", "image", "inspect", "alpine:3.20"], capture_output=True).returncode != 0:
            pytest.skip("alpine:3.20 not present locally (no registry pulls in the tests)")
        self.tmp, self.tags = tmp, []

    def image(self, version: str, layer: str, ready: bool = True) -> tuple[bytes, str]:
        ctx = self.tmp / f"img-{version}-{layer}"
        ctx.mkdir()
        (ctx / "Dockerfile").write_text(DOCKERFILE.format(layer=layer))
        (ctx / "run.sh").write_text(RUN_SH)
        tag = f"docker.io/flexnode/{MODULE}_t{uuid.uuid4().hex[:6]}:{version}"
        subprocess.run(["docker", "build", "-q", "-t", tag, str(ctx)], check=True, capture_output=True, timeout=300)
        tar = ctx / "image.tar"
        subprocess.run(["docker", "save", "-o", str(tar), tag], check=True, capture_output=True, timeout=300)
        import tarfile
        with tarfile.open(tar) as tf:
            dg = json.load(tf.extractfile("index.json"))["manifests"][0]["digest"]
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)
        self.tags.append(tag)
        data = tar.read_bytes()
        shutil.rmtree(ctx, ignore_errors=True)
        return data, f"{tag.rsplit(':', 1)[0]}@{dg}"

    def http_health(self) -> dict:
        return {"type": "http", "port": 8080, "path": "/healthz"}

    def cleanup(self):
        p = subprocess.run(["docker", "images", "--format", "{{.Repository}}"], capture_output=True, text=True)
        for repo in sorted(set(p.stdout.split())):
            if f"flexnode/{MODULE}_t" in repo:
                subprocess.run(["docker", "rmi", "-f", *subprocess.run(
                    ["docker", "images", "-q", repo], capture_output=True, text=True).stdout.split()],
                    capture_output=True)


class Lab:
    def __init__(self, tmp: Path, images, allowed: dict | None = None):
        self.tmp, self.images = tmp, images
        self.dev = f"{DEV}-{uuid.uuid4().hex[:4]}"
        self.cloud_key, self.rel_key = crypto.generate(), crypto.generate()
        (tmp / "cloud").mkdir()
        self.layout = Layout(tmp / "cloud" / "oci")
        self.srv = CloudServer(tmp / "cloud", 0, "admin-secret", self.cloud_key).start()
        s, tok = admin(self.srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": self.dev})
        assert s == 201, tok
        sd = tmp / "secrets"
        sd.mkdir(mode=0o700)
        (sd / "db-password").write_text("s3cret\n")
        (sd / "db-password").chmod(0o600)
        self.drv = CliContainerDriver(self.dev, "docker", allowed or {}, secrets_dir=str(sd))
        self.arch = self.drv.platform()["dev.flex.arch"]
        self.eng = Engine({"deviceId": self.dev, "dataDir": str(tmp / "data"), "apiUrl": self.srv.url,
                           "onboardingToken": tok["token"], "cloudPublicKey": crypto.public_b64(self.cloud_key),
                           "releasePublicKeys": [crypto.public_b64(self.rel_key)], "pollInterval": 0.05,
                           "httpTimeout": 5}, {"container": self.drv})
        self.eng.tick()

    def release(self, version: str, archs: list[str], health: dict | None = None, run_options=None,
                ready: bool = True) -> dict:
        payloads = []
        self.refs = {}
        for a in archs:
            data, ref = self.images.image(version, a, ready)
            self.refs[a] = ref
            payloads.append((MT_PAYLOAD_IMAGE, data, {"dev.flex.profile": "container", "dev.flex.arch": a,
                                                      "dev.flex.imageRef": ref,
                                                      "dev.flex.imageTag": f"flexnode/{MODULE}:{version}"}))
        h = {"periodSeconds": 0.2, "successThreshold": 2, "timeoutSeconds": 20, **(health or self.images.http_health())}
        repo = f"flexnodes/{MODULE}"
        m = publish_release(self.layout, repo, descriptor(version, h, run_options), payloads,
                            sbom_stub(MODULE, version, "container"))
        sign_release(self.layout, self.rel_key, repo, m, version)
        return m

    def deploy(self, version: str, ticks: int = 3) -> dict:
        s, p = admin(self.srv, "POST", "/api/v1/admin/plans", {"deviceId": self.dev, "deployments": [
            {"id": "node", "module": MODULE, "version": version, "driver": "container"}]})
        assert s == 201, p
        s, b = admin(self.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        for _ in range(ticks):
            self.eng.tick()
        return admin(self.srv, "GET", f"/api/v1/admin/devices/{self.dev}")[1]["status"]["node"]

    def inspect(self) -> dict:
        p = subprocess.run(["docker", "container", "inspect", self.drv.cname("node")], capture_output=True, text=True)
        return json.loads(p.stdout)[0]

    def close(self):
        self.drv.remove("node")
        self.srv.stop()
        self.images.cleanup()


@pytest.fixture(params=[pytest.param(FakeImages, id="fake-docker"),
                        pytest.param(DockerImages, id="docker", marks=[pytest.mark.docker, pytest.mark.slow])])
def images(request, tmp_path, monkeypatch):
    return request.param(tmp_path, monkeypatch)


def _lab(tmp_path, images, allowed=None):
    lab = Lab(tmp_path, images, allowed)
    return lab


def test_two_cpu_release_runs_the_runtime_cpu_image(tmp_path, images):
    lab = _lab(tmp_path, images)
    try:
        caps = admin(lab.srv, "GET", f"/api/v1/admin/devices/{lab.dev}")[1]["device"]["capabilities"]
        assert caps["x-flex"]["drivers"]["container"]["platform"] == {"dev.flex.arch": lab.arch}
        lab.release("1.0.0", [OTHER[lab.arch], lab.arch])
        st = lab.deploy("1.0.0")
        assert st["state"] == "Installed" and st.get("health") == "healthy", st
        info = lab.inspect()
        assert info["Config"]["Image"] == lab.refs[lab.arch] or info["Image"] == lab.refs[lab.arch] or (
            images.real and info["Config"]["Labels"].get("flex.test.layer") == lab.arch), info["Config"]
        # a release with only the other CPU's image: refused at preflight, nothing loaded, 1.0.0 keeps running
        lab.release("2.0.0", [OTHER[lab.arch]])
        st = lab.deploy("2.0.0")
        assert st["state"] == "Failed" and st["reason"] == (
            f"artefact rejected: container image is built for {OTHER[lab.arch]} (dev.flex.arch), this device is "
            f"{lab.arch}"), st
        assert st.get("reasonCode") in (None, "FE-REC-002"), st
        assert lab.drv.status("node").version == "1.0.0" and lab.drv.status("node").running
    finally:
        lab.close()


def test_exec_health_gate(tmp_path, images):
    lab = _lab(tmp_path, images)
    try:
        lab.release("1.0.0", [lab.arch], health={"type": "exec", "command": ["sh", "-c", "exit 0"]})
        st = lab.deploy("1.0.0")
        assert st["state"] == "Installed" and st.get("health") == "healthy", st
        lab.release("2.0.0", [lab.arch], health={"type": "exec", "command": ["sh", "-c", "exit 1"],
                                                 "timeoutSeconds": 2})
        st = lab.deploy("2.0.0", ticks=4)
        assert st["state"] == "Failed" and st["reason"] == "health check failed", st
        assert wait_until(lambda: lab.drv.status("node").version == "1.0.0", 30, 0.2), lab.drv.status("node")
    finally:
        lab.close()


def test_env_ports_volumes_through_the_engine(tmp_path, images):
    port = free_port()
    allowed = {"env": ["APP_MODE", "DB_PASSWORD"], "ports": [f"127.0.0.1:{port}"], "volumes": ["data"]}
    lab = _lab(tmp_path, images, allowed)
    opts = {"env": {"APP_MODE": "demo"}, "envFrom": {"DB_PASSWORD": {"file": "db-password"}},
            "ports": [f"127.0.0.1:{port}:8080"], "volumes": ["data:/data"]}
    vol = lab.drv.vname("node", "data")
    probe = {"type": "exec", "command": ["sh", "-c", 'test "$APP_MODE" = demo && test "$DB_PASSWORD" = s3cret']}
    try:
        lab.release("1.0.0", [lab.arch], health=probe, run_options=opts)
        st = lab.deploy("1.0.0")
        assert st["state"] == "Installed" and st.get("health") == "healthy", st
        assert vol in lab.drv._volumes("node")
        if images.real:
            assert wait_until(lambda: http_probe("127.0.0.1", port, "/healthz"), 15, 0.2)
            subprocess.run(["docker", "exec", lab.drv.cname("node"), "sh", "-c", "echo kept > /data/f"], check=True)
        lab.release("1.1.0", [lab.arch], health=probe, run_options=opts)
        st = lab.deploy("1.1.0")
        assert st["state"] == "Installed", st
        assert vol in lab.drv._volumes("node")
        if images.real:
            p = subprocess.run(["docker", "exec", lab.drv.cname("node"), "cat", "/data/f"], capture_output=True,
                               text=True)
            assert p.stdout.strip() == "kept"
        # a run option the device does not allow: refused at preflight, 1.1.0 keeps running
        lab.release("1.2.0", [lab.arch], health=probe, run_options={**opts, "volumes": ["other:/other"]})
        st = lab.deploy("1.2.0")
        assert st["state"] == "Failed" and st["reason"] == (
            "artefact rejected: runOptions.volumes='other:/other' is not in this device's allow-list"), st
        assert lab.drv.status("node").version == "1.1.0"
        # removal (empty desired state) deletes the deployment's volume
        s, p = admin(lab.srv, "POST", "/api/v1/admin/plans", {"deviceId": lab.dev, "deployments": []})
        assert s == 201, p
        assert admin(lab.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})[0] == 200
        for _ in range(3):
            lab.eng.tick()
        assert lab.drv.status("node").present is False
        assert lab.drv._volumes("node") == []
    finally:
        lab.close()

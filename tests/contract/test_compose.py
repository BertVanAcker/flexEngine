"""FE-4 container runtime `compose` (drivers/compose.py) on a real compose CLI: `docker compose` (marker docker) and
`podman compose` (marker podman). Nightly tier like the other real-runtime harnesses: collected always, run only when
selected with -m, skipped without a usable runtime (`<compose> ls` must answer, i.e. a daemon or socket is there).

Covered: install + the per-service health gate, stage (nothing runs) -> start -> stop -> start without a second
transfer, staging a new release over a running one, the fail-fast reason of a service that exits for good, roll back
to the previous tree as one unit, remove (containers and named volumes gone), a new driver instance sees the project,
and the on-device build (`compose build` of a source payload). The compose policy (host access) is unit-tested in
tests/unit/test_compose.py.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import BuildRequest, Workload, wait_until
from flexengine2.drivers.compose import ComposeDriver

DOCKERFILE = """FROM alpine:3.20
ARG VERSION=0
LABEL dev.flex.test.version=$VERSION
RUN echo "$VERSION" > /version
CMD ["sh", "-c", "sleep 3600"]
"""


def _usable(cli: str) -> bool:
    if not shutil.which(cli):
        return False
    try:
        a = subprocess.run([cli, "ps"], capture_output=True, timeout=30).returncode == 0
        b = subprocess.run([cli, "compose", "ls"], capture_output=True, timeout=60).returncode == 0
        return a and b
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(params=[pytest.param("docker", marks=[pytest.mark.docker, pytest.mark.slow]),
                        pytest.param("podman", marks=[pytest.mark.podman, pytest.mark.slow])])
def cli(request):
    if not _usable(request.param):
        pytest.skip(f"{request.param} compose not usable (`{request.param} ps` / `{request.param} compose ls` failed)")
    return request.param


_IMAGES: dict = {}


def image_tar(cli: str, work: Path, version: str) -> tuple[str, Path]:
    """Build (once per session and runtime) a small image and save it as a docker archive."""
    key = (cli, version)
    if key in _IMAGES and _IMAGES[key][1].exists():
        return _IMAGES[key]
    ctx = work / f"img-{cli}-{version}"
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    (ctx / "Dockerfile").write_text(DOCKERFILE)
    tag = f"localhost/flexnode/ct_compose_{cli}:{version}"
    env = {**os.environ, "DOCKER_BUILDKIT": os.environ.get("DOCKER_BUILDKIT", "0")}
    subprocess.run([cli, "build", "-q", "-t", tag, "--build-arg", f"VERSION={version}", str(ctx)], check=True,
                   capture_output=True, env=env, timeout=600)
    tar = ctx / "image.tar"
    fmt = ["--format", "docker-archive"] if cli == "podman" else []
    subprocess.run([cli, "save", *fmt, "-o", str(tar), tag], check=True, capture_output=True, timeout=300)
    subprocess.run([cli, "rmi", "-f", tag], capture_output=True, timeout=120)   # the driver loads it from the payload
    _IMAGES[key] = (tag, tar)
    return _IMAGES[key]


def targz(files: dict[str, bytes | Path]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in sorted(files.items()):
            data = data.read_bytes() if isinstance(data, Path) else data
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# podman runs healthchecks from systemd timers: on a host without systemd (a CI container) they stay "starting";
# FLEX_COMPOSE_TEST_HEALTHCHECK=0 leaves the healthcheck out there (the gate then checks the containers' states only)
HEALTHCHECK = """    healthcheck:
      test: ["CMD", "test", "-f", "/version"]
      interval: 1s
      timeout: 2s
      retries: 3
"""


def compose_yaml(tag: str, *, fail: bool = False) -> bytes:
    worker = '["sh", "-c", "exit 3"]' if fail else '["sh", "-c", "sleep 3600"]'
    restart = "no" if fail else "on-failure:2"
    hc = HEALTHCHECK if os.environ.get("FLEX_COMPOSE_TEST_HEALTHCHECK", "1") != "0" else ""
    return f"""services:
  web:
    image: {tag}
    command: ["sh", "-c", "sleep 3600"]
{hc}    volumes:
      - data:/data
  worker:
    image: {tag}
    command: {worker}
    restart: "{restart}"
volumes:
  data: {{}}
""".encode()


class Lab:
    def __init__(self, cli: str, tmp: Path):
        self.cli, self.tmp = cli, tmp
        self.device = f"ctc{os.getpid() % 10000}"
        self.work = Path(os.environ.get("FLEX_CONTRACT_WORK", "/tmp")) / "flex-compose-images"
        self.work.mkdir(parents=True, exist_ok=True)
        self.drivers: list[ComposeDriver] = []

    def driver(self) -> ComposeDriver:
        d = ComposeDriver(self.device, self.tmp / "data", cli=self.cli, compose=[self.cli, "compose"])
        self.drivers.append(d)
        return d

    def workload(self, version: str, dep_id: str = "app", fail: bool = False) -> Workload:
        tag, tar = image_tar(self.cli, self.work, version)
        p = self.tmp / f"payload-{dep_id}-{version}-{int(fail)}.tar.gz"
        p.write_bytes(targz({"compose.yaml": compose_yaml(tag, fail=fail), "images/app.tar": tar}))
        import hashlib
        dg = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        return Workload(dep_id=dep_id, module="ct_compose", version=version, digest=dg, payload_path=str(p),
                        payload_annotations={"dev.flex.profile": "container"},
                        health={"timeoutSeconds": 60, "periodSeconds": 0.5}, restart={})

    def gate(self, d, wl) -> tuple[bool, str | None]:
        eng = Engine({"deviceId": "ctc", "dataDir": str(self.tmp / "eng"), "apiUrl": "http://127.0.0.1:9",
                      "releasePublicKeys": [], "cloudPublicKey": "", "logFormat": "text"}, drivers={})
        ok, _ = eng.health_gate(d, wl.dep_id, wl.health)
        return ok, eng.gate_failure

    def project_ls(self) -> list[str]:
        p = subprocess.run([self.cli, "compose", "ls", "-a", "--format", "json"], capture_output=True, text=True,
                           timeout=60)
        try:
            return [x.get("Name") for x in json.loads(p.stdout or "[]")]
        except ValueError:
            return []

    def cleanup(self):
        for d in self.drivers[:1]:
            for dep in list(d._state()):
                d.remove(dep)


@pytest.fixture
def lab(cli, tmp_path):
    lab = Lab(cli, tmp_path)
    try:
        yield lab
    finally:
        lab.cleanup()


def running(d, wl):
    o = d.status(wl.dep_id)
    return o.present and o.running and o.digest == wl.digest


def idle(d, wl):
    o = d.status(wl.dep_id)
    return o.present and not o.running and o.digest == wl.digest


def test_install_gate_stop_start_remove(lab):
    d = lab.driver()
    wl = lab.workload("1.0.0")
    assert d.preflight(type("R", (), {"payload_path": wl.payload_path, "payload_annotations": {},
                                      "descriptor": {}})(), {}) is None
    d.apply(None, wl)
    ok, why = lab.gate(d, wl)
    assert ok, why
    assert running(d, wl) and running(lab.driver(), wl), "a new driver instance sees the project"
    assert d.project(wl.dep_id) in lab.project_ls()
    d.stop(wl.dep_id)
    assert wait_until(lambda: idle(d, wl), 30, 0.5), d.status(wl.dep_id)
    assert d.health(wl.dep_id, wl.health) is False and d.failed(wl.dep_id) is None
    d.start(wl.dep_id)
    ok, why = lab.gate(d, wl)
    assert ok, why
    d.remove(wl.dep_id)
    assert d.status(wl.dep_id).present is False
    assert d.project(wl.dep_id) not in lab.project_ls()
    vols = subprocess.run([lab.cli, "volume", "ls", "-q"], capture_output=True, text=True, timeout=60).stdout.split()
    assert not [v for v in vols if v.startswith(d.project(wl.dep_id))], "named volumes go with the deployment"


def test_stage_runs_nothing_and_replaces_a_running_release(lab):
    d = lab.driver()
    v1, v2 = lab.workload("1.0.0"), lab.workload("2.0.0")
    d.stage(None, v1)
    assert wait_until(lambda: idle(d, v1), 30, 0.5), d.status(v1.dep_id)
    d.start(v1.dep_id)
    assert lab.gate(d, v1)[0]
    d.stage(v1, v2)
    assert wait_until(lambda: idle(d, v2), 60, 0.5), d.status(v2.dep_id)
    d.start(v2.dep_id)
    ok, why = lab.gate(d, v2)
    assert ok, why
    out = subprocess.run([lab.cli, "compose", "-p", d.project("app"), "exec", "-T", "web", "cat", "/version"],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "2.0.0"


def test_a_service_that_exits_fails_fast_and_rolls_back_as_one_unit(lab):
    d = lab.driver()
    good, bad = lab.workload("1.0.0"), lab.workload("1.1.0", fail=True)
    d.apply(None, good)
    assert lab.gate(d, good)[0]
    d.apply(good, bad)
    ok, why = lab.gate(d, bad)
    assert not ok and why and why.startswith("service worker: container exited"), why
    d.apply(bad, good)                                   # the reconciler's rollback: the previous tree, no transfer
    ok, why = lab.gate(d, good)
    assert ok, why
    assert running(d, good)


def test_build_from_a_source_payload(lab, tmp_path):
    d = lab.driver()
    src = tmp_path / "src.tar.gz"
    src.write_bytes(targz({"Dockerfile": DOCKERFILE.encode(),
                           "compose.yaml": b'services:\n  web:\n    build: .\n    command: ["sh", "-c", "sleep 3600"]\n'}))
    lines: list = []
    req = BuildRequest(id="b1", source_path=str(src), spec={}, work_dir=str(tmp_path / "build"),
                       emit=lambda k, v: lines.append((k, v)), timeout=600)
    wl = lab.workload("1.0.0")
    os.environ.setdefault("DOCKER_BUILDKIT", "0")
    res = d.build(wl, req)
    assert res.digest.startswith("sha256:") and Path(res.payload_path).exists()
    assert any(k == "progress" for k, _ in lines)
    built = Workload(dep_id="app", module="ct_compose", version="1.0.0", digest=wl.digest,
                     payload_path=res.payload_path, payload_annotations=res.payload_annotations,
                     health={"timeoutSeconds": 60, "periodSeconds": 0.5}, build={"id": "b1", "resultDigest": res.digest})
    d.apply(None, built)
    ok, why = lab.gate(d, built)
    assert ok, why
    assert d.build(wl, req).digest == res.digest, "the same source builds the same images"

"""Harnesses: CliContainerDriver (docker | podman) and AnkaiosDriver, on fakes (push) or the real runtime (nightly)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from flexengine2.drivers import container as container_mod
from flexengine2.drivers.base import Workload
from flexengine2.drivers.container import AnkaiosDriver, CliContainerDriver

from .base import DEVICE, FAKES, Harness, deployment, digest_of, failing_shim, free_port, shim

HEALTH = {"type": "http", "path": "/healthz", "periodSeconds": 0.15, "successThreshold": 2, "timeoutSeconds": 8}

# minimal flexNode stand-in for the real runtimes (alpine is present locally; no registry pull)
DOCKERFILE = """FROM alpine:3.20
ARG VERSION
LABEL dev.flex.test.version=$VERSION
ARG READY=1
ARG EXIT_AFTER=
ENV READY=$READY EXIT_AFTER=$EXIT_AFTER
COPY run.sh /run.sh
ENTRYPOINT ["/bin/sh", "/run.sh"]
"""
RUN_SH = """#!/bin/sh
trap 'exit 0' TERM INT
if [ "$READY" = "1" ]; then printf 'HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok' > /r
else printf 'HTTP/1.0 503 Service Unavailable\\r\\nContent-Length: 2\\r\\n\\r\\nno' > /r; fi
( while true; do nc -l -p 8080 < /r > /dev/null 2>&1; done ) &
if [ -n "$EXIT_AFTER" ]; then sleep "$EXIT_AFTER"; exit 3; fi
while true; do sleep 1 & wait $!; done
"""


class _ContainerBase(Harness):
    profile = "container"
    runtime = "docker"
    fail_delay = 1.5
    #: EV-39/41 container cases: does `exec` run inside a real container filesystem (named volumes really mounted)?
    real_fs = False

    def setup(self):
        self.device = DEVICE + "-" + os.urandom(3).hex()
        self.calls: list[list[str]] = []
        real = container_mod._run

        def counting(cmd, *a, **kw):  # instrumentation only: record every CLI call
            self.calls.append(list(cmd))
            return real(cmd, *a, **kw)
        self.mp.setattr(container_mod, "_run", counting)

    @property
    def arch(self) -> str:
        """The runtime's architecture (EV-40): the valid workloads' image layer is annotated with it."""
        if not hasattr(self, "_arch"):
            self._arch = CliContainerDriver(self.device, self.runtime).platform()["dev.flex.arch"]
        return self._arch

    def refused_release(self):
        """A release asking for a run option this device does not allow (S4) -> preflight refusal, no transfer."""
        wl = self.workload("1.0.0")
        rel = self.release(wl)
        rel.descriptor["runOptions"] = {"devices": ["/dev/mem"]}
        return rel, deployment(wl.dep_id, wl.module, wl.version, "container")

    def exec_command(self, ok: bool) -> list[str]:
        """A health command that succeeds (in the container) / fails."""
        return ["sh", "-c", "exit 0"] if ok else ["sh", "-c", "exit 3"]

    def option_driver(self, allowed: dict, secrets_dir: str | None = None):
        """A driver with runOptions allowed (EV-41); None when the driver takes no runOptions."""
        return CliContainerDriver(self.device, self.runtime, allowed, secrets_dir=secrets_dir)

    def volumes(self) -> list[str]:
        p = self._rt("volume", "ls", "-q", "--filter", f"label=dev.flex.device={self.device}")
        return sorted(p.stdout.split())

    def exec(self, dep_id: str, *cmd: str) -> subprocess.CompletedProcess:
        return self._rt("exec", f"flex_{self.device}_{dep_id}", *cmd)

    def transfers(self):
        return sum(1 for c in self.calls if len(c) > 1 and c[1] == "load")

    def _rt(self, *args, check=False):
        return subprocess.run([self.runtime, *args], capture_output=True, text=True, timeout=120, check=check)

    def instances(self, dep_id):
        p = self._rt("ps", "-q", "--filter", f"label=dev.flex.device={self.device}",
                     "--filter", f"label=dev.flex.deployment={dep_id}")
        return len(p.stdout.split())

    def destroy_out_of_band(self, dep_id):
        p = self._rt("ps", "-a", "-q", "--filter", f"label=dev.flex.device={self.device}",
                     "--filter", f"label=dev.flex.deployment={dep_id}")
        for cid in p.stdout.split():
            self._rt("rm", "-f", cid)
        return True

    def cleanup(self):
        p = self._rt("ps", "-a", "-q", "--filter", f"label=dev.flex.device={self.device}")
        for cid in p.stdout.split():
            self._rt("rm", "-f", cid)
        for vol in self.volumes():
            self._rt("volume", "rm", "-f", vol)

    @contextmanager
    def unreachable(self):
        bindir = self.tmp / "down-bin"
        for n in ("docker", "podman", "ank"):
            failing_shim(bindir, n, f"Cannot connect to the {n} daemon. Is the daemon running?")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
            yield self.make_driver()

    def make_driver(self):
        return CliContainerDriver(self.device, self.runtime)


# ---- push tier: fake CLI ------------------------------------------------------------------------------------
class FakeCliHarness(_ContainerBase):
    def setup(self):
        super().setup()
        self.state = self.tmp / "fakeoci"
        bindir = self.tmp / "bin"
        shim(bindir, self.runtime, FAKES / "fakeoci.py", {"FAKE_OCI_STATE": self.state, "FAKE_OCI_NAME": self.runtime})
        self.path_prepend(bindir)
        self.port = free_port()

    def _payload(self, version: str, **cfg) -> tuple[str, dict, str]:
        img = {"port": self.port, "ready": True, **cfg}
        body = json.dumps(img, sort_keys=True).encode() + version.encode()
        ref = f"docker.io/flexnode/ct_app@{digest_of(body)}"
        p = self.tmp / "payloads" / f"ct_app-{version}.image.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({"fakeoci": 1, "images": {ref: img}}))
        ann = {"dev.flex.profile": "container", "dev.flex.imageRef": ref, "dev.flex.imageTag": f"flexnode/ct_app:{version}",
               "dev.flex.arch": self.arch}
        return str(p), ann, digest_of(p.read_bytes())

    def workload(self, version="1.0.0", dep_id=None):
        path, ann, dg = self._payload(version)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_app", version=version, digest=dg, payload_path=path,
                        payload_annotations=ann, health={**HEALTH, "port": self.port},
                        restart={"policy": "on-failure", "maxRetries": 2})

    def bad_workload(self, dep_id=None):
        path, ann, dg = self._payload("9.9.9-bad", ready=False, exit_after=self.fail_delay, exit_code=3)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_app", version="9.9.9-bad", digest=dg,
                        payload_path=path, payload_annotations=ann, health={**HEALTH, "port": self.port},
                        restart={"policy": "never"})


class FakeDockerHarness(FakeCliHarness):
    id = "container-docker[fake]"
    driver_cls = CliContainerDriver
    runtime = "docker"


class FakePodmanHarness(FakeDockerHarness):
    id = "container-podman[fake]"
    runtime = "podman"


class FakeAnkaiosHarness(FakeCliHarness):
    id = "container-ankaios[fake-ank+fake-podman]"
    driver_cls = AnkaiosDriver
    runtime = "podman"
    xfail = {"fail_fast": "EV-20: AnkaiosDriver.failed() is not implemented (always None)"}

    def setup(self):
        super().setup()
        bindir = self.tmp / "bin"
        shim(bindir, "ank", FAKES / "fakeank.py", {"FAKE_ANK_STATE": self.tmp / "fakeank"})

    def make_driver(self):
        return AnkaiosDriver(self.device, "ank", "http://127.0.0.1:25551", "agent_A")

    def option_driver(self, allowed, secrets_dir=None):
        return None   # Ankaios takes no runOptions (refused at preflight)

    def exec(self, dep_id, *cmd):
        p = self._rt("ps", "-q", "--filter", f"label=dev.flex.device={self.device}",
                     "--filter", f"label=dev.flex.deployment={dep_id}")
        return self._rt("exec", (p.stdout.split() or ["none"])[0], *cmd)


# ---- nightly tier: real runtime ---------------------------------------------------------------------------
_BUILT: dict[tuple[str, str], tuple[str, str, str]] = {}


def runtime_ok(rt: str) -> bool:
    if not shutil.which(rt):
        return False
    try:
        return subprocess.run([rt, "ps"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class RealCliHarness(_ContainerBase):
    real_fs = True

    def exec_command(self, ok):
        # /run.sh exists only inside the stand-in image: proves the probe runs in the container, not on the host
        return ["sh", "-c", "test -f /run.sh"] if ok else ["sh", "-c", "test -f /no/such/file"]

    def setup(self):
        if not runtime_ok(self.runtime):
            pytest.skip(f"{self.runtime} not installed or not usable (`{self.runtime} ps` failed)")
        super().setup()

    def _image(self, version: str, ready: bool, exit_after: str = "") -> tuple[str, str, str]:
        """Build (once per session) and save an image; returns (archive path, imageRef, manifest digest)."""
        key = (self.runtime, version)
        if key in _BUILT and Path(_BUILT[key][0]).exists():
            return _BUILT[key]
        ctx = Path(os.environ.get("FLEX_CONTRACT_WORK", "/tmp")) / f"flex-contract-img-{self.runtime}-{version}"
        shutil.rmtree(ctx, ignore_errors=True)
        ctx.mkdir(parents=True)
        (ctx / "Dockerfile").write_text(DOCKERFILE)
        (ctx / "run.sh").write_text(RUN_SH)
        repo = f"docker.io/flexnode/ct_app_{self.runtime}"
        tag = f"{repo}:{version}"
        # VERSION makes every version its own image: same content would give the same digest (a build-cache hit
        # under Docker 28's graph-driver store), i.e. two "releases" that are one image
        self._rt("build", "-q", "-t", tag, "--build-arg", f"VERSION={version}",
                 "--build-arg", f"READY={1 if ready else 0}", "--build-arg", f"EXIT_AFTER={exit_after}", str(ctx),
                 check=True)
        tar = ctx / "image.tar"
        fmt = ["--format", "oci-archive"] if self.runtime == "podman" else []
        self._rt("save", *fmt, "-o", str(tar), tag, check=True)
        with tarfile.open(tar) as tf:
            dg = json.load(tf.extractfile("index.json"))["manifests"][0]["digest"]
        self._rt("rmi", "-f", tag)  # the engine loads it from the payload (a transfer the suite can count)
        _BUILT[key] = (str(tar), f"{repo}@{dg}", dg)
        return _BUILT[key]

    def _wl(self, version, ready, exit_after, restart, dep_id):
        tar, ref, dg = self._image(version, ready, exit_after)
        ann = {"dev.flex.profile": "container", "dev.flex.imageRef": ref, "dev.flex.imageTag": f"ct_app:{version}",
               "dev.flex.arch": self.arch}
        return Workload(dep_id=dep_id or self.dep_id, module="ct_app", version=version, digest=dg, payload_path=tar,
                        payload_annotations=ann, health={**HEALTH, "port": 8080, "timeoutSeconds": 30},
                        restart=restart)

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, True, "", {"policy": "on-failure", "maxRetries": 2}, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9-bad", False, "2", {"policy": "never"}, dep_id)


class RealDockerHarness(RealCliHarness):
    id = "container-docker[real]"
    driver_cls = CliContainerDriver
    runtime = "docker"
    fail_delay = 2.0


class RealPodmanHarness(RealDockerHarness):
    id = "container-podman[real]"
    runtime = "podman"


class RealPodmanAnkaiosHarness(RealCliHarness):
    """Ankaios driver with the fake `ank` (server+agent) over the REAL podman: no Ankaios binaries in the lab."""
    id = "container-ankaios[fake-ank+real-podman]"
    driver_cls = AnkaiosDriver
    runtime = "podman"
    fail_delay = 2.0
    xfail = FakeAnkaiosHarness.xfail

    def setup(self):
        super().setup()
        bindir = self.tmp / "bin"
        shim(bindir, "ank", FAKES / "fakeank.py", {"FAKE_ANK_STATE": self.tmp / "fakeank"})
        self.path_prepend(bindir)

    def make_driver(self):
        return AnkaiosDriver(self.device, "ank", "http://127.0.0.1:25551", "agent_A")

    option_driver = FakeAnkaiosHarness.option_driver
    exec = FakeAnkaiosHarness.exec

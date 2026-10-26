"""Harness: ComposeDriver (FE-4, container runtime compose) on the fake compose CLI over the fake docker CLI (push
tier). The real `docker compose` / `podman compose` cases are in test_compose.py (nightly, markers docker / podman)."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager

import pytest

from flexengine2.drivers import container as container_mod
from flexengine2.drivers.base import Workload
from flexengine2.drivers.compose import ComposeDriver, project_name

from .base import FAKES, Harness, deployment, digest_of, failing_shim, free_port, shim, targz
from .container import HEALTH

DEVICE = "ctc"


class FakeComposeHarness(Harness):
    id = "container-compose[fake]"
    driver_cls = ComposeDriver
    profile = "container"
    fail_delay = 1.5
    na = {"exclusive_backstop": "not exclusive", "commit_failure_fails_gate": "no two-phase commit"}

    def setup(self):
        self.device = DEVICE + "-" + os.urandom(3).hex()
        self.calls: list[list[str]] = []
        real = container_mod._run

        def counting(cmd, *a, **kw):
            self.calls.append(list(cmd))
            return real(cmd, *a, **kw)
        import flexengine2.drivers.compose as compose_mod
        self.mp.setattr(compose_mod, "_run", counting)
        self.state = self.tmp / "fakeoci"
        bindir = self.tmp / "bin"
        shim(bindir, "docker", FAKES / "fakeoci.py", {"FAKE_OCI_STATE": self.state, "FAKE_OCI_NAME": "docker"})
        shim(bindir, "fakecompose", FAKES / "fakecompose.py", {"FAKE_OCI_STATE": self.state})
        self.path_prepend(bindir)
        self.port = free_port()

    @property
    def arch(self) -> str:
        if not hasattr(self, "_arch"):
            self._arch = self.make_driver().platform()["dev.flex.arch"]
        return self._arch

    def make_driver(self):
        return ComposeDriver(self.device, self.data, cli="docker", compose=["fakecompose"])

    def _wl(self, version: str, dep_id, restart: str, **cfg) -> Workload:
        img = {"port": self.port, "ready": True, **cfg}
        ref = f"docker.io/flexnode/ct_compose@{digest_of(json.dumps(img, sort_keys=True).encode() + version.encode())}"
        archive = json.dumps({"fakeoci": 1, "images": {ref: img}}).encode()
        compose = (f"services:\n  app:\n    image: {ref}\n    restart: \"{restart}\"\n"
                   f"    healthcheck:\n      test: [\"CMD\", \"true\"]\n").encode()
        data = targz({"compose.yaml": compose, "images/app.tar": archive})
        p = self.tmp / "payloads" / f"ct_compose-{version}.tar.gz"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_compose", version=version, digest=digest_of(data),
                        payload_path=str(p), payload_annotations={"dev.flex.profile": "container",
                                                                  "dev.flex.arch": self.arch},
                        health={**HEALTH, "port": self.port}, restart={})

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, dep_id, "on-failure:2")

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9-bad", dep_id, "no", ready=False, exit_after=self.fail_delay, exit_code=3)

    def refused_release(self):
        """A compose file asking for host access (privileged) on a device that does not allow it."""
        wl = self.workload("1.0.0")
        p = self.tmp / "payloads" / "privileged.tar.gz"
        p.write_bytes(targz({"compose.yaml": b"services:\n  app:\n    image: x\n    privileged: true\n"}))
        rel = self.release(wl)
        rel.payload_path = str(p)
        return rel, deployment(wl.dep_id, wl.module, wl.version, "container")

    def transfers(self):
        return sum(1 for c in self.calls if len(c) > 1 and c[1] == "load")

    def _rt(self, *args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)

    def _ids(self, dep_id=None):
        f = [f"label=com.docker.compose.project={project_name(self.device, dep_id)}"] if dep_id else []
        out = []
        for cid in self._rt("ps", "-a", "-q", *sum((["--filter", x] for x in f), [])).stdout.split():
            out.append(cid)
        return out

    def instances(self, dep_id):
        running = [c for c in self._ids(dep_id)
                   if json.loads(self._rt("container", "inspect", c).stdout)[0]["State"]["Running"]]
        return len(running)

    def destroy_out_of_band(self, dep_id):
        for cid in self._ids(dep_id):
            self._rt("rm", "-f", cid)
        return True

    def cleanup(self):
        for cid in self._ids():
            self._rt("rm", "-f", cid)

    @contextmanager
    def unreachable(self):
        bindir = self.tmp / "down-bin"
        failing_shim(bindir, "docker", "Cannot connect to the Docker daemon. Is the daemon running?")
        failing_shim(bindir, "fakecompose", "Cannot connect to the Docker daemon. Is the daemon running?")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
            yield self.make_driver()

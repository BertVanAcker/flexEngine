"""Harness interface of the driver contract suite: everything executor-specific lives behind it.

A harness adapts one driver to the shared contract cases in test_spi.py. It is built per test with a fresh
temporary directory and pytest's monkeypatch (for PATH / env of fake CLIs and for counting instrumentation that
does not change behaviour), and torn down with `cleanup()`.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import socket
import stat
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from flexengine2.drivers.base import Driver, Workload

FAKES = Path(__file__).resolve().parent / "fakes"
DEVICE = "ct1"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def shim(bindir: Path, name: str, target: Path, env: dict | None = None) -> Path:
    """An executable `name` in `bindir` that runs `target` (a Python script) with the suite's interpreter."""
    bindir.mkdir(parents=True, exist_ok=True)
    p = bindir / name
    exports = "".join(f"export {k}={json.dumps(str(v))}\n" for k, v in (env or {}).items())
    p.write_text(f"#!/bin/sh\n{exports}exec {sys.executable} {target} \"$@\"\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def failing_shim(bindir: Path, name: str, msg: str = "cannot connect: executor unreachable") -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    p = bindir / name
    p.write_text(f"#!/bin/sh\necho {json.dumps(msg)} >&2\nexit 1\n")
    p.chmod(0o755)
    return p


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def targz(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode = len(files[name]), 0, 0o644
            tf.addfile(ti, io.BytesIO(files[name]))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


@dataclass
class Release:
    """What the engine hands to `preflight` (artefacts.store's verified release, reduced to what drivers read)."""
    payload_path: str
    payload_annotations: dict
    descriptor: dict = field(default_factory=dict)


def deployment(dep_id: str, module: str, version: str, driver: str, params: dict | None = None) -> dict:
    """A desired-state deployment entry (only the fields drivers read in preflight)."""
    return {"metadata": {"id": dep_id}, "x-flex-driver": driver,
            "spec": {"applicationId": module, "parameters": dict(params or {}),
                     "deploymentProfile": {"components": [{"properties": {"revision": version}}]}}}


class Harness:
    """Base class. Subclasses set the class attributes and implement `make_driver` and `workload`."""

    id: str = "?"
    driver_cls: type[Driver]
    profile: str = "?"
    #: contract case -> reason, for cases where this driver violates the contract (test marked xfail strict)
    xfail: dict[str, str] = {}
    #: contract case -> reason, for cases that do not apply to this executor (test skipped)
    na: dict[str, str] = {}
    #: seconds after apply at which the "bad" workload visibly fails for good (None: at once / unknown)
    fail_delay: float | None = None
    dep_id = "dep-a"

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = Path(tmp)
        self.mp = monkeypatch
        self.data = self.tmp / "engine-data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.drivers: list[Driver] = []

    # ---- required -------------------------------------------------------------------------------------------
    def setup(self) -> None:
        """Start fakes / check the executor (call pytest.skip when it is missing)."""

    def make_driver(self) -> Driver:
        """A NEW driver instance over the SAME executor and engine data dir (an engine restart)."""
        raise NotImplementedError

    def workload(self, version: str = "1.0.0", dep_id: str | None = None) -> Workload:
        """A valid, healthy workload; '1.0.0' and '2.0.0' must both be available."""
        raise NotImplementedError

    def bad_workload(self, dep_id: str | None = None) -> Workload | None:
        """A workload that passes apply but visibly fails for good (and is never healthy) shortly after."""
        return None

    def cleanup(self) -> None:
        """Remove every workload of this harness from the executor; stop fakes."""

    # ---- optional hooks -------------------------------------------------------------------------------------
    def release(self, wl: Workload) -> Release:
        """The verified release object `preflight` sees for `wl`."""
        return Release(wl.payload_path, wl.payload_annotations, {"health": wl.health, "restartPolicy": wl.restart})

    def refused_release(self) -> tuple[Release, dict] | None:
        """(release, deployment) that preflight must refuse with a reason; None when the driver has no checks."""
        return None

    @contextmanager
    def unreachable(self):
        """While inside, the executor cannot be reached (yields a driver configured for that situation)."""
        raise NotImplementedError

    def transfers(self) -> int:
        """Number of payload transfers to the executor so far (image loads, module uploads, app installs, unpacks)."""
        return 0

    def instances(self, dep_id: str) -> int | None:
        """Number of executor-side instances of the deployment (None: not observable)."""
        return None

    def destroy_out_of_band(self, dep_id: str) -> bool:
        """Delete the workload behind the engine's back (drift); False when not possible for this executor."""
        return False

    def break_commit(self) -> bool:
        """Make the executor refuse the next commit (two-phase executors); False when not possible."""
        return False

    def probe(self, wl: Workload) -> dict:
        return wl.health

    # ---- helpers --------------------------------------------------------------------------------------------
    def new(self) -> Driver:
        d = self.make_driver()
        self.drivers.append(d)
        return d

    def path_prepend(self, d: Path):
        self.mp.setenv("PATH", f"{d}{os.pathsep}{os.environ.get('PATH', '')}")


def ns(**kw):
    return SimpleNamespace(**kw)

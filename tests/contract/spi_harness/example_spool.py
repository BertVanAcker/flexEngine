"""Harness: the SPI guide's minimal example driver (docs/examples/spool_driver.py) with a stand-in player thread.

Kept in the push tier so the example in docs/spi-guide.md keeps passing the conformance suite.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from flexengine2.drivers.base import Workload

from .base import DEVICE, Harness, Release, digest_of

EXAMPLE = Path(__file__).resolve().parents[3] / "docs" / "examples" / "spool_driver.py"


def _load():
    spec = importlib.util.spec_from_file_location("flex_example_spool_driver", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


spool_mod = _load()
SpoolDriver = spool_mod.SpoolDriver


class Player(threading.Thread):
    """The executor stand-in: plays what meta.json names; a payload starting with b"BAD" fails for good."""

    def __init__(self, spool: Path):
        super().__init__(daemon=True)
        self.spool = spool
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(0.03):
            try:
                deps = [p for p in self.spool.iterdir() if p.is_dir()] if self.spool.is_dir() else []
            except OSError:
                continue
            for d in deps:
                try:
                    meta = json.loads((d / "meta.json").read_text())
                    data = (d / "payloads" / meta["payload"]).read_bytes()
                except (OSError, ValueError, KeyError):
                    continue
                st = ({"state": "failed", "digest": meta["digest"], "detail": "payload does not play"}
                      if data.startswith(b"BAD") else {"state": "running", "digest": meta["digest"]})
                try:
                    old = json.loads((d / "state.json").read_text())
                except (OSError, ValueError):
                    old = None
                if old != st:
                    tmp = d / "state.tmp"
                    try:
                        tmp.write_text(json.dumps(st))
                        os.replace(tmp, d / "state.json")
                    except OSError:
                        pass


class SpoolHarness(Harness):
    id = "example-spool"
    driver_cls = SpoolDriver
    profile = "spool"
    na = {"exclusive_backstop": "not exclusive"}

    def setup(self):
        self.spool = self.tmp / "spool"
        self.spool.mkdir()
        self.copies = 0
        real = spool_mod.shutil.copyfile

        def counting_copy(*a, **kw):
            self.copies += 1
            return real(*a, **kw)
        # instrumentation only: count payload copies (this executor's "transfer")
        self.mp.setattr(spool_mod.shutil, "copyfile", counting_copy)
        self.player = Player(self.spool)
        self.player.start()

    def make_driver(self):
        return SpoolDriver(DEVICE, self.spool, max_bytes=4096)

    def _payload(self, data: bytes) -> tuple[str, str]:
        p = self.tmp / "payloads" / digest_of(data)[7:23]
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return str(p), digest_of(data)

    def _wl(self, version, data, dep_id):
        path, dg = self._payload(data)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_spool", version=version, digest=dg,
                        payload_path=path, payload_annotations={"dev.flex.profile": "spool"},
                        health={"periodSeconds": 0.05, "successThreshold": 2, "timeoutSeconds": 5})

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, f"slides {version}".encode(), dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9-bad", b"BAD slides", dep_id)

    def refused_release(self):
        path, _ = self._payload(b"x" * 5000)
        return (Release(path, {"dev.flex.profile": "spool"}, {}),
                {"metadata": {"id": self.dep_id}, "x-flex-driver": "spool", "spec": {"applicationId": "ct_spool"}})

    def transfers(self):
        return self.copies

    def instances(self, dep_id):
        return 1 if (self.spool / dep_id / "meta.json").exists() else 0

    def destroy_out_of_band(self, dep_id):
        shutil.rmtree(self.spool / dep_id)
        return True

    @contextmanager
    def unreachable(self):
        away = self.tmp / "spool-unmounted"
        self.spool.rename(away)
        try:
            yield self.new()
        finally:
            away.rename(self.spool)

    def cleanup(self):
        self.player.stop.set()
        self.player.join(timeout=2)

"""Harness: ProcessDriver (profile native) -- real local processes, no fake needed."""

from __future__ import annotations

import json
import os
import signal
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from flexengine2.drivers import process as process_mod
from flexengine2.drivers.base import Workload
from flexengine2.drivers.native import pyenv
from flexengine2.drivers.process import ProcessDriver

from .base import DEVICE, Harness, Release, digest_of, targz

APP = r'''"""contract-suite native app: GET /healthz on 127.0.0.1:$FLEX_HEALTH_PORT"""
import json, os, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
REL = json.loads((Path(__file__).parent / "release.json").read_text())
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = REL["ready"] and self.path == "/healthz"
        self.send_response(200 if ok else 503); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok" if ok else b"no")
    def log_message(self, *a): pass
HTTPServer.allow_reuse_address = True
srv = HTTPServer(("127.0.0.1", int(os.environ["FLEX_HEALTH_PORT"])), H)
signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
threading.Thread(target=srv.serve_forever, daemon=True).start()
if REL.get("exit_after"):
    time.sleep(REL["exit_after"]); os._exit(REL.get("exit_code", 1))
threading.Event().wait()
'''


def native_annotations() -> dict:
    """Layer annotations of a valid native payload for this host's interpreter (EV-37: platform() must match)."""
    return {"dev.flex.profile": "native", **pyenv.platform_of(pyenv.interpreter_info())}


def _markers(device: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            env = (p / "environ").read_bytes().split(b"\0")
            st = (p / "stat").read_text()
        except OSError:
            continue
        if st.split(") ")[-1].startswith("Z"):
            continue
        for e in env:
            if e.startswith(f"FLEX_UNIT={device}/".encode()):
                out.setdefault(e.decode().split("/", 1)[1], []).append(int(p.name))
    return out


class ProcessHarness(Harness):
    id = "native-process"
    driver_cls = ProcessDriver
    profile = "native"
    fail_delay = 1.5
    na = {"unreachable": "local processes: the executor cannot be unreachable",
          "drift_repair": "no out-of-band removal for a process unit (a killed process is supervised, not missing)"}

    def setup(self):
        self.device = DEVICE + "-" + os.urandom(3).hex()
        self.n_extract = 0
        real_open = tarfile.open

        def counting_open(*a, **kw):
            self.n_extract += 1
            return real_open(*a, **kw)
        # instrumentation only: count payload unpacks (the process driver's "transfer" to its executor)
        self.mp.setattr(process_mod, "tarfile", SimpleNamespace(open=counting_open))

    def make_driver(self):
        return ProcessDriver(self.device, self.data)

    def _payload(self, version: str, **rel) -> tuple[str, str]:
        data = targz({"app.py": APP.encode(), "release.json": json.dumps({"version": version, "ready": True, **rel}).encode()})
        p = self.tmp / "payloads" / f"native-{version}-{digest_of(data)[7:19]}.tar.gz"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return str(p), digest_of(data + version.encode())

    def workload(self, version="1.0.0", dep_id=None):
        path, dg = self._payload(version)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_native", version=version, digest=dg,
                        payload_path=path, payload_annotations=native_annotations(),
                        health={"type": "http", "path": "/healthz", "periodSeconds": 0.1, "successThreshold": 2,
                                "timeoutSeconds": 10},
                        restart={"policy": "on-failure", "maxRetries": 2, "restartSec": 0.2})

    def bad_workload(self, dep_id=None):
        path, dg = self._payload("9.9.9-bad", ready=False, exit_after=self.fail_delay, exit_code=3)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_native", version="9.9.9-bad", digest=dg,
                        payload_path=path, payload_annotations=native_annotations(),
                        health={"type": "http", "path": "/healthz", "periodSeconds": 0.1, "successThreshold": 2,
                                "timeoutSeconds": 10},
                        restart={"policy": "never"})

    def refused_release(self):
        """EV-37: a layer built for another CPU (preflight refuses it before anything is unpacked)."""
        path, _ = self._payload("1.0.0")
        other = "aarch64" if pyenv.interpreter_info()["arch"] != "aarch64" else "x86_64"
        rel = Release(path, {**native_annotations(), "dev.flex.arch": other}, {"health": {}, "restartPolicy": {}})
        return rel, {"metadata": {"id": self.dep_id}, "x-flex-driver": "native",
                     "spec": {"applicationId": "ct_native", "parameters": {},
                              "deploymentProfile": {"components": [{"properties": {"revision": "1.0.0"}}]}}}

    def transfers(self):
        return self.n_extract

    def instances(self, dep_id):
        return len(_markers(self.device).get(dep_id, []))

    def cleanup(self):
        for d in self.drivers:
            for dep in list(getattr(d, "units", {})):
                try:
                    d.remove(dep)
                except Exception:  # noqa: BLE001
                    pass
        for pids in _markers(self.device).values():
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        time.sleep(0.05)

    @contextmanager
    def unreachable(self):
        raise NotImplementedError

"""EV-35 lab: reference fake cloud, `flex import-flexia` as a subprocess, a real engine
(`python -m flexengine2.testing.single`) with the container driver on real Docker and the native process driver."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer

HERE = Path(__file__).resolve().parent
PACKAGES = HERE / "packages"
FLEXENGINE = HERE.parents[2]
REPO = FLEXENGINE.parent
WORK = FLEXENGINE / "tests" / "work" / "flexia"


def flex_env() -> dict:
    """Environment for the importer subprocess: this checkout's flexmodel and flexengine2 first on the path."""
    pp = [str(REPO / "flexmodel" / "src"), str(FLEXENGINE / "src")]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(pp + [os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}


def import_flexia(pkg: Path, out: Path, *args: str, timeout: float = 1800) -> tuple[int, dict, str]:
    p = subprocess.run([sys.executable, "-m", "flexmodel.cli", "import-flexia", str(pkg), "--out", str(out), "--json",
                        *args], capture_output=True, text=True, timeout=timeout, env=flex_env())
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = {}
    return p.returncode, doc, p.stderr


class Cloud:
    def __init__(self, tmp: Path, key):
        self.srv = CloudServer(tmp / "cloud", 0, "admin-secret", key).start()
        self.url, self.st = self.srv.url, self.srv.state
        self.token_file = tmp / "admin-token"
        self.token_file.write_text("admin-secret")

    def admin(self, method: str, path: str, body=None, user: str = "carol"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
                  headers={"Authorization": "Bearer admin-secret", "X-Flex-User": user,
                           "Content-Type": "application/json", "Connection": "close"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else {})

    def onboarding_token(self, dev: str) -> str:
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def deploy(self, dev: str, deployments: list[dict]) -> dict:
        s, p = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": deployments})
        assert s == 201, p
        if p["requiresApproval"]:
            s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, "dave")
            assert s == 200, b
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        return {**b, "plan": p}

    def status(self, dev: str) -> dict:
        return self.admin("GET", f"/api/v1/admin/devices/{dev}")[1].get("status") or {}

    def wait(self, pred, timeout: float, what: str):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.25)
        raise AssertionError(f"timeout waiting for {what}")

    def converged(self, dev: str, expect: dict[str, str]):
        st = self.status(dev)
        for dep, ver in expect.items():
            r = st.get(dep)
            if not r or r["state"] != "Installed" or r.get("version") != ver or r.get("health") != "healthy":
                return None
        return st

    def stop(self):
        self.srv.stop()


class Engine:
    def __init__(self, tmp: Path, cloud: Cloud, cloud_key, release_pub: str, dev: str):
        self.dev, self.dir = dev, tmp / f"dev-{dev}"
        self.dir.mkdir(parents=True)
        safe = self.dir / "machine_state.json"
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        self.cfg = {"deviceId": dev, "apiUrl": cloud.url, "dataDir": str(self.dir / "data"),
                    "onboardingToken": cloud.onboarding_token(dev), "cloudPublicKey": crypto.public_b64(cloud_key),
                    "releasePublicKeys": [release_pub], "pollInterval": 0.5, "backoff": [0.2, 2.0],
                    "safeStateFile": str(safe), "httpTimeout": 5,
                    "drivers": {"container": {"runtime": "docker"}, "native": True}}
        (self.dir / "engine.json").write_text(json.dumps(self.cfg, indent=1))
        self.log = self.dir / "engine.log"
        self.proc = subprocess.Popen([sys.executable, "-m", "flexengine2.testing.single", "--config",
                                      str(self.dir / "engine.json")], cwd=self.dir, stdout=open(self.log, "ab"),
                                     stderr=subprocess.STDOUT, env={**flex_env(), "PYTHONUNBUFFERED": "1"})

    def units(self) -> dict:
        p = self.dir / "data" / "native" / "units.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=15)
        ids = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"label=dev.flex.device={self.dev}"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
        for u in self.units().values():
            try:
                os.killpg(u["pid"], 9)
            except (ProcessLookupError, TypeError, PermissionError, KeyError):
                pass


def container_ip(name: str) -> str:
    out = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)[0]["NetworkSettings"]
    return info.get("IPAddress") or next(n["IPAddress"] for n in info["Networks"].values() if n.get("IPAddress"))


def http_get(host: str, port: int, path: str) -> tuple[int, bytes]:
    c = http.client.HTTPConnection(host, port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read()


def flcpy_wheels() -> Path | None:
    """flexCommunicator + dependencies as CPython 3.13 x86_64 musllinux wheels for the flcpy Dockerfile (python:3.13-alpine;
    cached; network once)."""
    d = WORK / "flcpy-wheels-cp313-musl"
    if any(d.glob("flexcommunicator-*.whl")):
        return d
    d.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-q", "--dest", str(d), "--only-binary=:all:",
           "--implementation", "cp", "--python-version", "3.13", "--platform", "musllinux_1_1_x86_64",
           "--platform", "musllinux_1_2_x86_64", "flexCommunicator"]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok or not any(d.glob("flexcommunicator-*.whl")):
        shutil.rmtree(d, ignore_errors=True)
        return None
    return d

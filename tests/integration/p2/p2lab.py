"""P2 lab helpers (ported from probes/p2-reconciling-engine/tests/conftest.py): flexCloud stub wrapper, the
engine as a real subprocess (so it can be SIGKILLed), container inspection, Ankaios binaries, metrics."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing.release import PROBES, REPO
from flexengine2.util import read_json

FLEXENGINE = Path(__file__).resolve().parents[3]
PROBE = PROBES / "p2-reconciling-engine"          # read-only: cached payloads, Ankaios binaries
WORK = FLEXENGINE / "tests" / "work" / "p2"                  # gitignored
RESULTS = FLEXENGINE / "results"
MODEL = REPO / "examples" / "models" / "uc1"
METRICS: dict = {}
ANK_DIR = Path(os.environ["FLEX_ANKAIOS_DIR"]) if os.environ.get("FLEX_ANKAIOS_DIR") else PROBE / ".cache" / "ankaios"
ENGINE_CMD = ["-m", "flexengine2.testing.single"]           # was: -m flexp2.engine


def metric(name: str, value):
    METRICS[name] = value


class Cloud:
    def __init__(self, tmp: Path, keys, registry):
        self.dir = tmp / "cloud"
        self.dir.mkdir(parents=True)
        os.symlink(registry["dir"], self.dir / "oci")  # shared, read-mostly registry
        # the stub derives safety classes from the same model and refuses mismatching releases/plans (S4)
        self.srv = CloudServer(self.dir, 0, "admin-secret", keys["cloud"], model_dirs=[MODEL]).start()
        self.url = self.srv.url
        self.st = self.srv.state
        self.registry = registry

    def admin(self, method: str, path: str, body=None, user: str = "carol"):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else b""
        c.request(method, path, body=data, headers={"Authorization": "Bearer admin-secret", "X-Flex-User": user,
                                                     "Content-Type": "application/json", "Connection": "close"})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out and r.getheader("Content-Type", "").endswith("json") else out)

    def token(self, dev: str) -> str:
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def plan(self, dev: str, deployments: list[dict], user: str = "carol"):
        s, b = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": deployments}, user)
        assert s == 201, b
        return b

    def deploy(self, dev: str, deployments: list[dict], approver: str | None = None) -> dict:
        self.wait(lambda: dev in self.st.s["devices"], 30, what=f"{dev} onboarded")
        p = self.plan(dev, deployments)
        if approver is None and p["requiresApproval"]:
            approver = "dave"  # safety-class plan (model-derived): second person approves
        if approver:
            s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, approver)
            assert s == 200, b
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        b["publishedAt"] = time.time()
        b["plan"] = p
        return b

    def device(self, dev: str) -> dict:
        s, b = self.admin("GET", f"/api/v1/admin/devices/{dev}")
        return b

    def status(self, dev: str) -> dict:
        return self.device(dev)["status"]

    def wait(self, pred, timeout: float = 60, period: float = 0.1, what: str = ""):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(period)
        raise AssertionError(f"timeout waiting for {what or pred}")

    def converged(self, dev: str, expect: dict[str, str]) -> float | None:
        """All expected deployments reported Installed+healthy at the expected version, nothing else
        left installed. Returns the cloud receive time of the last needed report."""
        st = self.status(dev)
        last = 0.0
        for dep, ver in expect.items():
            r = st.get(dep)
            if not r or r["state"] != "Installed" or r.get("version") != ver or r.get("health") == "unhealthy":
                return None
            last = max(last, r["receivedAt"])
        for dep, r in st.items():
            if dep not in expect:
                if r["state"] != "Removed":
                    return None
                last = max(last, r["receivedAt"])
        return last or time.time()

    def wait_converged(self, dev, expect, timeout=90):
        return self.wait(lambda: self.converged(dev, expect), timeout, what=f"{dev} -> {expect}")

    def stop(self):
        self.srv.stop()


def item(module: str, version: str, driver: str, dep_id: str | None = None, **kw) -> dict:
    return {"id": dep_id or module.replace("_", "-"), "module": module, "version": version, "driver": driver, **kw}


class EngineProc:
    def __init__(self, tmp: Path, cloud: Cloud, keys, dev: str, api_url: str | None = None,
                 container: dict | None = None, poll: float = 0.5, extra: dict | None = None):
        self.dev = dev
        self.dir = tmp / f"dev-{dev}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.safe_file = self.dir / "machine_state.json"
        self.safe_file.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        self.cfg = {
            "deviceId": dev, "apiUrl": api_url or cloud.url, "dataDir": str(self.dir / "data"),
            "onboardingToken": cloud.token(dev), "cloudPublicKey": crypto.public_b64(keys["cloud"]),
            "releasePublicKeys": [crypto.public_b64(keys["release"])], "pollInterval": poll,
            "backoff": [0.2, 2.0], "safeStateFile": str(self.safe_file), "httpTimeout": 3,
            "drivers": {"container": container or {"runtime": "docker"}, "native": True},
            **(extra or {})}
        self.cfg_path = self.dir / "engine.json"
        self.cfg_path.write_text(json.dumps(self.cfg, indent=1))
        self.proc = None
        self.log_path = self.dir / "engine.log"

    def start(self, *extra_args):
        self.proc = subprocess.Popen([sys.executable, *ENGINE_CMD, "--config", str(self.cfg_path), *extra_args],
                                     cwd=self.dir, stdout=open(self.log_path, "ab"), stderr=subprocess.STDOUT,
                                     env={**os.environ, "PYTHONUNBUFFERED": "1"})
        return self

    def kill(self, sig=signal.SIGKILL):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            self.proc.wait(timeout=15)

    def set_safe(self, **kw):
        cur = json.loads(self.safe_file.read_text())
        cur.update(kw)
        self.safe_file.write_text(json.dumps(cur))

    def actions(self) -> list[dict]:
        p = self.dir / "data" / "actions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def state(self) -> dict:
        return read_json(self.dir / "data" / "state.json", {})

    def units(self) -> dict:
        return read_json(self.dir / "data" / "native" / "units.json", {})

    def rss_kb(self) -> int:
        for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
            if line.startswith("VmRSS"):
                return int(line.split()[1])
        return 0

    def cleanup(self, runtime: str = "docker"):
        self.kill()
        rts = {"docker", "podman"} if runtime != "docker" else {"docker"}
        for rt in rts:
            if shutil.which(rt):
                ids = subprocess.run([rt, "ps", "-a", "-q", "--filter", f"label=dev.flex.device={self.dev}"],
                                     capture_output=True, text=True).stdout.split()
                if ids:
                    subprocess.run([rt, "rm", "-f", *ids], capture_output=True)
        for u in self.units().values():
            try:
                os.killpg(u["pid"], signal.SIGKILL)
            except (ProcessLookupError, TypeError, PermissionError):
                pass


def running_version(dev: str, dep: str, runtime: str = "docker") -> str | None:
    p = subprocess.run([runtime, "inspect", "-f", '{{index .Config.Labels "dev.flex.version"}} {{.State.Running}}',
                        f"flex_{dev}_{dep}"], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    v, running = p.stdout.split()
    return v if running == "true" else None


def ankaios_binaries() -> Path | None:
    if (ANK_DIR / "ank-server").exists():
        return ANK_DIR
    import platform
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "none")
    tgz = PROBE / ".cache" / f"ankaios-1.0.4-linux-{arch}.tar.gz"
    out = WORK / "ankaios"  # extract into our work dir, never into the probe
    if (out / "ank-server").exists():
        return out
    if tgz.exists():
        out.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz) as tf:
            tf.extractall(out, filter="data")
        return out
    return None

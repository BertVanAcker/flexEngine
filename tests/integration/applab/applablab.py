"""P4 lab on flexEngineV2 (port of probes/p4-applab-target/tests/conftest.py).

Build pipeline (session; P4's generators, run into tests/work), simulated board (the real arduino-router + host-built
sketches), the fake arduino-app-cli, the reference cloud fake, and the engine under test: `flexengine run` with one
device that has the `applab` driver, as a real subprocess.

Stand-ins imported read-only from the probe: `flexp4.pipeline` / `ardrelease` (App + .ard + release wrapping),
`flexp4.simboard`, the fake CLI (`tools/fakebin`, run by P4's venv, which holds the App Lab Python runtime the apps
import), `flexp4.rpc` as the test oracle, `flexp4.publish`. The engine never sees them (checked per engine).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
import sys

import yaml

from flexengine2.testing.release import PROBES

FLEXENGINE = Path(__file__).resolve().parents[3]
PROBE = PROBES / "p4-applab-target"
WORK = FLEXENGINE / "tests" / "work" / "applab"
RESULTS = Path(os.environ.get("FLEX_RESULTS_DIR") or FLEXENGINE / "results" / "latest")   # per run, gitignored
FAKEBIN = PROBE / "tools" / "fakebin"
BOARD_PY = PROBE / ".venv" / "bin" / "python"   # board-side Python (arduino.app_utils, router bridge)
METRICS: dict = {}

import pytest  # noqa: E402

if not (PROBE / "flexp4").is_dir():   # stand-ins from the framework (probes, demo/), not vendored: they import flexmodel
    pytest.skip("needs a modular-interoperable-framework checkout with probe P4 (App Lab): set FLEX_REPO",
                allow_module_level=True)
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))  # test stand-ins only

from flexp4 import hostbuild  # noqa: E402
from flexp4.simboard import SimBoard, have_toolchain  # noqa: E402

from flexengine2.identity import crypto  # noqa: E402
from flexengine2.testing.edge_lab import HostProc, admin  # noqa: E402
from flexengine2.testing.fakecloud import CloudServer  # noqa: E402
from flexengine2.util import read_json  # noqa: E402

# host-built sketch cache: keep it out of the probe tree (seeded from the probe's cache, read-only)
_CACHE = WORK / "host-cache"
if not _CACHE.exists() and hostbuild.CACHE.exists():
    shutil.copytree(hostbuild.CACHE, _CACHE)
hostbuild.CACHE = _CACHE


def metric(name: str, value):
    METRICS[name] = value


def board_sim_missing() -> str | None:
    if not have_toolchain():
        return "needs the arduino-router build (probes/p4-applab-target/.ref/bin) and g++"
    if not BOARD_PY.exists():
        return f"{BOARD_PY} missing (P4 venv with the App Lab Python runtime: probes/p4-applab-target/run.sh)"
    return None


# ---- simulated board + fake arduino-app-cli -------------------------------------------------------

class Board:
    def __init__(self, tmp: Path):
        self.dir = tmp / "board"
        self.sim = SimBoard(self.dir, None)  # router only; the fake CLI "flashes" the MCU process
        self.sim.set_analog("A0", 200)        # 20.0 degC
        self.sim.set_analog("A1", 500)
        self.sim.start()
        self.env = {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                    "ARDUINO_APP_CLI__DATA_DIR": str(tmp / "appcli"),
                    "ARDUINO_APP_CLI__APPS_DIR": str(tmp / "ArduinoApps"),
                    "FAKE_APPCLI_BOARD": "unoq",
                    "FAKE_APPCLI_ROUTER": str(self.sim.sock),
                    "FAKE_APPCLI_UART": str(self.sim.tty),
                    "FAKE_APPCLI_SIM_DIR": str(self.sim.sim),
                    "FAKE_APPCLI_PYTHON": str(BOARD_PY),
                    "PATH": f"{FAKEBIN}:{os.environ['PATH']}"}
        self.state_path = tmp / "appcli" / "fake-state.json"

    def cli(self, *args, check=True) -> dict | str:
        r = subprocess.run([str(FAKEBIN / "arduino-app-cli"), *args, "--format", "json"], capture_output=True,
                           text=True, env=self.env, timeout=120)
        if check and r.returncode != 0:
            raise AssertionError(f"arduino-app-cli {args}: rc={r.returncode} {r.stderr}")
        if r.returncode != 0:
            return {"rc": r.returncode, "error": json.loads(r.stderr.strip().splitlines()[-1])["error"]}
        try:
            return json.loads(r.stdout)
        except ValueError:
            return r.stdout

    def ps(self) -> dict:
        return {a.get("release_id") or a["name"]: a["status"] for a in self.cli("app", "ps", "--all")["apps"]}

    def io(self) -> list[str]:
        p = Path(self.env["ARDUINO_APP_CLI__DATA_DIR"]) / "mcu.log"
        return [l for l in p.read_text().splitlines() if l.startswith("IO ")] if p.exists() else []

    def stop(self):
        st = read_json(self.state_path, {})
        pids = [a.get("pid") for a in st.get("apps", {}).values()] + [(st.get("mcu") or {}).get("pid")]
        for pid in pids:
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.sim.stop()


# ---- cloud fake + flexEngineV2 with the applab driver ----------------------------------------------------

class Cloud:
    def __init__(self, tmp: Path, keys, registry, model_dir: Path):
        self.dir = tmp / "cloud"
        self.dir.mkdir(parents=True)
        os.symlink(registry["dir"], self.dir / "oci")
        self.srv = CloudServer(self.dir, 0, "admin-secret", keys["cloud"], model_dirs=[model_dir]).start()
        self.url = self.srv.url
        self.st = self.srv.state

    def admin(self, method, path, body=None, user="carol"):
        return admin(self.srv.port, method, path, body, user, timeout=10)

    def token(self, dev):
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def deploy(self, dev, deployments):
        self.wait(lambda: dev in self.st.s["devices"], 30, what=f"{dev} onboarded")
        s, p = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": deployments})
        assert s == 201, p
        if p["requiresApproval"]:
            s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, "dave")
            assert s == 200, b
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        b["publishedAt"] = time.time()
        b["plan"] = p
        return b

    def status(self, dev):
        s, b = self.admin("GET", f"/api/v1/admin/devices/{dev}")
        return b["status"]

    def history(self, dev):
        return [r for r in self.st.s["statusLog"] if r.get("deviceId") == dev] if "statusLog" in self.st.s else []

    def wait(self, pred, timeout=60, period=0.1, what=""):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(period)
        raise AssertionError(f"timeout waiting for {what or pred}")

    def wait_state(self, dev, dep, pred, timeout=90, what=""):
        return self.wait(lambda: (lambda r: r if r and pred(r) else None)(self.status(dev).get(dep)), timeout,
                         what=what or f"{dev}/{dep}")

    def stop(self):
        self.srv.stop()


class EngineProc:
    """`flexengine run` with one device (the UNO Q) whose only driver is `applab` (probe: flexp4.engine_main)."""

    def __init__(self, tmp: Path, cloud: Cloud, keys, board: Board, dev: str, applab: dict | None = None):
        self.dev = dev
        self.dir = tmp / f"dev-{dev}"
        self.dir.mkdir(parents=True, exist_ok=True)
        safe = self.dir / "machine_state.json"
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        self.cfg = {
            "cloud": {"apiUrl": cloud.url, "cloudPublicKey": crypto.public_b64(keys["cloud"]),
                      "releasePublicKeys": [crypto.public_b64(keys["release"])], "pollInterval": 0.5,
                      "backoff": [0.2, 2.0], "httpTimeout": 3},
            "dataDir": str(self.dir),
            "devices": [{
                "deviceId": dev, "dataDir": str(self.dir / "data"), "onboardingToken": cloud.token(dev),
                "safeStateFile": str(safe),
                "drivers": {"native": False, "applab": {"board": "unoq", "cli": str(FAKEBIN / "arduino-app-cli"),
                                                        "routerSocket": str(board.sim.sock), "healthVia": "router",
                                                        **(applab or {})}}}]}
        self.cfg_path = self.dir / "engine.yaml"
        self.cfg_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False))
        self.host = HostProc(self.cfg_path, self.dir / "engine.log", env=board.env, cwd=self.dir)

    def start(self):
        self.host.start()
        return self

    def kill(self):
        self.host.kill()

    def stop(self):
        """SIGTERM, as systemd stops the service (the engine exits cleanly; SIGKILL only after a timeout)."""
        self.host.stop()

    def actions(self):
        p = self.dir / "data" / "actions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def cli_calls(self):
        p = self.dir / "data" / "applab" / "cli-calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def log(self):
        return self.host.log()


def item(version: str, dep_id: str = "greenhouse", **kw) -> dict:
    return {"id": dep_id, "module": "uc3_greenhouse", "version": version, "driver": "applab", **kw}


import pytest  # noqa: E402

requires_board_sim = pytest.mark.skipif(board_sim_missing() is not None, reason=str(board_sim_missing()))

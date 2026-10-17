"""Harness: ApplabDriver over the P4 fake `arduino-app-cli` (probes/p4-applab-target/flexp4/fakecli.py, read-only).

The release archives (.ard) are built here: a Python part that prints the FLEX01 handshake line the driver's
`healthVia: logs` check looks for, plus a stand-in "sketch" (prebuild/flex-host-mcu) the fake CLI "flashes".
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from flexengine2.drivers.applab.driver import ApplabDriver
from flexengine2.drivers.base import Workload

from .base import DEVICE, Harness, digest_of, failing_shim, shim

REPO = Path(__file__).resolve().parents[4]
FAKECLI = REPO / "probes" / "p4-applab-target" / "flexp4" / "fakecli.py"
HASH = "FLEX01_" + "ab" * 32

MAIN_PY = """import sys, time
from flex_bridge import CONTRACT_HASH
READY, EXIT_AFTER = {ready}, {exit_after}
if READY:
    print("FLEX_CONTRACT_OK " + CONTRACT_HASH, flush=True)
else:
    print("FLEX_CONTRACT_MISMATCH " + CONTRACT_HASH, flush=True)
if EXIT_AFTER:
    time.sleep(EXIT_AFTER)
    sys.exit(3)
while True:
    time.sleep(1)
"""
FIRMWARE = "#!/bin/sh\nln -sf /dev/null \"$FLEX_PTY_LINK\"\nexec sleep 100000\n"


def build_ard(out: Path, rel: str, *, ready: bool = True, exit_after: float | None = None, board: str = "unoq") -> Path:
    files = {
        "release.yaml": (yaml.safe_dump({"schema": 1, "name": "ct_app", "release_label": rel, "icon": "",
                                         "target": board, "created_at": "2026-10-17T00:00:00Z"}).encode(), 0o644),
        "prebuild/bricks-list.yaml": (b"bricks: []\n", 0o644),
        "prebuild/models-list.yaml": (b"models: []\n", 0o644),
        "prebuild/flex-host-mcu": (FIRMWARE.encode(), 0o755),
        "src/app.yaml": (b"name: ct_app\ndescription: contract suite app\n", 0o644),
        "src/python/main.py": (MAIN_PY.format(ready=ready, exit_after=exit_after).encode(), 0o644),
        "src/python/flex_bridge.py": (f'CONTRACT_HASH = "{HASH}"\n'.encode(), 0o644),
        "src/sketch/flex_bridge.h": (f'static const char CONTRACT_HASH[] = "{HASH}";\n'.encode(), 0o644),
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, (data, mode) in files.items():
            ti = tarfile.TarInfo(f"{rel}/{name}")
            ti.size, ti.mode, ti.mtime = len(data), mode, 0
            tf.addfile(ti, io.BytesIO(data))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{rel}.ard"
    p.write_bytes(buf.getvalue())
    return p


class ApplabHarness(Harness):
    id = "applab[fake-cli]"
    driver_cls = ApplabDriver
    profile = "applab"
    fail_delay = 1.5
    na = {"preflight_refusal": "the applab driver checks archives in apply, it has no preflight"}

    def setup(self):
        if not FAKECLI.exists():
            pytest.skip(f"P4 fake arduino-app-cli not found at {FAKECLI}")
        self.device = DEVICE
        board = self.tmp / "board"
        self.env = {"ARDUINO_APP_CLI__DATA_DIR": board / "data", "ARDUINO_APP_CLI__APPS_DIR": board / "apps",
                    "ARDUINO_APP_CLI__RELEASES_DIR": board / "releases", "FAKE_APPCLI_BOARD": "unoq",
                    "FAKE_APPCLI_UART": board / "uart", "FAKE_APPCLI_ROUTER": board / "router.sock"}
        self.cli = shim(self.tmp / "bin", "arduino-app-cli", FAKECLI, self.env)

    def make_driver(self):
        return ApplabDriver(self.device, self.data, {"cli": str(self.cli), "healthVia": "logs", "cliTimeout": 60})

    def _wl(self, rel: str, version: str, dep_id: str | None, **kw) -> Workload:
        ard = build_ard(self.tmp / "payloads", rel, **kw)
        return Workload(dep_id=dep_id or self.dep_id, module="ct_app", version=version,
                        digest=digest_of(ard.read_bytes()), payload_path=str(ard),
                        payload_annotations={"dev.flex.profile": "applab", "dev.flex.releaseName": rel,
                                             "dev.flex.contractHash": HASH},
                        health={"type": "applab", "periodSeconds": 0.2, "successThreshold": 2, "timeoutSeconds": 20})

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(f"ct-app-{version.replace('.', '-')}-unoq", version, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("ct-app-9-9-9-bad-unoq", "9.9.9-bad", dep_id, ready=False, exit_after=self.fail_delay)

    def _calls(self) -> list[dict]:
        p = self.data / "applab" / "cli-calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def transfers(self):
        return sum(1 for c in self._calls() if c["args"][:2] == ["app", "install"])

    def _ps(self) -> dict:
        import subprocess
        p = subprocess.run([str(self.cli), "app", "ps", "--all", "--format", "json"], capture_output=True, text=True,
                           timeout=60)
        return {a.get("release_id") or a["id"]: a for a in json.loads(p.stdout or "{}").get("apps", [])}

    def instances(self, dep_id):
        d = self.drivers[-1] if self.drivers else self.make_driver()
        rels = set((d.units.get(dep_id) or {}).get("installed", []))
        return sum(1 for r, a in self._ps().items() if r in rels and a.get("status") in ("running", "starting"))

    def destroy_out_of_band(self, dep_id):
        import subprocess
        d = self.make_driver()
        rel = (d.units.get(dep_id) or {}).get("release")
        if rel:
            subprocess.run([str(self.cli), "app", "destroy", f"release:{rel}", "--format", "json"],
                           capture_output=True, text=True, timeout=60)
        return True

    def cleanup(self):
        import subprocess
        for r in self._ps():
            subprocess.run([str(self.cli), "app", "destroy", f"release:{r}", "--format", "json"], capture_output=True,
                           timeout=60)
        st = Path(self.env["ARDUINO_APP_CLI__DATA_DIR"]) / "fake-state.json"
        if st.exists():
            mcu = (json.loads(st.read_text()).get("mcu") or {}).get("pid")
            if mcu:
                try:
                    os.killpg(mcu, 9)
                except (ProcessLookupError, PermissionError):
                    pass

    @contextmanager
    def unreachable(self):
        down = failing_shim(self.tmp / "down-bin", "arduino-app-cli", '{"error": "board not reachable"}')
        yield ApplabDriver(self.device, self.data, {"cli": str(down), "healthVia": "logs", "cliTimeout": 10})

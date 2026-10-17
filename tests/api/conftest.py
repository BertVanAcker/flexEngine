"""Fixtures for the connection-point contract tests (EV-00, layer 3 of docs/flexengine-v2-plan.md §7.2)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from flexengine2.drivers.base import Driver, Observed

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parents[1]
REPO = FLEXENGINE.parent
GOLDEN = HERE / "golden"
API_DOCS = FLEXENGINE / "api"
VECTORS = API_DOCS / "vectors"
TOOLS = API_DOCS / "tools"
SRC = FLEXENGINE / "src" / "flexengine2"


def tool(name: str):
    """Import a script from flexengine/api/tools (reasonscan, gen_reason_codes, gen_vectors)."""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(f"_api_tool_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve(ref: str) -> Path:
    kind, _, rel = ref.partition(":")
    return {"golden": GOLDEN, "vectors": VECTORS}[kind] / rel


def load_ref(ref: str):
    return json.loads(resolve(ref).read_bytes())


def recorded(pattern: str) -> list[Path]:
    """Work-dir recordings of earlier runs (gitignored; absent on a fresh checkout)."""
    return sorted(p for p in REPO.glob(pattern) if ".venv" not in p.parts)


class FakeDriver(Driver):
    """In-memory executor with a real profile name (the schema only knows real profiles)."""

    def __init__(self, profile: str = "native", healthy: bool = True):
        self.profile, self.name = profile, "fake"
        self.running: dict[str, dict] = {}
        self.healthy = healthy
        self.calls: list[tuple] = []

    def plan(self, current, desired):
        return [f"fake apply {desired.version}"] if desired else [f"fake remove {current.dep_id}"]

    def apply(self, current, desired):
        self.calls.append(("apply", desired.dep_id, desired.version))
        self.running[desired.dep_id] = {"digest": desired.digest, "version": desired.version}

    def remove(self, dep_id):
        self.calls.append(("remove", dep_id))
        self.running.pop(dep_id, None)

    def status(self, dep_id):
        r = self.running.get(dep_id)
        return Observed(present=bool(r), running=bool(r), digest=(r or {}).get("digest"), version=(r or {}).get("version"))

    def list(self):
        return {k: self.status(k) for k in self.running}

    def health(self, dep_id, probe):
        return self.healthy and dep_id in self.running


@pytest.fixture
def engine_cfg(tmp_path):
    def make(**kw):
        cfg = {"deviceId": "vec-dev", "dataDir": str(tmp_path / "data"), "apiUrl": "http://127.0.0.1:9",
               "releasePublicKeys": [], "cloudPublicKey": "", "httpTimeout": 0.5}
        cfg.update(kw)
        return cfg
    return make


@pytest.fixture(scope="session")
def vectors():
    """The committed vectors (CP-3/CP-4)."""
    return {"keys": json.loads((VECTORS / "keys.json").read_text()),
            "manifest": json.loads((VECTORS / "manifest" / "cases.json").read_text()),
            "request": json.loads((VECTORS / "request" / "cases.json").read_text()),
            "release": json.loads((VECTORS / "release" / "cases.json").read_text())}

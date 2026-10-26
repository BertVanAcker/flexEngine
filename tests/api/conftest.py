"""Fixtures for the connection-point contract tests (EV-00, layer 3 of docs/flexengine-v2-plan.md §7.2)."""

from __future__ import annotations

import json

import pytest

from _api_helpers import VECTORS


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

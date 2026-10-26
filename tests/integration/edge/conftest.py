"""Fixtures of the ported demo/edge suite. See edgelab.py."""

from __future__ import annotations

import json

from flexengine2.util import read_json

from .edgelab import METRICS, RESULTS

METRICS_FILE = RESULTS / "metrics-edge-port.json"


def pytest_sessionfinish(session, exitstatus):
    if METRICS:
        RESULTS.mkdir(parents=True, exist_ok=True)
        old = read_json(METRICS_FILE, {})
        old.update(METRICS)
        METRICS_FILE.write_text(json.dumps(old, indent=2, sort_keys=True, default=str) + "\n")

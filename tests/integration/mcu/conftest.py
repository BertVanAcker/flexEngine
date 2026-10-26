"""Fixtures of the ported P3 suite (MCU hot-load on QEMU). See mculab.py."""

from __future__ import annotations

import json

import pytest

from flexengine2.util import read_json

from .mculab import METRICS, RESULTS, Lab, missing_build

METRICS_FILE = RESULTS / "metrics-mcu-port.json"


def pytest_sessionfinish(session, exitstatus):
    if METRICS:
        RESULTS.mkdir(parents=True, exist_ok=True)
        old = read_json(METRICS_FILE, {})
        old.update(METRICS)
        METRICS_FILE.write_text(json.dumps(old, indent=2, sort_keys=True, default=str) + "\n")


@pytest.fixture(scope="session")
def lab():
    why = missing_build()
    if why:
        pytest.skip(why)
    lb = Lab()
    try:
        yield lb
        # the engine under test was flexengine2 only (no probe engine module in the engine subprocess)
        lb.gw.check_clean()
    finally:
        lb.close()

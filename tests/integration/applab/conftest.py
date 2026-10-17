"""Fixtures of the ported P4 deploy suite (App Lab driver). See applablab.py."""

from __future__ import annotations

import json
import shutil
import time
import uuid

import pytest

from flexengine2.artefacts.oci import Layout
from flexengine2.identity import crypto
from flexengine2.util import read_json

from .applablab import METRICS, RESULTS, WORK, Board, Cloud, EngineProc, board_sim_missing, metric

from flexp4 import pipeline  # noqa: E402  (stand-in build pipeline, on sys.path via applablab)

METRICS_FILE = RESULTS / "metrics-applab-port.json"


def pytest_sessionfinish(session, exitstatus):
    if METRICS:
        RESULTS.mkdir(exist_ok=True)
        old = read_json(METRICS_FILE, {})
        old.update(METRICS)
        METRICS_FILE.write_text(json.dumps(old, indent=2, sort_keys=True, default=str) + "\n")


@pytest.fixture(scope="session")
def built():
    t0 = time.time()
    b = pipeline.build_uc3(WORK / "pipeline")
    metric("pipeline_build_seconds", round(time.time() - t0, 2))
    return b


@pytest.fixture(scope="session")
def keys():
    kd = WORK / "keys"
    return {"release": crypto.load_or_create(kd / "release.key"), "cloud": crypto.load_or_create(kd / "cloud.key")}


@pytest.fixture(scope="session")
def registry(built, keys):
    oci = WORK / "oci"
    shutil.rmtree(oci, ignore_errors=True)
    layout = Layout(oci)
    info = pipeline.wrap_all(layout, keys["release"], built, health_timeout=15)
    return {"dir": oci, "layout": layout, "info": info}


@pytest.fixture
def board(tmp_path):
    why = board_sim_missing()
    if why:
        pytest.skip(why)
    b = Board(tmp_path)
    yield b
    b.stop()


@pytest.fixture
def env(tmp_path, keys, registry, board):
    cloud = Cloud(tmp_path, keys, registry, pipeline.UC3_MODEL)
    engines = []

    class Env:
        pass

    e = Env()
    e.cloud, e.board, e.registry, e.keys, e.tmp = cloud, board, registry, keys, tmp_path

    def engine(prefix="unoq", **kw):
        eng = EngineProc(tmp_path, cloud, keys, board, f"{prefix}-{uuid.uuid4().hex[:6]}", **kw)
        engines.append(eng)
        return eng

    e.engine = engine
    yield e
    for eng in engines:
        eng.kill()
    cloud.stop()
    for eng in engines:  # the engine under test was flexengine2 only
        if eng.host.snapshots or eng.host.modules_file.exists():
            eng.host.check_clean()

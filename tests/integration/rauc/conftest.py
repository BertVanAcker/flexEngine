"""Fixtures of the os-rauc engine flow (EV-17). Push tier: fake rauc + boot simulator. Nightly tier (`-m docker`):
the same flows with the real rauc 1.11.3 in a privileged Ubuntu 24.04 container."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from flexengine2.identity import crypto

from .rauclab import OsDevice, RaucCloud, RaucLab, SimRauc

HERE = Path(__file__).resolve().parent
WORK = HERE.parents[1] / "work" / "rauc-int"


@pytest.fixture(scope="session")
def keys():
    kd = WORK / "keys"
    return {"release": crypto.load_or_create(kd / "release.key"), "cloud": crypto.load_or_create(kd / "cloud.key")}


@pytest.fixture(params=[pytest.param("fake", id="fake-rauc"),
                        pytest.param("real", id="rauc-1.11", marks=[pytest.mark.docker, pytest.mark.slow])])
def lab_kind(request):
    return request.param


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m"):
        return
    for it in items:
        if "integration/rauc/" in it.nodeid.replace("\\", "/") and "rauc-1.11" in it.nodeid:
            it.add_marker(pytest.mark.skip(reason='nightly tier: select with -m "docker"'))


@pytest.fixture
def make_device(tmp_path, keys, lab_kind):
    cloud = RaucCloud(tmp_path, keys)
    made: list[OsDevice] = []

    def make(*, shared: Path | None = None, max_tries: int = 3, **kw) -> OsDevice:
        cls = RaucLab if lab_kind == "real" else SimRauc
        lab = cls(tmp_path / f"sim-{len(made)}", auto_boot=False, max_tries=max_tries, mount=tmp_path).start()
        d = OsDevice(tmp_path, cloud, keys, lab, f"panda-{uuid.uuid4().hex[:6]}", shared=shared, **kw)
        made.append(d)
        return d

    make.cloud = cloud
    yield make
    for d in made:
        d.stop()
    cloud.stop()

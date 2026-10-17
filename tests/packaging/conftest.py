"""Fixtures of the packaging layer (plan §7.2 layer 8): built wheels, version-bumped source tree."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fe_pkg import DIST, PACKAGING, VERSION, make_bumped, sh


@pytest.fixture(scope="session")
def dist() -> Path:
    """flexengine/dist with the engine wheel and the wheelhouses (built by build-wheels.sh when missing;
    FLEXENGINE_PKG_REBUILD=1 forces a rebuild)."""
    have = list(DIST.glob(f"flexengine_v2-{VERSION}-py3-none-any.whl")) and (DIST / "wheelhouse").is_dir()
    if not have or os.environ.get("FLEXENGINE_PKG_REBUILD") == "1":
        r = sh(["bash", str(PACKAGING / "build-wheels.sh"), "--no-bundle"], timeout=1800)
        if r.returncode != 0:
            pytest.skip(f"build-wheels.sh failed (network?):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return DIST


@pytest.fixture(scope="session")
def wheel(dist) -> Path:
    return dist / f"flexengine_v2-{VERSION}-py3-none-any.whl"


@pytest.fixture
def bumped_source(tmp_path) -> Path:
    """A copy of flexengine/ with the version bumped to <ver>.post1 (an "upgrade" for install.sh --from-source)."""
    return make_bumped(tmp_path / "flexengine-next")

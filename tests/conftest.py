"""Shared, generic fixtures for the flexEngineV2 suite (layers in docs/flexengine-v2-plan.md §7.2).

Layer-specific fixtures live next to their tests (e.g. tests/integration/p2/conftest.py). Markers (pyproject.toml):
`docker` / `podman` tests are skipped with a reason when the runtime is not usable; `slow` is nightly-only
(deselect with `-m "not slow"`).
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FLEXENGINE = Path(__file__).resolve().parents[1]   # flexengine/
REPO = FLEXENGINE.parent                            # repo checkout
WORK = FLEXENGINE / "tests" / "work"                # gitignored caches (images, OCI layouts)
RESULTS = FLEXENGINE / "results"                    # committed results (§7.6)


@functools.cache
def runtime_usable(rt: str) -> bool:
    if not shutil.which(rt):
        return False
    try:
        return subprocess.run([rt, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config, items):
    for it in items:
        for rt in ("docker", "podman"):
            if it.get_closest_marker(rt) and not runtime_usable(rt):
                it.add_marker(pytest.mark.skip(reason=f"{rt} not installed or its daemon/service is not usable"))


@pytest.fixture(scope="session")
def python_exe() -> str:
    """The interpreter that runs engines as subprocesses (the suite's venv, with flexengine2 installed)."""
    return sys.executable


@pytest.fixture(scope="session")
def work_dir() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    return WORK


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO

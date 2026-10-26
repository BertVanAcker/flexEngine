"""Shared, generic fixtures for the flexEngineV2 suite (layers in the engine plan §7.2, run_tests.sh).

Layer-specific fixtures live next to their tests (e.g. tests/integration/p2/conftest.py). Markers (pyproject.toml):
`docker` / `podman` tests are skipped with a reason when the runtime is not usable; `framework` tests without a
framework checkout ($FLEX_REPO), `flexmodel` tests without flexmodel; `slow` is nightly-only (deselect with
`-m "not slow"`).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from flexengine2.testing.release import FRAMEWORK, VECTORS  # noqa: E402,F401  (VECTORS: vendored framework files)

FLEXENGINE = Path(__file__).resolve().parents[1]   # repository root
WORK = FLEXENGINE / "tests" / "work"                # gitignored caches (images, OCI layouts)
RESULTS = Path(os.environ.get("FLEX_RESULTS_DIR") or FLEXENGINE / "results" / "latest")   # per run, gitignored

# The fake cloud speaks plain HTTP. Plain HTTP needs an explicit lab flag (lead decision 2026-10-17); the suites set
# it for themselves and for every engine subprocess they start (inherited environment).
os.environ.setdefault("FLEX_ALLOW_PLAIN_HTTP", "1")


@functools.cache
def runtime_usable(rt: str) -> bool:
    if not shutil.which(rt):
        return False
    try:
        return subprocess.run([rt, "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@functools.cache
def flexmodel_importable() -> bool:
    try:
        import flexmodel  # noqa: F401
    except ImportError:
        return False
    return True


def pytest_collection_modifyitems(config, items):
    for it in items:
        if it.get_closest_marker("framework") and FRAMEWORK is None:
            it.add_marker(pytest.mark.skip(reason="needs a modular-interoperable-framework checkout: set FLEX_REPO"))
        if it.get_closest_marker("flexmodel") and not flexmodel_importable():
            it.add_marker(pytest.mark.skip(reason="flexmodel not importable (not on PyPI; set FLEXMODEL_SRC or "
                                                  "pip install -e <framework>/flexmodel)"))
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


@pytest.fixture
def sock_dir():
    """A short directory for AF_UNIX sockets. sun_path holds 104 bytes on macOS (108 on Linux), and macOS's tmp_path
    (/private/var/folders/<..>/T/pytest-of-<user>/pytest-<n>/<test>/) alone comes close to that, so a socket under
    tmp_path does not bind there. Removed after the test."""
    d = Path(tempfile.mkdtemp(prefix="fe-", dir="/tmp" if os.path.isdir("/tmp") else None))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)

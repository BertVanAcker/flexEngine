"""Desk-runbook layer (plan 7.2 layer 9, EV-14/EV-15): the pinned flexNode-arduino runner + GUI with the flex step
types, the step types against stand-ins, every runbook's format, and the dry runs against simulated targets.

The pinned runner is fetched once (flexengine/hwtest/upstream.py: a local checkout in $FLEXNODE_ARDUINO_REPO, else a
shallow fetch of the pinned commits); without it the layer fails, it does not skip."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HWTEST = Path(__file__).resolve().parents[2] / "hwtest"
if str(HWTEST) not in sys.path:
    sys.path.insert(0, str(HWTEST))


class Ctx:
    """The runner's Context as the steps see it (vars, marks, sub)."""

    def __init__(self, vars_: dict | None = None, unattended: bool = True):
        import argparse
        import re

        self.vars = dict(vars_ or {})
        self.marks: dict = {}
        self.args = argparse.Namespace(unattended=unattended, yes=True)
        self._re = re

    def sub(self, value):
        if isinstance(value, str):
            return self._re.sub(r"\$\{(\w+)\}", lambda m: str(self.vars.get(m.group(1), m.group(0))), value)
        if isinstance(value, list):
            return [self.sub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.sub(v) for k, v in value.items()}
        return value


@pytest.fixture(scope="session")
def runner():
    import run

    try:
        return run.load_runner()
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"pinned flexNode-arduino runner not available ({e}); set FLEXNODE_ARDUINO_REPO to a checkout "
                    f"or allow the fetch from GitHub")


@pytest.fixture
def ctx(runner):
    return Ctx()


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    """A bin directory first on PATH for fake commands (journalctl, ssh, ...)."""
    b = tmp_path / "bin"
    b.mkdir()
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b


def pytest_collection_modifyitems(config, items):
    """The dry runs (minutes, real engine processes, QEMU, Docker) run only when this layer is selected by path
    (`pytest tests/hwtest`) or FLEX_HWTEST_DRYRUN=1; a plain `pytest` over all layers skips them."""
    here = str(Path(__file__).resolve().parent)
    selected = os.environ.get("FLEX_HWTEST_DRYRUN") == "1" or any(
        str(Path(a.split("::")[0]).resolve()).startswith(here) for a in config.args)
    if selected:
        return
    skip = pytest.mark.skip(reason="desk-runbook dry run: select tests/hwtest (or FLEX_HWTEST_DRYRUN=1)")
    for it in items:
        if "dryrun" in it.keywords:
            it.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "dryrun: desk-runbook dry run against simulated targets (plan 7.2 layer 9)")

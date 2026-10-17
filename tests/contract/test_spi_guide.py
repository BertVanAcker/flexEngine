"""EV-16: the worked example in docs/spi-guide.md is the file the conformance suite runs (harness example-spool)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[2]


def test_guide_embeds_the_example_driver_verbatim():
    guide = (FE / "docs" / "spi-guide.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", guide, re.S)
    example = (FE / "docs" / "examples" / "spool_driver.py").read_text()
    assert example in blocks, "docs/spi-guide.md §4 differs from docs/examples/spool_driver.py: copy the file in"


def test_example_harness_is_in_the_push_tier():
    from spi_harness.example_spool import SpoolHarness

    # this directory's conftest.py, whatever other conftest modules the session has imported
    conftest = sys.modules.get("tests.contract.conftest") or next(
        m for n, m in sys.modules.items() if n.endswith("conftest") and getattr(m, "__file__", "")
        and Path(m.__file__).resolve() == FE / "tests" / "contract" / "conftest.py")
    assert SpoolHarness in conftest.PUSH


def test_guide_lists_every_spi_method():
    from flexengine2.drivers.base import Driver

    guide = (FE / "docs" / "spi-guide.md").read_text()
    names = [n for n in dir(Driver) if not n.startswith("_") and callable(getattr(Driver, n)) and n != "placement"]
    names += ["failure_detail", "reboots_on_apply", "repairs_digest_drift", "exclusive", "max_deployments"]
    missing = [n for n in names if f"`{n}" not in guide]
    assert not missing, f"docs/spi-guide.md does not describe {missing}"

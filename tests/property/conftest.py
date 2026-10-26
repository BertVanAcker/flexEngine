"""EV-13 crash / restart / offline property tests (plan §7.2 layer 5): hypothesis profiles and the shared lab.

Tiers
  push     (default)  every property with a small, fixed-seed budget (`derandomize`): < 3 min in total
  nightly  FLEX_PROPERTY_PROFILE=nightly  -m slow  -> the `*_nightly` variants, >= 200 schedules across the tests,
           random seeds, failing examples kept in the example database (tests/work/property/hypothesis, gitignored)

`slow` tests of this directory are skipped unless the nightly profile is selected (so the plain push command
`pytest tests/property` stays inside its budget).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.database import DirectoryBasedExampleDatabase

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parents[1]
WORK = FLEXENGINE / "tests" / "work" / "property"
# helpers of this directory (prop_*.py) and the unit-test fakes (FakeDriver, CP-1-valid documents)
for p in (HERE, FLEXENGINE / "tests" / "unit"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

PROFILE = os.environ.get("FLEX_PROPERTY_PROFILE", "push")
_common = dict(deadline=None, print_blob=True,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                      HealthCheck.data_too_large, HealthCheck.filter_too_much])
settings.register_profile("push", max_examples=20, derandomize=True, database=None, **_common)
settings.register_profile("nightly", max_examples=100, derandomize=False,
                          database=DirectoryBasedExampleDatabase(str(WORK / "hypothesis")), **_common)
settings.load_profile(PROFILE if PROFILE in ("push", "nightly") else "push")


def pytest_collection_modifyitems(config, items):
    if PROFILE == "nightly":
        return
    skip = pytest.mark.skip(reason="nightly tier: set FLEX_PROPERTY_PROFILE=nightly (and -m slow)")
    for it in items:
        if "tests/property/" in it.nodeid.replace("\\", "/") and it.get_closest_marker("slow"):
            it.add_marker(skip)


@pytest.fixture(scope="session")
def lab(tmp_path_factory):
    """Signed native releases of the P2 test app + one reference fake cloud for the whole session."""
    import prop_lab

    lab = prop_lab.Lab(tmp_path_factory.mktemp("prop-lab"))
    yield lab
    lab.close()

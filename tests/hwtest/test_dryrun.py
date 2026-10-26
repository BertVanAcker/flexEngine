"""EV-15: every runbook runs unchanged against its simulated desk (dryrun.py): the PC-side steps alone (`--only`)
and the full run with the manual steps answered by --set dry_run=1 / --yes. Results go to a temporary directory
(the committed ones in hwtest/hwtest-results/ come from `dryrun.py --all`)."""

from __future__ import annotations

import json
import shutil

import pytest

import dryrun

pytestmark = pytest.mark.dryrun
NEEDS = {"EV-HW-02": "docker", "EV-HW-09": "docker", "EV-HW-04": "qemu-system-arm", "EV-HW-05": "qemu-system-arm"}


def _need(rid):
    tool = NEEDS.get(rid)
    if tool and not shutil.which(tool):
        pytest.skip(f"{tool} not installed")


def _result(results, rid):
    return json.loads(next(results.glob(f"*_{rid}/result.json")).read_text())


@pytest.mark.parametrize("rid", sorted(dryrun.SIM))
def test_pc_side_steps(rid, tmp_path, runner):
    _need(rid)
    assert dryrun.dry_run(rid, tmp_path, pc_only=True) == 0
    res = _result(tmp_path, rid)
    assert [s["id"] for s in res["steps"]] == dryrun.SIM[rid][1] and res["summary"]["FAIL"] == 0


@pytest.mark.parametrize("rid", sorted(dryrun.SIM))
def test_full_dry_run(rid, tmp_path, runner):
    _need(rid)
    assert dryrun.dry_run(rid, tmp_path) == 0
    res = _result(tmp_path, rid)
    assert res["summary"]["FAIL"] == 0 and res["summary"]["SKIPPED"] == 0 and not res["aborted"]
    assert res["flexhwtest"]["dry_run"] is True and res["flexhwtest"]["target"] == "sim"

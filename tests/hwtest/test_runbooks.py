"""Every EV-HW runbook: loads in the pinned runner, passes the plugin's format check (`run.py --list`), has its GUI
sidecar with a known board, a dry-run profile, and only uses releases of the lab catalog."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
import yaml

import dryrun
import flexsteps
import gui
import run

RUNBOOKS = run.all_runbooks()
IDS = [p.name.split("_")[0] for p in RUNBOOKS]


def test_the_eleven_runbooks_of_plan_8_3():
    assert IDS == [f"EV-HW-{i:02d}" for i in range(1, 12)]
    assert sorted(dryrun.SIM) == IDS


@pytest.mark.parametrize("path", RUNBOOKS, ids=IDS)
def test_runbook_format(runner, path):
    rb = runner.load_runbook(str(path))
    assert rb["id"] == path.name.split("_")[0] and rb.get("title") and rb.get("description") and rb.get("setup")
    assert flexsteps.validate(rb, runner.ACTIONS) == []
    kinds = {runner.step_action(s) for s in rb["steps"]}
    assert kinds & set(flexsteps.NEW), "an EV-HW runbook uses the flex step types"
    for name, spec in rb["vars"].items():   # bench defaults, never a simulated desk's path
        assert "/tmp/rb-" not in str(spec.get("default", "")), name
    for s in rb["steps"]:
        kind = runner.step_action(s)
        if kind == "power_cycle":
            assert s[kind].get("simulate"), f"{s['id']}: power_cycle needs `simulate` for the dry run"
        if kind == "manual" and rb["id"] != "EV-HW-08":
            pass


def test_list_command_passes_for_all(runner):
    r = subprocess.run([sys.executable, str(run.HERE / "run.py"), "--check-all"], capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stdout[-3000:]
    assert "ERROR" not in r.stdout


@pytest.mark.parametrize("path", RUNBOOKS, ids=IDS)
def test_gui_sidecar(path):
    side = path.with_name(path.name.replace(".yaml", ".gui.yaml"))
    meta = yaml.safe_load(side.read_text())
    assert meta["goal"] and meta["duration"] and meta["boards"] and meta["tags"]
    board = meta["target"]["board"]
    assert board in gui.BOARDS or board in ("portenta_h7", "opta", "machine_control", "generic")
    if board in gui.BOARDS:
        for view in gui.BOARDS[board]["views"]:
            assert (gui.BOARDS_DIR / f"{board}_{view}.svg").is_file()


def test_lab_catalog_has_every_release():
    cat = yaml.safe_load((run.HERE / "catalogs" / "lab-d2.yaml").read_text())
    used = set()
    for p in RUNBOOKS:
        text = p.read_text()
        for m in re.finditer(r"\$\{desk\} (publish|deploy)([^\"&|]*)", text):
            for tok in m.group(2).split():
                if m.group(1) == "publish" and not tok.startswith("-"):
                    used.add(tok)
                elif "=" in tok and not tok.startswith("-"):
                    used.add(tok.split("=", 1)[1])
    used.discard("")
    missing = sorted(u for u in used if u not in cat["releases"])
    assert not missing, missing
    assert len(used) >= 25


def test_lab_defaults_follow_the_inventory():
    """Lab facts the runbooks rely on (demo/lab/inventory.example.yaml): LattePanda .20 / lattepanda.lab, CX .30,
    Opta .40, UNO Q unoq.local, SMP of the Portenta on the HAT Carrier UART (/dev/ttyUSB0 via the socat bridge)."""
    t = {p.name.split("_")[0]: p.read_text() for p in RUNBOOKS}
    assert "lab@lattepanda.lab" in t["EV-HW-02"] and "9700" in t["EV-HW-02"]
    assert "192.168.50.30" in t["EV-HW-07"] and "192.168.50.40" in t["EV-HW-07"]
    assert "unoq.local" in t["EV-HW-06"]
    assert "/dev/ttyUSB0" in t["EV-HW-04"] and "127.0.0.1" in t["EV-HW-04"] and '"5555"' in t["EV-HW-04"]
    assert "NOT /dev/ttyACM0" in t["EV-HW-04"]

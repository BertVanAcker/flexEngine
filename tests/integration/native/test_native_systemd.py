"""EV-19 on real systemd: workloads run without the engine.

An Ubuntu 24.04 container with systemd as PID 1 (tests/packaging/fe_pkg.py image); the driver code of this checkout
runs inside it through `docker exec` (tests/contract/spi_harness/systemd.py: SystemdBox + RemoteSystemdDriver, a
NEW driver instance per call = an engine that restarts between every step).

    install -> engine "restart" (new instance) adopts, no second unpack -> kill -9 the main PID -> systemd restarts
    it -> supervise() reports `restarted` -> reboot (daemon-reexec + container restart): the enabled unit is back
    without any engine -> remove cleans unit file, enablement, payload and processes.

Nightly: markers docker + slow (`-m docker`).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flexengine2.drivers.base import DriverError, Workload, wait_until

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

from spi_harness.base import digest_of, targz  # noqa: E402
from spi_harness.process import APP  # noqa: E402
from spi_harness.systemd import RemoteSystemdDriver, SystemdBox, polkit_image  # noqa: E402

pytestmark = [pytest.mark.docker, pytest.mark.slow]
DEVICE = "lab-panda"
DATA = "/var/lib/flexengine"


@pytest.fixture(scope="module")
def box():
    try:
        b = SystemdBox()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no systemd container: {e}")
    yield b
    b.close()


def workload(box, dep: str, version: str = "1.0.0") -> Workload:
    data = targz({"app.py": APP.encode(), "release.json": json.dumps({"version": version, "ready": True}).encode()})
    p = box.shared / f"{dep}-{version}.tar.gz"
    p.write_bytes(data)
    p.chmod(0o644)
    return Workload(dep_id=dep, module="cell_controller", version=version, digest=digest_of(data), payload_path=str(p),
                    payload_annotations={"dev.flex.profile": "native"}, parameters={"line": "A"},
                    health={"type": "http", "path": "/healthz", "periodSeconds": 0.2, "successThreshold": 2,
                            "timeoutSeconds": 15},
                    restart={"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5})


def engine(box, **kw) -> RemoteSystemdDriver:
    """A fresh driver = the engine after a restart (nothing in memory)."""
    return RemoteSystemdDriver(box, DEVICE, DATA, kw)


def show(box, unit: str, prop: str) -> str:
    return box.sh(f"systemctl show -p {prop} --value {unit}").stdout.strip()


def extracts(box) -> int:
    return len(box.sh(f"cat {DATA}/.ct-extracts 2>/dev/null").stdout.split())


def test_install_adopt_crash_restart_reboot_remove(box):
    wl = workload(box, "d-pick-cell")
    # ---- install (engine instance 1); secrets in the engine's environment must not reach the unit ----
    e1 = RemoteSystemdDriver(box, DEVICE, DATA, env={"FLEX_CLOUD_TOKEN": "s3cret", "AWS_SECRET_ACCESS_KEY": "s3cret"})
    e1.apply(None, wl)
    st = e1.status("d-pick-cell")
    assert st.present and st.running and st.digest == wl.digest, st
    unit = box.sh(f"ls /etc/systemd/system | grep '^flex-lab_panda_.*d_pick_cell'").stdout.strip()
    assert unit.endswith(".service"), unit
    assert box.sh(f"systemctl is-enabled {unit}").stdout.strip() == "enabled"
    assert show(box, unit, "ProtectSystem") == "strict" and show(box, unit, "NoNewPrivileges") == "yes"
    pid = show(box, unit, "MainPID")
    env = box.sh(f"tr '\\0' '\\n' < /proc/{pid}/environ").stdout.split("\n")
    keys = {ln.split("=", 1)[0] for ln in env if ln}
    assert "FLEX_CLOUD_TOKEN" not in keys and "AWS_SECRET_ACCESS_KEY" not in keys, keys
    assert {"FLEX_UNIT", "FLEX_HEALTH_PORT", "FLEX_PARAM_LINE"} <= keys, keys
    assert wait_until(lambda: e1.health("d-pick-cell", wl.health), 15, 0.2)
    assert extracts(box) == 1
    # ---- engine restart: a new instance lists and adopts (no apply, no unpack) ----
    e2 = engine(box)
    lst = e2.list()
    assert lst["d-pick-cell"].running and lst["d-pick-cell"].digest == wl.digest
    assert e2.health("d-pick-cell", wl.health) and e2.failed("d-pick-cell") is None
    assert e2.supervise() == []
    assert show(box, unit, "MainPID") == pid and extracts(box) == 1
    # ---- crash: systemd restarts it, the engine only reports ----
    box.sh(f"kill -9 {pid}")
    assert wait_until(lambda: show(box, unit, "MainPID") not in ("0", pid) and
                      show(box, unit, "ActiveState") == "active", 15, 0.2)
    assert int(show(box, unit, "NRestarts")) == 1
    evs = engine(box).supervise()
    assert evs == [{"dep": "d-pick-cell", "event": "restarted", "restarts": 1}], evs
    assert engine(box).supervise() == []
    assert wait_until(lambda: engine(box).health("d-pick-cell", wl.health), 15, 0.3)
    # ---- reboot without the engine: daemon-reexec, then the whole container restarts ----
    assert box.sh("systemctl daemon-reexec").returncode == 0
    assert show(box, unit, "ActiveState") == "active"
    subprocess.run(["docker", "restart", "-t", "5", box.name], capture_output=True, timeout=120, check=True)
    assert box.wait_systemd(60)
    assert wait_until(lambda: show(box, unit, "ActiveState") == "active", 30, 0.3), \
        box.sh(f"systemctl status {unit} --no-pager").stdout
    new_pid = show(box, unit, "MainPID")
    assert new_pid not in ("0", pid)
    # nothing of the engine ran so far after the boot; now it comes back and adopts what systemd started
    e3 = engine(box)
    o = e3.status("d-pick-cell")
    assert o.running and o.digest == wl.digest and o.version == wl.version, o
    assert wait_until(lambda: e3.health("d-pick-cell", wl.health), 15, 0.3)
    assert extracts(box) == 1
    # ---- remove: unit file, enablement, payload, process gone ----
    e3.remove("d-pick-cell")
    assert box.sh(f"test -e /etc/systemd/system/{unit}").returncode != 0
    assert box.sh(f"ls /etc/systemd/system/multi-user.target.wants/ | grep -c {unit}").stdout.strip() == "0"
    assert show(box, unit, "LoadState") == "not-found"
    assert box.sh(f"test -e {DATA}/native/d_pick_cell_*").returncode != 0
    assert box.sh(f"kill -0 {new_pid}").returncode != 0
    assert e3.status("d-pick-cell").present is False and e3.list() == {}
    e3.remove("d-pick-cell")


def test_user_option_runs_the_unit_as_that_user(box):
    wl = workload(box, "d-as-nobody")
    d = engine(box, user="nobody")
    d.apply(None, wl)
    try:
        assert wait_until(lambda: d.health("d-as-nobody", wl.health), 15, 0.2), d.status("d-as-nobody")
        unit = box.sh("ls /etc/systemd/system | grep 'd_as_nobody'").stdout.strip()
        pid = show(box, unit, "MainPID")
        assert box.sh(f"ps -o user= -p {pid}").stdout.strip() == "nobody"
        assert show(box, unit, "User") == "nobody"
    finally:
        d.remove("d-as-nobody")
    time.sleep(0.1)


def test_engine_as_flex_user_with_the_polkit_rule():
    """The packaged setup: engine as user flex (not root), unitDir under its data dir (the engine's own unit has
    ProtectSystem=strict), packaging/polkit/50-flexengine-systemd.rules installed. Other units stay off-limits."""
    try:
        b = SystemdBox(image=polkit_image())
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no systemd+polkit container: {e}")
    try:
        rules = Path(__file__).resolve().parents[3] / "packaging" / "polkit" / "50-flexengine-systemd.rules"
        (b.shared / "rules").write_text(rules.read_text())
        assert b.sh("useradd --system --user-group --home-dir /var/lib/flexengine --shell /usr/sbin/nologin flex && "
                    "install -d -o flex -g flex -m 0700 /var/lib/flexengine").returncode == 0
        wl = workload(b, "d-polkit")
        d = RemoteSystemdDriver(b, DEVICE, DATA, {"unit_dir": f"{DATA}/units"}, user="flex")
        with pytest.raises(DriverError, match="Interactive authentication required|Access denied"):
            d.apply(None, wl)                            # no rule yet: the system manager refuses user flex
        assert b.sh(f"install -m 0644 {b.shared}/rules /etc/polkit-1/rules.d/50-flexengine-systemd.rules && "
                    "systemctl restart polkit").returncode == 0
        d.apply(None, wl)
        unit = b.sh(f"ls {DATA}/units").stdout.strip()
        assert unit.startswith("flex-") and unit.endswith(".service"), unit
        assert b.sh(f"systemctl is-enabled {unit}").stdout.strip() in ("enabled", "linked")
        assert b.sh(f"test -L /etc/systemd/system/multi-user.target.wants/{unit}").returncode == 0
        assert show(b, unit, "User") == "flex" and show(b, unit, "ActiveState") == "active"
        assert wait_until(lambda: d.health("d-polkit", wl.health), 15, 0.2)
        assert d.list()["d-polkit"].running
        r = b.sh("runuser -u flex -- systemctl restart cron.service ssh.service dbus.service")
        assert r.returncode != 0, "the rule must not cover units other than flex-*.service"
        d.remove("d-polkit")
        assert show(b, unit, "LoadState") == "not-found"
        assert b.sh(f"test -e /etc/systemd/system/{unit} || test -L /etc/systemd/system/{unit}").returncode != 0
        assert d.status("d-polkit").present is False
    finally:
        b.close()

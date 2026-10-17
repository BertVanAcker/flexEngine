"""EV-36 acceptance: a flexCommunicator (flcpy) GettingStarted node packaged with its wheels runs natively, without
network during the install, on the process driver and on the systemd driver (real systemd in a container).

The node is examples/GettingStarted/flexCloud_variable of flexCommunicator (flcpy 0.1.7 from PyPI: numpy, bcrypt,
PyYAML are CPU/ABI-specific wheels). "Runs" = the driver reports it running/healthy (health type `process`) AND
the node itself works: its flexCloud status key in Redis says RUNNING and its flexCloud variable gets samples.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from flexengine2.drivers.base import Workload, wait_until
from flexengine2.drivers.process import ProcessDriver

from .npylab import flcpy_payload, host_python
from spi_harness.base import Release, digest_of

pytestmark = [pytest.mark.docker]
HEALTH = {"type": "process", "periodSeconds": 0.25, "successThreshold": 4, "timeoutSeconds": 60}
RESTART = {"policy": "on-failure", "maxRetries": 3, "restartSec": 1}


def workload(path, ann, dep: str, app_id: str) -> Workload:
    return Workload(dep_id=dep, module="flcpy_flexcloud_variable", version="0.1.7", digest=digest_of(path.read_bytes()),
                    payload_path=str(path), payload_annotations=ann, parameters={"appId": app_id},
                    health=dict(HEALTH), restart=dict(RESTART))


def node_works(redis, app_id: str, timeout: float = 60) -> None:
    assert wait_until(lambda: redis.status(app_id) == "RUNNING", timeout, 0.5), redis.status(app_id)
    n = redis.samples(app_id)
    assert wait_until(lambda: redis.samples(app_id) >= n + 2, 15, 0.5), "the node publishes no samples"


def test_flcpy_node_on_the_process_driver(redis, tmp_path, monkeypatch):
    py, arch = host_python()
    app_id = f"ev36_process_{uuid.uuid4().hex[:6]}"
    path, ann = flcpy_payload(tmp_path, redis.ip, app_id, py, arch)
    assert ann == {"dev.flex.profile": "native", "dev.flex.arch": arch, "dev.flex.pyabi": "cp" + py.replace(".", "")}
    # no network for the install: the engine's environment points every index/proxy at a dead port
    for k in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9/")
    d = ProcessDriver("lab-ev36", tmp_path / "data")
    assert d.platform() == {k: v for k, v in ann.items() if k != "dev.flex.profile"}
    assert d.preflight(Release(str(path), ann, {"health": HEALTH, "restartPolicy": RESTART}), {}) is None
    wl = workload(path, ann, "flcpy-node", app_id)
    try:
        t0 = time.time()
        d.apply(None, wl)
        install_s = time.time() - t0
        u = d.units[wl.dep_id]
        assert u["argv"][0].endswith("/.venv/bin/python") and u["argv"][1] == "app.py"
        cmd = open(f"/proc/{u['pid']}/cmdline", "rb").read().split(b"\0")
        assert cmd[0].decode() == u["argv"][0]
        node_works(redis, app_id)
        assert d.health(wl.dep_id, HEALTH) and d.failed(wl.dep_id) is None
        st = d.status(wl.dep_id)
        assert st.running and st.digest == wl.digest
        # an engine restart (new driver instance) adopts it: same pid, no second venv build
        d2 = ProcessDriver("lab-ev36", tmp_path / "data")
        assert d2.status(wl.dep_id).running and d2.units[wl.dep_id]["pid"] == u["pid"]
        print(json.dumps({"flcpyInstallSeconds": round(install_s, 1)}))
    finally:
        ProcessDriver("lab-ev36", tmp_path / "data").remove(wl.dep_id)


@pytest.mark.slow
def test_flcpy_node_on_the_systemd_driver(redis, tmp_path):
    from spi_harness.systemd import RemoteSystemdDriver, SystemdBox

    try:
        box = SystemdBox()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no systemd container: {e}")
    try:
        drv = RemoteSystemdDriver(box, "lab-ev36", "/var/lib/flexengine")
        plat = drv.platform()                             # the box's python3 (Ubuntu 24.04: cp312)
        py = plat["dev.flex.pyabi"].removeprefix("cp")
        py = f"{py[0]}.{py[1:]}"
        app_id = f"ev36_systemd_{uuid.uuid4().hex[:6]}"
        src, ann = flcpy_payload(tmp_path, redis.ip, app_id, py, plat["dev.flex.arch"])
        path = box.shared / src.name
        path.write_bytes(src.read_bytes())
        path.chmod(0o644)
        rel = Release(str(path), ann, {"health": HEALTH, "restartPolicy": RESTART})
        assert drv.preflight(rel, {}) is None
        wl = workload(path, ann, "flcpy-node", app_id)
        drv.apply(None, wl)
        unit = box.sh("ls /etc/systemd/system | grep 'lab_ev36.*flcpy_node'").stdout.strip()
        exec_start = box.sh(f"systemctl show -p ExecStart --value {unit}").stdout
        assert "/.venv/bin/python" in exec_start and "app.py" in exec_start, exec_start
        assert box.sh("ls /var/lib/flexengine/native/flcpy_node*/*/.venv.complete").returncode == 0
        node_works(redis, app_id)
        assert drv.health(wl.dep_id, HEALTH) and drv.failed(wl.dep_id) is None
        # the box's system interpreter got nothing installed (the venv is per payload)
        assert box.sh("python3 -c 'import flexCommunicator'").returncode != 0
        drv.remove(wl.dep_id)
        assert drv.status(wl.dep_id).present is False
    finally:
        box.close()

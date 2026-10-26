"""EV-37 acceptance: one native release carries several platform layers; the ENGINE (reconciler + artefact store +
process driver, against the reference flexCloud fake over HTTP) picks the layer that fits the device.

* (b1) a flcrs GettingStarted binary built for x86_64 and aarch64 in ONE release: the engine installs the layer of
  its own CPU (the running program's ELF machine is the host's) and the node works (Redis flexCloud keys).
* (b2) a release with only the other CPU's layer: refused at preflight with FE-NAT-PRE-001, nothing unpacked, the
  previous state keeps running.
* (b3) a flcpy node with wheels for two Python ABIs in one release: the engine picks its interpreter's (pyabi).
* (c) the flcrs node via unit.json on real systemd (nightly: docker + slow).
"""

from __future__ import annotations

import http.client
import json
import re
import uuid
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.artefacts.oci import Layout
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import wait_until
from flexengine2.drivers.native import pyenv
from flexengine2.drivers.process import ProcessDriver
from flexengine2.identity import crypto
from flexengine2.testing import native_release as nr
from flexengine2.testing.fakecloud import CloudServer

from .npylab import EXAMPLE, elf_machine, flcpy_payload, flcrs_payload, host_python

pytestmark = [pytest.mark.docker]
DEV = "lab-ev37"


def admin(srv, method, path, body=None, user="carol"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
              headers={"Authorization": "Bearer admin-secret", "X-Flex-User": user, "Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").endswith("json") else data)


class Lab:
    """flexCloud fake + one engine (in-process) with the real process driver."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cloud_key, self.rel_key = crypto.generate(), crypto.generate()
        (tmp / "cloud").mkdir()
        self.layout = Layout(tmp / "cloud" / "oci")
        self.srv = CloudServer(tmp / "cloud", 0, "admin-secret", self.cloud_key).start()
        safe = tmp / "safe.json"
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        s, tok = admin(self.srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": DEV})
        assert s == 201, tok
        self.drv = ProcessDriver(DEV, tmp / "data")
        self.eng = Engine({"deviceId": DEV, "dataDir": str(tmp / "data"), "apiUrl": self.srv.url,
                           "onboardingToken": tok["token"], "cloudPublicKey": crypto.public_b64(self.cloud_key),
                           "releasePublicKeys": [crypto.public_b64(self.rel_key)], "safeStateFile": str(safe),
                           "pollInterval": 0.05, "httpTimeout": 5}, {"native": self.drv})
        self.eng.tick()                                    # onboarding + capabilities

    def release(self, module: str, version: str, layers: list[tuple[bytes, dict]]) -> dict:
        return nr.publish_native_release(self.layout, self.rel_key, module, version, layers)

    def deploy(self, items: list[dict], ticks: int = 3) -> dict:
        s, p = admin(self.srv, "POST", "/api/v1/admin/plans", {"deviceId": DEV, "deployments": items})
        assert s == 201, p
        s, b = admin(self.srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        for _ in range(ticks):
            self.eng.tick()
        s, dev = admin(self.srv, "GET", f"/api/v1/admin/devices/{DEV}")
        return dev

    def close(self):
        for dep in list(self.drv.units):
            self.drv.remove(dep)
        self.srv.stop()


@pytest.fixture
def lab(tmp_path):
    lb = Lab(tmp_path)
    yield lb
    lb.close()


def other_arch() -> str:
    return "aarch64" if pyenv.interpreter_info()["arch"] != "aarch64" else "x86_64"


def test_capabilities_announce_the_native_platform(lab):
    s, dev = admin(lab.srv, "GET", f"/api/v1/admin/devices/{DEV}")
    frag = dev["device"]["capabilities"]["x-flex"]["drivers"]["native"]
    assert frag["platform"] == lab.drv.platform()
    contracts.validate("capabilities", dev["device"]["capabilities"])


def test_two_arch_flcrs_release_runs_the_host_layer(lab, flcrs_bins, redis):
    host = pyenv.interpreter_info()["arch"]
    assert flcrs_bins[host][1], "the host's flcrs binary must be a real build"
    app_id = f"ev37_flcrs_{uuid.uuid4().hex[:6]}"
    layers = [flcrs_payload(lab.tmp, flcrs_bins[a][0], a, redis.ip, app_id) for a in ("aarch64", "x86_64")]
    assert {elf_machine(flcrs_bins[a][0]) for a in ("aarch64", "x86_64")} == {"aarch64", "x86_64"}
    info = lab.release("flcrs_flexcloud_variable", "1.0.0", layers)
    man = json.loads(lab.layout.get_blob(info["digest"]))
    contracts.validate("oci-profile#ReleaseManifest", man)
    assert sorted(l["annotations"].get("dev.flex.arch", "") for l in man["layers"]) == ["", "aarch64", "x86_64"]
    dev = lab.deploy([{"id": "flcrs-node", "module": "flcrs_flexcloud_variable", "version": "1.0.0",
                       "driver": "native"}])
    assert dev["status"]["flcrs-node"]["state"] == "Installed", dev["status"]
    u = lab.drv.units["flcrs-node"]
    prog = Path(u["argv"][0])
    assert prog.name == EXAMPLE and prog.parent.name == "bin" and elf_machine(prog) == host
    assert wait_until(lambda: redis.status(app_id) == "RUNNING", 30, 0.5), redis.status(app_id)
    n = redis.samples(app_id)
    assert wait_until(lambda: redis.samples(app_id) > n, 15, 0.5)


def test_other_arch_only_release_is_refused_at_preflight(lab, flcrs_bins, redis):
    other = other_arch()
    data, ann = flcrs_payload(lab.tmp, flcrs_bins[other][0], other, redis.ip, "ev37_never")
    lab.release("flcrs_other_arch", "1.0.0", [(data, ann)])
    dev = lab.deploy([{"id": "flcrs-other", "module": "flcrs_other_arch", "version": "1.0.0", "driver": "native"}],
                     ticks=2)
    st = dev["status"]["flcrs-other"]
    host = pyenv.interpreter_info()["arch"]
    assert st["state"] == "Failed", st
    assert st["reason"] == f"artefact rejected: native payload is built for {other} (dev.flex.arch), this device is {host}"
    e = contracts.reason_code(st["reason"], "reason")
    assert e["code"] == "FE-REC-002" and st.get("reasonCode") == "FE-REC-002"
    inner = re.fullmatch(e["pattern"], st["reason"]).group(1)
    assert contracts.reason_code(inner, "detail")["code"] == "FE-NAT-PRE-001"
    assert "flcrs-other" not in lab.drv.units and not (lab.tmp / "data" / "native" / "flcrs-other").exists()


def test_two_pyabi_flcpy_release_picks_the_interpreter_layer(lab, redis, tmp_path):
    py, arch = host_python()
    other = "3.12" if py != "3.12" else "3.13"
    app_id = f"ev37_flcpy_{uuid.uuid4().hex[:6]}"
    mine, ann_mine = flcpy_payload(tmp_path / "a", redis.ip, app_id, py, arch)
    theirs, ann_theirs = flcpy_payload(tmp_path / "b", redis.ip, app_id, other, arch)
    assert ann_mine["dev.flex.pyabi"] != ann_theirs["dev.flex.pyabi"]
    lab.release("flcpy_two_abi", "1.0.0", [(theirs.read_bytes(), ann_theirs), (mine.read_bytes(), ann_mine)])
    dev = lab.deploy([{"id": "flcpy-node", "module": "flcpy_two_abi", "version": "1.0.0", "driver": "native"}])
    assert dev["status"]["flcpy-node"]["state"] == "Installed", dev["status"]
    tree = Path(lab.drv.units["flcpy-node"]["dir"])
    wheels = {p.name for p in (tree / "wheels").iterdir()}
    assert any(f"-{ann_mine['dev.flex.pyabi']}-" in w for w in wheels), wheels
    assert wait_until(lambda: redis.status(app_id) == "RUNNING", 60, 0.5), redis.status(app_id)


@pytest.mark.slow
def test_flcrs_node_via_unit_json_on_real_systemd(flcrs_bins, redis, tmp_path):
    from spi_harness.base import Release, digest_of
    from spi_harness.systemd import RemoteSystemdDriver, SystemdBox

    from flexengine2.drivers.base import Workload

    try:
        box = SystemdBox()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no systemd container: {e}")
    try:
        drv = RemoteSystemdDriver(box, DEV, "/var/lib/flexengine")
        arch = drv.platform()["dev.flex.arch"]
        app_id = f"ev37_systemd_{uuid.uuid4().hex[:6]}"
        data, ann = flcrs_payload(tmp_path, flcrs_bins[arch][0], arch, redis.ip, app_id)
        path = box.shared / "flcrs.tar.gz"
        path.write_bytes(data)
        path.chmod(0o644)
        health = {"type": "process", "periodSeconds": 0.25, "successThreshold": 4, "timeoutSeconds": 30}
        restart = {"policy": "on-failure", "maxRetries": 3, "restartSec": 0.5}
        assert drv.preflight(Release(str(path), ann, {"health": health, "restartPolicy": restart}), {}) is None
        other = other_arch()
        refused = drv.preflight(Release(str(path), {**ann, "dev.flex.arch": other}, {"restartPolicy": restart}), {})
        assert refused == f"native payload is built for {other} (dev.flex.arch), this device is {arch}"
        wl = Workload(dep_id="flcrs-node", module="flcrs_flexcloud_variable", version="1.0.0",
                      digest=digest_of(data), payload_path=str(path), payload_annotations=ann, health=health,
                      restart=restart)
        drv.apply(None, wl)
        unit = box.sh("ls /etc/systemd/system | grep 'flcrs_node'").stdout.strip()
        exec_start = box.sh(f"systemctl show -p ExecStart --value {unit}").stdout
        assert f"/bin/{EXAMPLE}" in exec_start and "python" not in exec_start, exec_start
        assert wait_until(lambda: redis.status(app_id) == "RUNNING", 30, 0.5), \
            box.sh(f"journalctl -u {unit} --no-pager | tail -20").stdout
        assert drv.health(wl.dep_id, health) and drv.failed(wl.dep_id) is None
        # crash: systemd restarts the binary, the node comes back
        pid = box.sh(f"systemctl show -p MainPID --value {unit}").stdout.strip()
        box.sh(f"kill -9 {pid}")
        assert wait_until(lambda: box.sh(f"systemctl show -p MainPID --value {unit}").stdout.strip() not in ("0", pid)
                          and drv.health(wl.dep_id, health), 20, 0.5)
        drv.remove(wl.dep_id)
        assert drv.status(wl.dep_id).present is False
    finally:
        box.close()

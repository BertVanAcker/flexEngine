"""deskctl's FE-4 plan items (EV-HW-12): run state, runtime, build id and dependsOn on the items it plans, carried
over by --keep and by the `state` verb (the four buttons), against the reference fake cloud."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import deskctl
from flexengine2.artefacts.oci import Layout
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing.release import VECTORS, make_release


def items():
    return [{"id": "a", "module": "m", "version": "1.0.0", "driver": "native"},
            {"id": "b", "module": "m", "version": "1.0.0", "driver": "native", "runState": "stopped"}]


def test_fe4_keys_set_run_state_runtime_build_and_order():
    out = deskctl.fe4_keys(items(), state=["a=staged"], runtime=["a=process", "b=systemd"], build=["a=b-1"],
                           after=["b=a"])
    a, b = out
    assert a == {"id": "a", "module": "m", "version": "1.0.0", "driver": "native", "runState": "staged",
                 "runtime": "process", "build": {"id": "b-1"}}
    assert b["runState"] == "stopped" and b["runtime"] == "systemd" and b["dependsOn"] == [{"deployment": "a"}]


def test_running_is_the_absence_of_the_key_and_state_all():
    out = deskctl.fe4_keys(items(), state_all="running")
    assert all("runState" not in i for i in out)
    out = deskctl.fe4_keys(items(), state_all="stopped", state=["a=running"])
    assert "runState" not in out[0] and out[1]["runState"] == "stopped"


@pytest.mark.parametrize("kw, msg", [({"state": ["x=staged"]}, "no deployment x"),
                                     ({"state": ["a=paused"]}, "run state 'paused'"),
                                     ({"runtime": ["a"]}, "not DEP=VALUE")])
def test_fe4_keys_refuse_what_cannot_be_planned(kw, msg):
    with pytest.raises(deskctl.DeskError, match=msg):
        deskctl.fe4_keys(items(), **kw)


class Cloud:
    """deskctl.Cloud against the fake, without a token file round trip."""

    def __init__(self, srv):
        self.url, self.token, self.user, self.timeout, self.ctx = srv.url, "t0k", "carol", 10, None

    call = deskctl.Cloud.call


@pytest.fixture
def cloud(tmp_path):
    (tmp_path / "cloud").mkdir()
    make_release(Layout(tmp_path / "cloud" / "oci"), tmp_path, crypto.generate(), model_dir=VECTORS / "models" / "uc1",
                 module="detection_logger", version="1.0.0", profile="native")
    srv = CloudServer(tmp_path / "cloud", 0, "t0k", crypto.generate()).start()
    st = srv.state
    with st.lock:   # an onboarded device that reports the FE-4 features and runtimes (no engine needed)
        st.s["devices"]["d1"] = {"capabilities": {"x-flex": {"features": ["runState", "runtime", "build", "retry"],
                                                             "runtimes": ["process", "systemd"]}}}
        st.save()
    try:
        yield Cloud(srv), srv
    finally:
        srv.stop()


def served(srv) -> dict:
    return {d["metadata"]["id"]: d for d in srv.state.desired_doc("d1")["deployments"]}


def test_keep_and_state_carry_the_fe4_keys(cloud, monkeypatch):
    c, srv = cloud
    monkeypatch.setattr(deskctl, "deploy_item", lambda cat, dep, name: {
        "id": dep, "module": "detection_logger", "version": "1.0.0", "driver": "native"})
    out = deskctl.deploy(None, c, "d1", ["a=x", "b=x"], False, [], 201, "dave", 5,
                         {"state_all": "staged", "runtime": ["a=process", "b=systemd"], "after": ["b=a"],
                          "build": [], "state": []})
    assert out["status"] == 201
    d = served(srv)
    assert d["a"]["x-flex-runState"] == "staged" and d["b"]["x-flex-runtime"] == "systemd"
    assert d["b"]["x-flex-dependsOn"] == [{"deployment": "a"}]
    out = deskctl.set_state(c, "d1", "running", ["b"], "dave", 5)            # Run one
    assert out["runState"] == {"a": "staged", "b": "running"}
    d = served(srv)
    assert "x-flex-runState" not in d["b"] and d["a"]["x-flex-runState"] == "staged"
    assert d["b"]["x-flex-runtime"] == "systemd" and d["b"]["x-flex-dependsOn"] == [{"deployment": "a"}]
    deskctl.set_state(c, "d1", "stopped", [], "dave", 5)                     # Stop all
    assert {k: v.get("x-flex-runState") for k, v in served(srv).items()} == {"a": "stopped", "b": "stopped"}
    deskctl.deploy(None, c, "d1", ["b=x"], True, ["a"], 201, "dave", 5, {"build": ["b=b-2"]})   # keep + new release
    d = served(srv)
    assert list(d) == ["b"] and d["b"]["x-flex-build"] == {"id": "b-2"} and d["b"]["x-flex-runtime"] == "systemd"


def test_state_on_an_empty_device_says_so(cloud):
    c, _ = cloud
    with pytest.raises(deskctl.DeskError, match="nothing deployed"):
        deskctl.set_state(c, "d1", "running", [], "dave", 5)


def test_the_sim_catalog_names_the_lab_catalog_releases():
    import yaml
    import simdesk

    lab = yaml.safe_load((Path(deskctl.__file__).parent / "catalogs" / "lab-d2.yaml").read_text())["releases"]
    sim = simdesk.fe4_catalog({"ctr": 1, "cam": 2})["releases"]
    for name, ent in sim.items():
        assert {k: ent[k] for k in ("module", "version", "driver")} == \
               {k: lab[name][k] for k in ("module", "version", "driver")}, name
    assert json.dumps(sim).count("fake-") == 2

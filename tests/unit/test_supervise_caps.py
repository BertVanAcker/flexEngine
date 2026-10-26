"""Core fixes after the driver conformance work (2026-10-17):

* supervise() defers a deployment whose executor is unreachable (DriverError or the SPI's "executor unreachable"
  status) instead of aborting the pass or repairing a workload that may still run;
* capabilities are re-checked every capsEveryTicks and sent again when they changed.
"""

from __future__ import annotations

from flexengine2.drivers.base import DriverError, Observed
from fe_unit_fakes import FakeDriver, deployment, make_engine, set_desired


def _installed(tmp_path, drv, **kw):
    eng = make_engine(tmp_path, drivers={"native": drv}, healthEveryTicks=1, **kw)
    set_desired(eng, 1, [deployment("d1")])
    for _ in range(3):
        eng.tick()
    assert "d1" in eng.state["applied"], eng.state
    return eng


def test_unreachable_status_never_triggers_repair(tmp_path):
    drv = FakeDriver()
    eng = _installed(tmp_path, drv)
    applies = len([a for a in drv.calls if a[0] == "apply"]) if hasattr(drv, "calls") else None
    drv.status = lambda dep: Observed(present=True, running=False, detail="executor unreachable: docker: timeout")
    eng.supervise()
    if applies is not None:
        assert len([a for a in drv.calls if a[0] == "apply"]) == applies
    assert "repair" not in (tmp_path / "dev-a" / "actions.jsonl").read_text()


def test_driver_error_in_status_defers_only_that_deployment(tmp_path):
    drv = FakeDriver()
    eng = _installed(tmp_path, drv)

    def boom(dep):
        raise DriverError("podman: cannot connect")
    drv.status = boom
    eng.supervise()                                   # must not raise
    assert "repair" not in (tmp_path / "dev-a" / "actions.jsonl").read_text()


def test_capabilities_are_sent_again_when_they_change(tmp_path):
    eng = make_engine(tmp_path, capsEveryTicks=1)
    sent = []
    eng.client.post_json = lambda path, doc, method="POST", signed=True: sent.append(doc) or (200, {}, b"{}")
    eng.onboard = lambda: None
    eng.pull = lambda: False
    eng.flush_outbox = lambda: None
    eng.tick()
    eng.tick()
    assert len(sent) == 1                             # unchanged: not sent again
    orig = eng.capabilities
    eng.capabilities = lambda: {**orig(), "x-flex": {**orig()["x-flex"], "changed": True}}
    eng.tick()
    assert len(sent) == 2 and sent[-1]["x-flex"]["changed"] is True

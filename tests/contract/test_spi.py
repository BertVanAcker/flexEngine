"""Driver SPI v1 conformance suite (EV-03; the spec is the docstring of flexengine2.drivers.base).

One parametrised suite, every driver must pass it. The `harness` fixture (conftest.py) adapts a driver and its
executor (fake or real); the test names are the contract cases (a harness's `xfail` / `na` keys).
"""

from __future__ import annotations

import inspect
import json
import time

import pytest

from flexengine2.core.reconciler import Engine
from flexengine2.drivers.applab.driver import ApplabDriver
from flexengine2.drivers.base import SPI_VERSION, Driver, DriverError, Observed, wait_until
from flexengine2.drivers.container import AnkaiosDriver, CliContainerDriver
from flexengine2.drivers.mcu.driver import McuFirmwareDriver, McuModuleDriver
from flexengine2.drivers.plc.verify import PlcVerifyDriver
from flexengine2.drivers.process import ProcessDriver

from spi_harness.base import deployment

ALL_DRIVERS = [CliContainerDriver, AnkaiosDriver, ProcessDriver, ApplabDriver, McuModuleDriver, McuFirmwareDriver,
               PlcVerifyDriver]
SPI_METHODS = ["plan", "apply", "remove", "status", "list", "health", "capabilities", "supervise", "failed",
               "preflight", "commit", "platform"]


# ---- helpers ---------------------------------------------------------------------------------------------------
_ENGINES: dict = {}


def engine(data_dir=None) -> Engine:
    """A real (offline) reconciler Engine: only its health gate / capabilities are used, no cloud is contacted."""
    import tempfile
    from pathlib import Path
    key = data_dir or "default"
    if key not in _ENGINES:
        d = Path(data_dir or tempfile.mkdtemp(prefix="flex-contract-engine-"))
        cfg = {"deviceId": "ct1", "dataDir": str(d), "apiUrl": "http://127.0.0.1:9", "releasePublicKeys": [],
               "cloudPublicKey": "", "logFormat": "text"}
        _ENGINES[key] = Engine(cfg, drivers={})
    return _ENGINES[key]


def gate(drv: Driver, dep_id: str, probe: dict, rollback: bool = False) -> tuple[bool, float, str | None]:
    """The reconciler's own health gate (fail-fast via failed(), commit() after a pass) with this driver."""
    eng = engine()
    ok, dt = eng.health_gate(drv, dep_id, probe, rollback=rollback)
    return ok, dt, eng.gate_failure


def install(h, drv, wl) -> None:
    """apply + passed health gate (incl. commit), as the reconciler installs a release."""
    drv.apply(None, wl)
    ok, dt, why = gate(drv, wl.dep_id, h.probe(wl))
    assert ok, f"health gate of a valid workload failed after {dt:.1f}s: {why}"


def running(drv, wl, timeout: float = 30.0) -> Observed:
    assert wait_until(lambda: (lambda o: o.present and o.running and o.digest == wl.digest)(drv.status(wl.dep_id)),
                      timeout, 0.1), f"{wl.dep_id} not running {wl.version}: {drv.status(wl.dep_id)}"
    return drv.status(wl.dep_id)


def assert_observed(o, wl):
    assert isinstance(o, Observed)
    assert o.present and o.running, o
    assert o.digest == wl.digest and o.version == wl.version, (o, wl.digest, wl.version)


# ---- class-level: attributes and signatures (no executor) -----------------------------------------------------
NAME_BUG = pytest.mark.xfail(strict=True, reason="bug: CliContainerDriver/AnkaiosDriver set `name` only in __init__ "
                                                  "(class default '?')")


@pytest.mark.parametrize("cls", [pytest.param(c, id=c.__name__, marks=[NAME_BUG] if c in (CliContainerDriver, AnkaiosDriver)
                                              else []) for c in ALL_DRIVERS])
def test_driver_class_attributes(cls):
    """Every driver class sets profile and name (not the base's "?") and implements the required methods."""
    assert issubclass(cls, Driver)
    assert not getattr(cls, "__abstractmethods__", None), f"abstract: {cls.__abstractmethods__}"
    assert isinstance(cls.profile, str) and cls.profile not in ("", "?")
    assert isinstance(cls.name, str) and cls.name not in ("", "?")
    assert isinstance(cls.exclusive, bool)
    if cls.exclusive:
        assert cls.max_deployments is None or int(cls.max_deployments) >= 1


@pytest.mark.parametrize("cls", ALL_DRIVERS, ids=lambda c: c.__name__)
def test_driver_method_signatures(cls):
    """SPI methods keep the base signatures (positional parameters), so the reconciler can call every driver alike."""
    for m in SPI_METHODS:
        base = [p.name for p in inspect.signature(getattr(Driver, m)).parameters.values()]
        own = [p for p in inspect.signature(getattr(cls, m)).parameters.values()]
        req = [p for p in own if p.default is inspect.Parameter.empty and p.kind in
               (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        assert len(req) == len(base), f"{cls.__name__}.{m}{inspect.signature(getattr(cls, m))} vs {base}"


def test_spi_version_in_engine_capabilities(tmp_path):
    """The engine reports the SPI version it speaks; SPI v1 is frozen as "1.0"."""
    assert SPI_VERSION == "1.0"
    eng = engine(tmp_path / "eng")
    eng.drivers = {"native": ProcessDriver("ct1", tmp_path)}
    doc = eng.capabilities()
    assert doc["x-flex"]["engine"]["spi"] == SPI_VERSION
    assert doc["x-flex"]["drivers"]["native"]["profile"] == "native"


# ---- per driver ------------------------------------------------------------------------------------------------
def test_identity_and_capabilities(harness):
    d = harness.new()
    assert d.profile == harness.profile and d.profile != "?"
    assert isinstance(d.name, str) and d.name not in ("", "?")
    caps = d.capabilities()
    json.dumps(caps)
    assert caps["profile"] == d.profile and caps["driver"] == d.name, caps
    for k, v in d.placement().items():
        assert caps.get(k) == v, f"placement fragment {k}={v!r} missing from capabilities {caps}"
    if d.exclusive:
        assert caps["exclusive"] is True and caps["maxDeployments"] == int(d.max_deployments or 1) >= 1
    else:
        assert not caps.get("exclusive") and "maxDeployments" not in caps


def test_unknown_deployment(harness):
    d = harness.new()
    o = d.status("never-deployed")
    assert isinstance(o, Observed) and o.present is False, o
    assert "never-deployed" not in d.list()
    assert d.failed("never-deployed") is None
    assert d.health("never-deployed", harness.probe(harness.workload())) is False
    d.remove("never-deployed")


def test_install_healthy(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    steps = d.plan(None, wl)
    assert isinstance(steps, list) and steps and all(isinstance(s, str) for s in steps)
    install(harness, d, wl)
    assert_observed(d.status(wl.dep_id), wl)
    lst = d.list()
    assert wl.dep_id in lst and lst[wl.dep_id].digest == wl.digest
    n = harness.instances(wl.dep_id)
    assert n in (None, 1), f"{n} executor instances for one deployment"


def test_failed_none_while_healthy(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    install(harness, d, wl)
    for _ in range(5):
        assert d.failed(wl.dep_id) is None, "failed() returned a reason for a healthy workload"
        time.sleep(0.1)
    assert d.health(wl.dep_id, harness.probe(wl)) is True


def test_idempotent_apply(harness):
    """apply twice = same observed state, one instance, no second payload transfer."""
    d = harness.new()
    wl = harness.workload("1.0.0")
    d.apply(None, wl)
    o1 = running(d, wl)
    t1 = harness.transfers()
    d.apply(None, wl)
    o2 = running(d, wl)
    assert (o2.present, o2.digest, o2.version) == (o1.present, o1.digest, o1.version)
    assert harness.transfers() == t1, "second apply of the same workload transferred the payload again"
    assert harness.instances(wl.dep_id) in (None, 1)
    ok, _, why = gate(d, wl.dep_id, harness.probe(wl))
    assert ok, why


def test_restart_adopt(harness):
    """A new driver instance (engine restart) over the same executor lists the workload and can adopt it."""
    d = harness.new()
    wl = harness.workload("1.0.0")
    install(harness, d, wl)
    t0 = harness.transfers()
    d2 = harness.new()
    lst = d2.list()
    assert wl.dep_id in lst, f"list() after restart lost {wl.dep_id}: {lst}"
    assert lst[wl.dep_id].digest == wl.digest and lst[wl.dep_id].version == wl.version, lst[wl.dep_id]
    o = d2.status(wl.dep_id)
    assert o.present and o.digest == wl.digest and o.running, f"not adoptable after restart: {o}"
    ok, _, why = gate(d2, wl.dep_id, harness.probe(wl))
    assert ok, f"health through the new instance: {why}"
    assert d2.failed(wl.dep_id) is None
    assert harness.transfers() == t0
    assert harness.instances(wl.dep_id) in (None, 1)


def test_remove_idempotent(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    d.apply(None, wl)
    running(d, wl)
    d.remove(wl.dep_id)
    d.remove(wl.dep_id)
    assert d.status(wl.dep_id).present is False
    assert wl.dep_id not in d.list()
    assert harness.instances(wl.dep_id) in (None, 0)
    assert wl.dep_id not in harness.new().list()


def test_fail_fast(harness):
    """failed(): None while not yet failed, a reason once visibly failed for good; the gate stops at once."""
    d = harness.new()
    bad = harness.bad_workload()
    if bad is None:
        pytest.skip("harness has no workload that fails for good")
    t0 = time.time()
    try:
        d.apply(None, bad)
    except DriverError:
        return  # fail-fast too: the engine skips the gate on a DriverError from apply
    if harness.fail_delay and time.time() - t0 < harness.fail_delay * 0.6:
        assert d.failed(bad.dep_id) is None, "failed() reported a reason before the workload failed"
    probe = harness.probe(bad)
    timeout = float(probe.get("timeoutSeconds", 10))
    ok, dt, why = gate(d, bad.dep_id, probe)
    assert not ok, "the health gate passed a workload that fails for good"
    assert why and not why.startswith("timeout"), f"gate waited for its timeout ({dt:.1f}s): {why}"
    assert dt < timeout
    r = d.failed(bad.dep_id)
    assert isinstance(r, str) and r


def test_rollback_without_retransfer(harness):
    d = harness.new()
    v1, v2 = harness.workload("1.0.0"), harness.workload("2.0.0")
    assert v1.digest != v2.digest
    install(harness, d, v1)
    d.apply(v1, v2)
    running(d, v2, timeout=60)
    t0 = harness.transfers()
    d.apply(v2, v1)  # the engine's rollback after a failed gate (no commit of v2)
    ok, _, why = gate(d, v1.dep_id, harness.probe(v1), rollback=True)
    assert ok, why
    assert_observed(d.status(v1.dep_id), v1)
    assert harness.transfers() == t0, "rollback transferred the previous payload again"
    assert harness.instances(v1.dep_id) in (None, 1)


def test_drift_repair(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    install(harness, d, wl)
    if not harness.destroy_out_of_band(wl.dep_id):
        pytest.skip("no out-of-band removal for this executor")
    assert wait_until(lambda: d.status(wl.dep_id).present is False, 15, 0.1), d.status(wl.dep_id)
    d.apply(None, wl)  # the reconciler's drift repair
    ok, _, why = gate(d, wl.dep_id, harness.probe(wl))
    assert ok, why
    assert_observed(d.status(wl.dep_id), wl)


def test_preflight_valid(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    assert d.preflight(harness.release(wl), deployment(wl.dep_id, wl.module, wl.version, d.profile)) is None


def test_preflight_refusal(harness):
    rr = harness.refused_release()
    if rr is None:
        pytest.skip("driver has no preflight checks")
    d = harness.new()
    t0 = harness.transfers()
    reason = d.preflight(*rr)
    assert isinstance(reason, str) and reason, "preflight did not refuse the bad release"
    assert harness.transfers() == t0, "preflight transferred something to the executor"


def test_platform(harness):
    d = harness.new()
    p = d.platform()
    assert p is None or (isinstance(p, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in p.items())), p
    if p:
        ann = harness.workload().payload_annotations
        assert {k: ann.get(k) for k in p} == p, f"the valid layer's annotations {ann} do not match platform() {p}"


def test_supervise(harness):
    d = harness.new()
    wl = harness.workload("1.0.0")
    install(harness, d, wl)
    for _ in range(3):
        evs = d.supervise()
        assert isinstance(evs, list)
        for ev in evs:
            assert isinstance(ev, dict) and isinstance(ev.get("dep"), str) and isinstance(ev.get("event"), str), ev
    assert_observed(d.status(wl.dep_id), wl)


def test_commit_failure_fails_gate(harness):
    """A DriverError from commit() fails the reconciler's gate (executor-injected where possible)."""
    d = harness.new()
    wl = harness.workload("1.0.0")
    d.apply(None, wl)
    if not harness.break_commit():
        def commit(self, dep_id):
            raise DriverError("confirm refused (injected)")
        d.__class__ = type(f"{type(d).__name__}CommitFails", (type(d),), {"commit": commit})
    ok, _, why = gate(d, wl.dep_id, harness.probe(wl))
    assert not ok and why and why.startswith("commit failed"), (ok, why)


def test_exclusive_backstop(harness):
    d = harness.new()
    if not d.exclusive:
        pytest.skip("not an exclusive executor")
    a = harness.workload("1.0.0")
    install(harness, d, a)
    n = int(d.max_deployments or 1)
    for i in range(n):
        b = harness.workload("2.0.0", dep_id=f"dep-extra{i}")
        with pytest.raises(DriverError):
            d.apply(None, b)
        d.remove(b.dep_id)
    assert_observed(d.status(a.dep_id), a)


def test_unreachable_defers(harness):
    """An unreachable executor never yields a refusal: preflight -> None or DriverError; platform/apply -> DriverError."""
    wl = harness.workload("1.0.0")
    with harness.unreachable() as d:
        try:
            r = d.preflight(harness.release(wl), deployment(wl.dep_id, wl.module, wl.version, d.profile))
            assert r is None, f"unreachable executor produced a refusal: {r}"
        except DriverError:
            pass
        try:
            p = d.platform()
            assert p is None or isinstance(p, dict)
        except DriverError:
            pass
        try:
            d.apply(None, wl)
        except DriverError:
            pass
        json.dumps(d.capabilities())


def test_unreachable_no_false_drift(harness):
    """Observation on an unreachable executor never raises and never reports a deployed workload as absent."""
    d = harness.new()
    wl = harness.workload("1.0.0")
    install(harness, d, wl)
    with harness.unreachable() as dd:
        o = dd.status(wl.dep_id)
        assert dd.health(wl.dep_id, harness.probe(wl)) is False
        assert dd.failed(wl.dep_id) is None
        assert isinstance(dd.list(), dict)
        assert isinstance(dd.supervise(), list)
        assert o.present is not False, f"unreachable executor reported {wl.dep_id} as missing -> false drift repair: {o}"

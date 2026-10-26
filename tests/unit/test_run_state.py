"""FE-4 at unit level: the run state (x-flex-runState staged | running | stopped), the runtime per deployment
(x-flex-runtime), the on-device build (x-flex-build) with its events, the retry of a failed release, the capability
flags (x-flex.features, x-flex.runtimes) and the refusals of what a device does not support (FE-DS-007..009)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fe_unit_fakes import FakeDriver, deployment, desired, make_engine, reports, set_desired

from flexengine2 import contracts
from flexengine2.core import outbox as outbox_policy
from flexengine2.drivers.base import BuildResult, BuildTimeout, DriverError, Observed, supports
from flexengine2.identity import crypto


class RunDriver(FakeDriver):
    """FakeDriver with the FE-4 run states and a build step; `bad` digests fail their health gate."""

    runtime = "process"

    def __init__(self, name: str = "fake", runtime: str | None = "process", profile: str = "native"):
        super().__init__()
        self.name, self.runtime, self.profile = name, runtime, profile
        self.bad: set[str] = set()
        self.build_error: BaseException | None = None
        self.builds: list[str] = []
        self.log: list[tuple[str, str]] = []

    def apply(self, current, desired):
        super().apply(current, desired)
        self.units[desired.dep_id]["running"] = True
        self.units[desired.dep_id]["build"] = dict(desired.build or {})
        self.log.append(("apply", desired.dep_id))

    def stage(self, current, desired):
        self._enter("stage", desired.dep_id)
        self.units[desired.dep_id] = {"digest": desired.digest, "version": desired.version, "running": False,
                                      "build": dict(desired.build or {})}
        self.log.append(("stage", desired.dep_id))

    def start(self, dep_id):
        self._enter("start", dep_id)
        if dep_id not in self.units:
            raise DriverError(f"unit {dep_id} is not installed")
        self.units[dep_id]["running"] = True
        self.log.append(("start", dep_id))

    def stop(self, dep_id):
        self._enter("stop", dep_id)
        if dep_id in self.units:
            self.units[dep_id]["running"] = False
        self.log.append(("stop", dep_id))

    def status(self, dep_id):
        self._enter("status", dep_id)
        u = self.units.get(dep_id)
        return Observed(present=bool(u), running=bool(u and u.get("running")), digest=(u or {}).get("digest"))

    def health(self, dep_id, probe):
        self._enter("health", dep_id)
        u = self.units.get(dep_id)
        return bool(u and u.get("running")) and u["digest"] not in self.bad

    def failed(self, dep_id):
        u = self.units.get(dep_id)
        return "crashed" if u and u.get("running") and u["digest"] in self.bad else None

    def build(self, desired, request):
        self._enter("build", desired.dep_id)
        self.builds.append(request.id)
        request.progress(0.5)
        request.line("compiling")
        if self.build_error is not None:
            raise self.build_error
        return BuildResult(digest=contracts_digest(request.id), payload_path="", payload_annotations={"built": "1"})

    def capabilities(self):
        return {"profile": self.profile, "driver": self.name}


def contracts_digest(x: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(x.encode()).hexdigest()


def dep(dep_id, version="1.0.0", run=None, after=(), **kw):
    d = deployment(dep_id, version)
    if run:
        d["x-flex-runState"] = run
    if after:
        d["x-flex-dependsOn"] = [{"deployment": a} for a in after]
    d.update(kw)
    return d


def release(source="/src.tar.gz", descriptor=None):
    def verify(repo, digest, drv, cls, platform=None, **kw):
        return SimpleNamespace(payload_path="/nonexistent", payload_annotations={},
                               source_path=source if kw.get("source") else None,
                               descriptor=descriptor or {"health": {"timeoutSeconds": 0.3, "periodSeconds": 0.01}})
    return verify


def engine(tmp_path, drivers=None, runtimes=None, **cfg):
    drivers = drivers if drivers is not None else {"native": RunDriver()}
    eng = make_engine(tmp_path, drivers=drivers, **cfg)
    for d in (runtimes or {}).values():
        eng.runtime_drivers.setdefault(d.runtime, d)
    eng.store.verify_release = release()
    return eng, drivers.get("native")


def states(eng, dep_id):
    return [r["state"] for r in reports(eng) if r.get("dep") == dep_id]


def last(eng, dep_id):
    return [r for r in reports(eng) if r.get("dep") == dep_id][-1]


def events(eng, kind=None):
    return [r for r in reports(eng) if r.get("dep") is None and (kind is None or r["event"] == kind)]


# ---- run state ------------------------------------------------------------------------------------------------
def test_staged_is_installed_but_not_started_and_skips_the_gate(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="staged")])
    eng.reconcile()
    assert ("stage", "a") in drv.log and ("apply", "a") not in drv.log
    assert not any(c == ("health", "a") for c in drv.calls), "staging runs no health gate"
    assert states(eng, "a")[-1] == "Staged"
    assert eng.state["applied"]["a"]["run"] == "staged"
    rec = last(eng, "a")
    assert contracts.errors("status", {k: v for k, v in rec.items()}) == []


def test_start_stop_start_keeps_the_release_and_gates_only_the_start(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="staged")])
    eng.reconcile()
    set_desired(eng, 2, [dep("a")])                      # running = the key's absence
    eng.reconcile()
    assert drv.log[-1] == ("start", "a") and ("health", "a") in drv.calls
    assert last(eng, "a")["state"] == "Installed" and last(eng, "a")["health"] == "healthy"
    assert eng.state["applied"]["a"]["run"] == "running"
    n_health = drv.calls.count(("health", "a"))
    set_desired(eng, 3, [dep("a", run="stopped")])
    eng.reconcile()
    assert drv.log[-1] == ("stop", "a") and last(eng, "a")["state"] == "Stopped"
    assert drv.calls.count(("health", "a")) == n_health, "stopping runs no gate"
    assert "a" in eng.state["applied"] and drv.units["a"]["running"] is False
    eng.supervise()
    assert drv.units["a"]["running"] is False and last(eng, "a")["state"] == "Stopped", "no health report, no restart"
    set_desired(eng, 4, [dep("a")])
    eng.reconcile()
    assert drv.log[-1] == ("start", "a") and last(eng, "a")["state"] == "Installed"
    assert [x for x in drv.log if x[0] in ("apply", "stage")] == [("stage", "a")], "one install for the release"
    for r in reports(eng):
        assert contracts.errors("status", r) == [], r


def test_running_unchanged_stays_without_a_report(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a")])
    eng.reconcile()
    n = len(reports(eng))
    set_desired(eng, 2, [dep("a", run="running")])       # explicit default = no change
    eng.reconcile()
    assert len(reports(eng)) == n


def test_start_order_follows_dependencies_and_stop_goes_in_reverse(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("b", run="staged", after=["a"]), dep("a", run="staged")])
    eng.reconcile()
    drv.log.clear()
    set_desired(eng, 2, [dep("b", after=["a"]), dep("a")])
    eng.reconcile()
    assert drv.log == [("start", "a"), ("start", "b")]
    drv.log.clear()
    set_desired(eng, 3, [dep("b", run="stopped", after=["a"]), dep("a", run="stopped")])
    eng.reconcile()
    assert drv.log == [("stop", "b"), ("stop", "a")]


def test_a_dependent_waits_for_a_dependency_that_is_to_run_but_not_for_one_kept_stopped(tmp_path):
    eng, drv = engine(tmp_path)
    a = dep("a", run="staged")
    drv.raise_in["start"] = None
    set_desired(eng, 1, [dep("b", run="staged", after=["a"]), a])
    eng.reconcile()
    bad = DriverError("cannot start a")
    real_start = drv.start

    def start(dep_id):
        if dep_id == "a":
            raise bad
        real_start(dep_id)
    drv.start = start
    set_desired(eng, 2, [dep("b", after=["a"]), dep("a")])   # a is to run, but does not come up: b is held
    eng.reconcile()
    eng.reconcile()
    assert ("start", "b") not in drv.log
    assert last(eng, "b")["state"] == "Pending" and last(eng, "b")["waitingFor"] == ["a"]
    assert states(eng, "b").count("Pending") == 1, "the hold is reported once"
    drv.start = real_start
    set_desired(eng, 3, [dep("b", after=["a"]), dep("a", run="stopped")])   # "run one": a is kept stopped
    eng.reconcile()
    assert drv.log[-1] == ("start", "b") and last(eng, "b")["state"] == "Installed"


def test_a_failed_start_without_a_previous_release_stops_it_again_until_a_retry(tmp_path):
    eng, drv = engine(tmp_path)
    d = dep("a", run="staged")
    drv.bad.add(d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"])
    set_desired(eng, 1, [d])
    eng.reconcile()
    set_desired(eng, 2, [dep("a")])
    eng.reconcile()
    r = last(eng, "a")
    assert r["state"] == "Failed" and r["reason"] == "health check failed" and r["reasonCode"] == "FE-REC-004"
    assert "rolledBackTo" not in r and contracts.errors("status", r) == []
    assert drv.units["a"]["running"] is False and eng.state["applied"]["a"]["run"] == "staged"
    n = drv.log.count(("start", "a"))
    eng.reconcile()
    set_desired(eng, 3, [dep("a"), dep("other")])        # another change: no retry of the failed start
    eng.reconcile()
    assert drv.log.count(("start", "a")) == n
    set_desired(eng, 4, [dep("a")])                       # the same deployments as at the failure: a retry
    drv.bad.clear()
    eng.reconcile()
    assert drv.log.count(("start", "a")) == n + 1 and last(eng, "a")["state"] == "Installed"


def test_a_failed_start_rolls_back_to_the_previous_release(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", "1.0.0")])
    eng.reconcile()
    v2 = dep("a", "2.0.0", run="staged")
    drv.bad.add(v2["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"])
    set_desired(eng, 2, [v2])
    eng.reconcile()
    assert last(eng, "a")["state"] == "Staged" and drv.units["a"]["version"] == "2.0.0"
    set_desired(eng, 3, [dep("a", "2.0.0")])
    eng.reconcile()
    r = last(eng, "a")
    assert r["state"] == "Failed" and r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True
    assert drv.units["a"]["version"] == "1.0.0" and drv.units["a"]["running"] is True
    assert eng.state["applied"]["a"]["wl"]["version"] == "1.0.0"


def test_a_start_error_is_start_failed(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="stopped")])
    eng.reconcile()
    drv.raise_in["start"] = DriverError("unit a is not installed")
    set_desired(eng, 2, [dep("a")])
    eng.reconcile()
    r = last(eng, "a")
    assert r["state"] == "Failed" and r["reason"] == "start failed" and r["reasonCode"] == "FE-REC-011"
    assert r["failure"] == "start failed: unit a is not installed"


def test_stop_after_a_failed_start_clears_the_verdict(tmp_path):
    eng, drv = engine(tmp_path)
    d = dep("a", run="staged")
    dg = d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]
    drv.bad.add(dg)
    set_desired(eng, 1, [d])
    eng.reconcile()
    set_desired(eng, 2, [dep("a")])
    eng.reconcile()
    set_desired(eng, 3, [dep("a", run="stopped")])
    eng.reconcile()
    assert last(eng, "a")["state"] == "Stopped" and "a" not in eng.state["failed"]
    drv.bad.clear()
    set_desired(eng, 4, [dep("a"), dep("x")])
    eng.reconcile()
    assert last(eng, "a")["state"] == "Installed"


def test_staging_a_new_release_over_a_running_one(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", "1.0.0")])
    eng.reconcile()
    set_desired(eng, 2, [dep("a", "2.0.0", run="staged")])
    eng.reconcile()
    assert drv.units["a"] == {"digest": drv.units["a"]["digest"], "version": "2.0.0", "running": False, "build": {}}
    rec = eng.state["applied"]["a"]
    assert rec["run"] == "staged" and rec["previous"]["version"] == "1.0.0"
    assert "a" not in eng.state["health"]


def test_drift_of_a_stopped_release_is_staged_again_and_never_started(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="stopped")])
    eng.reconcile()
    del drv.units["a"]
    eng.supervise()
    assert drv.log[-1] == ("stage", "a") and last(eng, "a")["state"] == "Stopped"
    assert last(eng, "a")["reason"] == "drift repaired (workload was missing)"
    drv.units["a"]["running"] = True                      # started behind the engine's back
    eng.supervise()
    assert drv.log[-1] == ("stop", "a") and drv.units["a"]["running"] is False


def test_a_stop_is_held_by_the_activation_gate(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", **{"x-flex-activation": {"requires": ["machine_parked"]}})])
    eng.safe.signals = lambda: {"machine_parked": True}
    eng.reconcile()
    assert last(eng, "a")["state"] == "Installed"
    eng.safe.signals = lambda: {}
    set_desired(eng, 2, [dep("a", run="stopped", **{"x-flex-activation": {"requires": ["machine_parked"]}})])
    eng.reconcile()
    assert last(eng, "a")["state"] == "Pending" and ("stop", "a") not in drv.log
    eng.safe.signals = lambda: {"machine_parked": True}
    eng.reconcile()
    assert ("stop", "a") in drv.log and last(eng, "a")["state"] == "Stopped"


def test_crash_during_a_start_is_resumed_or_taken_back(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="staged")])
    eng.reconcile()
    wl = eng.state["applied"]["a"]["wl"]
    drv.units["a"]["running"] = True
    eng.state["journal"] = {"dep": "a", "digest": wl["digest"], "version": wl["version"], "driver": "native",
                            "phase": "starting", "at": 0}
    set_desired(eng, 2, [dep("a", run="stopped")])        # no longer desired running: stopped again
    eng.resolve_journal()
    assert drv.units["a"]["running"] is False and eng.state["journal"] is None
    eng.state["journal"] = {"dep": "a", "digest": wl["digest"], "version": wl["version"], "driver": "native",
                            "phase": "starting", "at": 0}
    set_desired(eng, 3, [dep("a")])
    eng.reconcile()                                       # left to the start path: started again, gated
    assert last(eng, "a")["state"] == "Installed" and eng.state["journal"] is None


# ---- runtime -------------------------------------------------------------------------------------------------------
def test_the_runtime_picks_the_driver_instance_and_a_change_reinstalls(tmp_path):
    a, b = RunDriver("docker", "docker", "container"), RunDriver("podman", "podman", "container")
    eng, _ = engine(tmp_path, drivers={"container": a}, runtimes={"podman": b})
    assert eng.capabilities()["x-flex"]["runtimes"] == ["docker", "podman"]
    set_desired(eng, 1, [deployment("c", driver="container")])
    eng.reconcile()
    assert "c" in a.units and "c" not in b.units
    set_desired(eng, 2, [dict(deployment("c", driver="container"), **{"x-flex-runtime": "podman"})])
    eng.reconcile()
    assert "c" not in a.units and b.units["c"]["running"], "removed from docker, installed on podman"
    assert last(eng, "c")["state"] == "Installed"
    set_desired(eng, 3, [dict(deployment("c", driver="container"), **{"x-flex-runtime": "podman"})])
    n = len(b.log)
    eng.reconcile()
    assert len(b.log) == n, "same runtime, same release: nothing to do"


# ---- capabilities and refusals --------------------------------------------------------------------------------
def signed(eng_key, doc):
    body = json.dumps(doc).encode()
    return body, crypto.sign(eng_key, body)


def test_capabilities_report_the_features_and_runtimes(tmp_path):
    eng, _ = engine(tmp_path)
    x = eng.capabilities()["x-flex"]
    assert x["features"] == ["runState", "runtime", "build", "retry"] and x["runtimes"] == ["process"]
    plain, _ = engine(tmp_path / "plain", drivers={"native": FakeDriver()})
    x = plain.capabilities()["x-flex"]
    assert x["features"] == ["runtime", "retry"] and x["runtimes"] == []
    assert supports(RunDriver(), "runState") and not supports(FakeDriver(), "runState")


@pytest.mark.parametrize("case,code", [
    ("feature", "FE-DS-007"), ("build-feature", "FE-DS-007"), ("runtime", "FE-DS-008"), ("driver", "FE-DS-009"),
    ("profile", "FE-DS-005")])
def test_what_the_device_does_not_support_is_refused_as_a_whole(tmp_path, case, code):
    key = crypto.generate()
    drivers = ({"native": FakeDriver()} if case in ("feature", "build-feature")
               else {"native": RunDriver(), "applab": RunDriver("app", "applab", "applab")})
    if case == "driver":
        drivers["applab"] = FakeDriver()
        drivers["applab"].profile = "applab"
    eng, _ = engine(tmp_path, drivers=drivers, cloudPublicKey=crypto.public_b64(key))
    d = {"feature": dep("a", run="staged"),
         "build-feature": dep("a", **{"x-flex-build": {"id": "b1"}}),
         "runtime": dep("a", **{"x-flex-runtime": "systemd"}),
         "driver": dict(deployment("p", driver="applab"), **{"x-flex-runState": "stopped"}),
         "profile": dep("a", **{"x-flex-runtime": "docker"})}[case]
    doc = desired(eng.device, 1, [d])
    assert eng.accept_manifest(*signed(key, doc), None) is False
    ev = events(eng, "DesiredStateRefused")[-1]
    assert ev["reasonCode"] == code, ev["reason"]
    assert eng.state["desired"] is None
    ev = {k: v for k, v in ev.items() if k != "dep"}
    assert contracts.errors("device-event", ev) == []


def test_an_engine_with_the_features_accepts_the_keys(tmp_path):
    key = crypto.generate()
    eng, _ = engine(tmp_path, cloudPublicKey=crypto.public_b64(key))
    doc = desired(eng.device, 1, [dep("a", run="staged", **{"x-flex-runtime": "process", "x-flex-build": {"id": "b1"}})])
    assert eng.accept_manifest(*signed(key, doc), None) is True


# ---- retry ------------------------------------------------------------------------------------------------------
def test_a_release_that_failed_its_gate_is_retried_by_the_same_deployments_in_a_newer_manifest(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", "1.0.0")])
    eng.reconcile()
    v2 = dep("a", "2.0.0")
    drv.bad.add(v2["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"])
    set_desired(eng, 2, [v2])
    eng.reconcile()
    assert last(eng, "a")["state"] == "Failed"
    applies = drv.log.count(("apply", "a"))
    eng.reconcile()                                       # same manifest: kept
    set_desired(eng, 3, [v2, dep("b")])                   # an unrelated change: still kept
    eng.reconcile()
    assert drv.log.count(("apply", "a")) == applies + 0
    drv.bad.clear()
    set_desired(eng, 4, [v2])                             # the deployments it failed under, again: the retry
    eng.reconcile()
    assert drv.log.count(("apply", "a")) == applies + 1 and last(eng, "a")["state"] == "Installed"


# ---- build -------------------------------------------------------------------------------------------------------
def build_events(eng):
    return [(e["event"], e.get("reasonCode")) for e in events(eng) if e["event"].startswith("Build")]


def test_a_build_runs_once_per_id_and_reports_its_events(tmp_path):
    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="staged", **{"x-flex-build": {"id": "b1"}})])
    eng.reconcile()
    evs = [e for e in events(eng) if e["event"].startswith("Build")]
    kinds = [e["event"] for e in evs]
    assert kinds[0] == "BuildStarted" and kinds[-1] == "BuildSucceeded"
    assert "BuildProgress" in kinds and "BuildLog" in kinds
    assert all(e["deployment"] == "a" and e["buildId"] == "b1" for e in evs)
    assert evs[-1]["resultDigest"] == contracts_digest("b1")
    assert [e["line"] for e in evs if e["event"] == "BuildLog"] == ["compiling"]
    for e in evs:
        assert contracts.errors("device-event", {k: v for k, v in e.items() if k != "dep"}) == [], e
    assert drv.units["a"]["build"] == {"id": "b1", "resultDigest": contracts_digest("b1")}
    assert last(eng, "a")["state"] == "Staged"
    eng.reconcile()
    set_desired(eng, 2, [dep("a", **{"x-flex-build": {"id": "b1"}})])
    eng.reconcile()
    assert drv.builds == ["b1"], "built once per id"
    assert last(eng, "a")["state"] == "Installed"
    set_desired(eng, 3, [dep("a", **{"x-flex-build": {"id": "b2"}})])
    eng.reconcile()
    assert drv.builds == ["b1", "b2"] and drv.units["a"]["build"]["id"] == "b2"
    assert last(eng, "a")["state"] == "Installed"


@pytest.mark.parametrize("why,code", [
    (DriverError("cc exited with status 2"), "FE-BLD-001"), (BuildTimeout("build timed out after 5 s"), "FE-BLD-002"),
    ("no-source", "FE-BLD-003"), ("no-build-step", "FE-BLD-004")])
def test_a_failed_build_keeps_the_previous_state_and_names_its_code(tmp_path, why, code):
    if why == "no-build-step":
        eng, _ = engine(tmp_path, drivers={"native": RunDriver(), "applab": FakeDriver()})
        eng.drivers["applab"].profile = "applab"
        d = dict(deployment("a", driver="applab"), **{"x-flex-build": {"id": "b1"}})
        drv = None
    else:
        eng, drv = engine(tmp_path)
        d = dep("a", **{"x-flex-build": {"id": "b1"}})
        set_desired(eng, 1, [dep("a")])
        eng.reconcile()
        if why == "no-source":
            eng.store.verify_release = release(source=None)
        else:
            drv.build_error = why
    set_desired(eng, 2, [d])
    eng.reconcile()
    evs = build_events(eng)
    assert evs[0] == ("BuildStarted", None) and evs[-1] == ("BuildFailed", code), evs
    failed = events(eng, "BuildFailed")[-1]
    assert contracts.errors("device-event", {k: v for k, v in failed.items() if k != "dep"}) == []
    if drv is not None:
        assert drv.units["a"]["build"] == {} and drv.units["a"]["running"], "the previous release keeps running"
    n = len(evs)
    eng.reconcile()
    assert len(build_events(eng)) == n, "a failed build id is not built again"
    set_desired(eng, 3, [d])                              # the same deployments in a newer manifest: a retry
    eng.reconcile()
    assert len(build_events(eng)) > n


def test_build_log_lines_are_bounded_and_dropped_first_by_the_outbox(tmp_path):
    import flexengine2.core.reconciler as rec

    eng, drv = engine(tmp_path)

    def chatty(desired, request):
        for i in range(30):
            request.line(f"line {i}")
        return BuildResult(digest=contracts_digest("x"))
    drv.build = chatty
    old = rec.BUILD_LOG_MAX
    rec.BUILD_LOG_MAX = 10
    try:
        set_desired(eng, 1, [dep("a", **{"x-flex-build": {"id": "b1"}})])
        eng.reconcile()
    finally:
        rec.BUILD_LOG_MAX = old
    lines = [e["line"] for e in events(eng, "BuildLog")]
    assert len(lines) == 11 and lines[-1] == "... 20 more lines in the engine log"
    kept, dropped = outbox_policy.compact(list(eng.state["outbox"]), 5)
    assert dropped and not any(r.get("event") in ("BuildLog", "BuildProgress") for r in kept[:-1])
    assert any(r.get("event") == "BuildSucceeded" for r in kept)


def test_the_local_view_shows_staged_and_stopped(tmp_path):
    from flexengine2.local.api import EngineProvider, LocalApi

    eng, drv = engine(tmp_path)
    set_desired(eng, 1, [dep("a", run="staged"), dep("b", run="stopped"), dep("c")])
    eng.reconcile()
    api = LocalApi(EngineProvider([eng]), socket_path=None, log=lambda m: None)
    got = {d["id"]: d["state"] for d in api.status()["devices"][0]["deployments"]}
    assert got == {"a": "Staged", "b": "Stopped", "c": "Installed"}
    assert contracts.errors("cli-status", api.status()) == []

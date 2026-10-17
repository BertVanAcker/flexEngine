"""EV-13 subprocess properties: the engine as a real process (native process driver, reference fake cloud, real
signed releases) SIGKILLed at random points across install / commit / rollback / remove, optionally followed by a
simulated power cut, then restarted.

Schedule generator: 1-3 phases; each phase publishes a random desired state over two deployments (good, failing and
unsigned releases, removals by absence) and starts an incarnation armed with one kill point
(prop_killrun.py: EVENT:K:WHEN). A phase ends when the kill fires (-> crash record, optional power cut) or the device
converged (-> graceful SIGTERM). A final unarmed incarnation must then converge.

Power cut model (derived from the fsync contract, util.atomic_write_bytes / Engine.action): every completed atomic
write (state.json, units.json, CAS) and every completed action line survives; an atomic write in flight leaves at
most a torn temp file (kill WHEN=torn); unsynced history lines are dropped; every workload process dies with the
machine. Files the code never fsyncs at all (the unpacked native payload) may lose their content: modelled only in
test_bug_power_cut_loses_unpacked_payload.

Invariants: convergence within 25 s (desired good releases run healthy, failing ones rolled back, a rejected
manifest keeps the previous state), executor == applied == what flexCloud was told, every persisted report delivered
once (seq dedup), cloud lastSeq == device seq, no orphan workload process, journal closed, <= 1 apply per
(deployment, digest) run unless a crash left it incomplete, manifestVersion never goes down.
"""

from __future__ import annotations

import signal

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

import prop_lab as L
import prop_oracle as O
from prop_lab import budget

BUG_BOUND = 8   # bug tests: a fixed engine converges in ~2 s; keeps the expected failures cheap
EVENTS = ("action:apply", "action:adopt", "action:rollback", "action:remove", "action:reject", "action:repair",
          "write:state.json", "write:units.json", "write:cas", "drv:apply", "drv:remove", "gate")
COMMON = ("write:state.json", "write:units.json", "write:cas", "action:apply", "drv:apply", "gate")
VERSIONS = st.sampled_from(L.GOOD * 3 + ("1.2.0", "1.3.0"))
SPEC = st.dictionaries(st.sampled_from(L.DEPS), VERSIONS, max_size=2)


@st.composite
def kill_points(draw):
    # events that happen on every install are weighted up, so most armed kill points actually fire
    ev = draw(st.sampled_from(EVENTS + COMMON * 3))
    when = draw(st.sampled_from(("before", "after", "torn") if ev.startswith("write:") else ("before", "after")))
    return f"{ev}:{draw(st.integers(1, 2 if ev == 'gate' or ev.startswith('action') else 4))}:{when}"


def phases(power_cut=st.booleans()):
    return st.lists(st.tuples(SPEC, st.one_of(st.none(), kill_points(), kill_points(), kill_points()), power_cut),
                    min_size=1, max_size=3)


def run_phases(lab, schedule, known=O.KNOWN_BUGS, lose_payload: bool = False, onboard: bool = True,
               bound: float = 25):
    dev = L.SubDev(lab)
    try:
        if onboard:
            dev.onboard()
        mv = 0
        for spec, kill, power_cut in schedule:
            mv = lab.publish(dev.dev, spec)
            dev.start(kill=kill)
            r = dev.run_until(lambda: not dev.converged(cloud_mv=mv, known=known), 15)
            if r == "killed":
                assert dev.proc.returncode == -signal.SIGKILL, f"engine died with {dev.proc.returncode}\n{dev.context()}"
                dev.after_kill(power_cut=power_cut, label=kill)
                if power_cut and lose_payload:
                    lose_unsynced_payload(dev)
            else:
                dev.stop(signal.SIGTERM)
                dev.observe()
        dev.start()
        errs = dev.settle(bound, cloud_mv=mv, known=known)
        errs += dev.final_errors(known=known)
        O.raise_if(errs, dev.context())
    finally:
        dev.cleanup()


def lose_unsynced_payload(dev):
    """POSIX power cut: the process driver unpacks payloads and writes `.complete` without any fsync, so after the
    cut the marker may exist while the extracted files are empty (ext4 delayed allocation does exactly this)."""
    for f in (dev.data / "native").glob("*/*/*"):
        if f.is_file() and f.name != ".complete":
            f.write_bytes(b"")


# ---- push tier ------------------------------------------------------------------------------------------------

@budget(push=5, nightly=5, shrink=False)
@example(schedule=[({"dl-a": "1.0.0"}, None, False), ({"dl-a": "1.2.0", "dl-b": "1.0.0"}, "action:rollback:1:after",
                                                      False)])
@given(schedule=phases(power_cut=st.just(False)))
def test_sigkill_restart_converges(lab, schedule):
    run_phases(lab, schedule)


@budget(push=4, nightly=4, shrink=False)
@example(schedule=[({"dl-a": "1.0.0", "dl-b": "1.1.0"}, "write:state.json:3:torn", True)])
@given(schedule=phases(power_cut=st.just(True)))
def test_power_cut_restart_converges(lab, schedule):
    run_phases(lab, schedule)


# ---- nightly tier ---------------------------------------------------------------------------------------------

@pytest.mark.slow
@budget(push=0, nightly=40, shrink=False)
@given(schedule=phases())
def test_sigkill_restart_converges_nightly(lab, schedule):
    run_phases(lab, schedule)


@pytest.mark.slow
@budget(push=0, nightly=25, shrink=False)
@given(schedule=phases(power_cut=st.just(True)))
def test_power_cut_restart_converges_nightly(lab, schedule):
    run_phases(lab, schedule)


# ---- bugs (explicit minimal schedules, strict) ---------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="bug: ProcessDriver._unpack writes `.complete` without fsyncing the extracted "
                   "files; after a power cut the unit restarts from an empty app.py and never becomes healthy")
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, None, False), ({"dl-a": "1.0.0"}, "write:state.json:1:after", True)])
@example(schedule=[({"dl-a": "1.0.0"}, "gate:1:after", True)])
@given(schedule=phases())
def test_bug_power_cut_loses_unpacked_payload(lab, schedule):
    run_phases(lab, schedule, lose_payload=True, bound=BUG_BOUND)


@pytest.mark.xfail(strict=True, reason="bug: a kill between Popen and the unit-table save in ProcessDriver._spawn "
                   "leaves an unsupervised orphan workload process (pid never recorded, port kept)")
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, "spawn:1:after", False)])
@given(schedule=phases())
def test_bug_kill_in_spawn_leaks_orphan(lab, schedule):
    run_phases(lab, schedule, bound=BUG_BOUND)


@pytest.mark.xfail(strict=True, reason="bug: onboarding is not idempotent: a kill after the onboarding POST was "
                   "accepted but before `onboarded` is saved consumes the token for good; every restart gets 403 "
                   "'Invalid or used onboarding token' and the device never pulls or reports")
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, "write:state.json:1:before", False)])
@given(schedule=phases())
def test_bug_kill_during_onboarding_bricks_device(lab, schedule):
    run_phases(lab, schedule, onboard=False, bound=BUG_BOUND)

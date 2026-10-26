"""EV-13 subprocess properties: the engine as a real process (native process driver, reference fake cloud, real
signed releases) SIGKILLed at random points across install / commit / rollback / remove, optionally followed by a
simulated power cut, then restarted.

Schedule generator: 1-3 phases; each phase publishes a random desired state over two deployments (good, failing and
unsigned releases, removals by absence) and starts an incarnation armed with one kill point
(prop_killrun.py: EVENT:K:WHEN, including `spawn` = between Popen and the unit-table save). A phase ends when the kill
fires (-> crash record, optional power cut) or the device converged (-> graceful SIGTERM). A final unarmed
incarnation must then converge. The device is either onboarded first (outside the kill points) or onboards inside
the first armed incarnation (kills during onboarding).

Power cut model (derived from the fsync contract, util.atomic_write_bytes / Engine.action): every completed atomic
write (state.json, units.json, CAS) and every completed action line survives; an atomic write in flight leaves at
most a torn temp file (kill WHEN=torn); unsynced history lines are dropped; every workload process dies with the
machine; every file of an unpacked native payload the engine did not fsync loses its content (prop_killrun.py logs
the fsyncs, lose_unsynced_payload empties the others).

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
          "write:state.json", "write:units.json", "write:cas", "drv:apply", "drv:remove", "gate", "spawn")
COMMON = ("write:state.json", "write:units.json", "write:cas", "action:apply", "drv:apply", "gate")
VERSIONS = st.sampled_from(L.GOOD * 3 + ("1.2.0", "1.3.0"))
SPEC = st.dictionaries(st.sampled_from(L.DEPS), VERSIONS, max_size=2)


@st.composite
def kill_points(draw):
    # events that happen on every install are weighted up, so most armed kill points actually fire
    ev = draw(st.sampled_from(EVENTS + COMMON * 3))
    when = draw(st.sampled_from(("before", "after", "torn") if ev.startswith("write:") else ("before", "after")))
    return f"{ev}:{draw(st.integers(1, 2 if ev in ('gate', 'spawn') or ev.startswith('action') else 4))}:{when}"


def phases(power_cut=st.booleans()):
    return st.lists(st.tuples(SPEC, st.one_of(st.none(), kill_points(), kill_points(), kill_points()), power_cut),
                    min_size=1, max_size=3)


def run_phases(lab, schedule, onboard: bool = True, bound: float = 25):
    dev = L.SubDev(lab)
    try:
        if onboard:
            dev.onboard()
        mv = 0
        for spec, kill, power_cut in schedule:
            mv = lab.publish(dev.dev, spec)
            dev.start(kill=kill)
            r = dev.run_until(lambda: not dev.converged(cloud_mv=mv), 15)
            if r == "killed":
                assert dev.proc.returncode == -signal.SIGKILL, f"engine died with {dev.proc.returncode}\n{dev.context()}"
                dev.after_kill(power_cut=power_cut, label=kill)
                if power_cut:
                    lose_unsynced_payload(dev)
            else:
                dev.stop(signal.SIGTERM)
                dev.observe()
        dev.start()
        errs = dev.settle(bound, cloud_mv=mv)
        errs += dev.final_errors()
        O.raise_if(errs, dev.context())
    finally:
        dev.cleanup()


def lose_unsynced_payload(dev):
    """POSIX power cut: an extracted payload file whose data was never fsynced may be empty after the cut while the
    `.complete` marker exists (ext4 delayed allocation does exactly this). Files the engine fsynced (fsynced.log,
    written by prop_killrun.py) keep their content."""
    try:
        synced = set((dev.dir / "fsynced.log").read_text().splitlines())
    except OSError:
        synced = set()
    for f in (dev.data / "native").glob("*/*/*"):
        if f.is_file() and f.name != ".complete" and str(f.resolve()) not in synced:
            f.write_bytes(b"")


# ---- push tier ------------------------------------------------------------------------------------------------

@budget(push=5, nightly=5, shrink=False)
@example(schedule=[({"dl-a": "1.0.0"}, None, False), ({"dl-a": "1.2.0", "dl-b": "1.0.0"}, "action:rollback:1:after",
                                                      False)], onboard=True)
@given(schedule=phases(power_cut=st.just(False)), onboard=st.booleans())
def test_sigkill_restart_converges(lab, schedule, onboard):
    run_phases(lab, schedule, onboard=onboard)


@budget(push=4, nightly=4, shrink=False)
@example(schedule=[({"dl-a": "1.0.0", "dl-b": "1.1.0"}, "write:state.json:3:torn", True)])
@given(schedule=phases(power_cut=st.just(True)))
def test_power_cut_restart_converges(lab, schedule):
    run_phases(lab, schedule)


# ---- nightly tier ---------------------------------------------------------------------------------------------

@pytest.mark.slow
@budget(push=0, nightly=40, shrink=False)
@given(schedule=phases(), onboard=st.booleans())
def test_sigkill_restart_converges_nightly(lab, schedule, onboard):
    run_phases(lab, schedule, onboard=onboard)


@pytest.mark.slow
@budget(push=0, nightly=25, shrink=False)
@given(schedule=phases(power_cut=st.just(True)))
def test_power_cut_restart_converges_nightly(lab, schedule):
    run_phases(lab, schedule)


# ---- regressions of the bugs these properties found (EV-13), with their minimal schedules ------------------
# Each ran as a strict xfail until its fix landed; the broad properties above cover the triggers again.

# ProcessDriver._unpack fsyncs the extracted files and writes `.complete` atomically: a power cut after the unpack
# no longer leaves a marked payload with empty files
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, None, False), ({"dl-a": "1.0.0"}, "write:state.json:1:after", True)])
@example(schedule=[({"dl-a": "1.0.0"}, "gate:1:after", True)])
@given(schedule=phases())
def test_power_cut_keeps_unpacked_payload(lab, schedule):
    run_phases(lab, schedule, bound=BUG_BOUND)


# a kill between Popen and the unit-table save: the restarted driver adopts the process it finds by its FLEX_UNIT
# marker ("spawning" record) instead of leaking an orphan
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, "spawn:1:after", False)])
@example(schedule=[({"dl-a": "1.0.0"}, None, False), ({"dl-a": "1.1.0"}, "spawn:1:before", True)])
@given(schedule=phases())
def test_kill_in_spawn_no_orphan(lab, schedule):
    run_phases(lab, schedule, bound=BUG_BOUND)


# a kill after the onboarding POST was accepted but before `onboarded` is saved: the retry with the same token and
# device key is answered like the first one (idempotent onboarding), the device onboards
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, "write:state.json:1:before", False)])
@given(schedule=phases())
def test_kill_during_onboarding_retries(lab, schedule):
    run_phases(lab, schedule, onboard=False, bound=BUG_BOUND)

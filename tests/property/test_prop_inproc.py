"""EV-13 in-process properties: the real reconciler (`Engine`) under random schedules of manifests, replays, crashes
at named points, power cuts and executor drift (prop_model.py). Fast, so these carry the schedule volume.

Invariants (checked after every step and at the end, prop_oracle.py):
  * eventual convergence within 8 undisturbed ticks: good desired releases applied, bad ones (health / apply /
    commit failure) rolled back to what ran before, a manifest with a rejected release keeps the previous state
  * at most one apply per (deployment, digest) run except declared rollbacks / repairs and applies a crash left
    incomplete (oracle: actions.jsonl)
  * no partial state: the journal is closed, executor == applied == what the outbox reported
  * outbox seq strictly increasing, persisted seq never goes down; manifestVersion never goes down (replays refused)
"""

from __future__ import annotations

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

import prop_model as M
import prop_oracle as O
from prop_lab import budget

VERSIONS = st.sampled_from(M.GOOD * 4 + tuple(v for v, k in M.KINDS.items() if k != "good"))
SPEC = st.dictionaries(st.sampled_from(M.DEPS), VERSIONS, max_size=3)
KILL = st.builds(M.KillPoint, event=st.sampled_from(M.EVENTS), k=st.integers(1, 4),
                 when=st.sampled_from(("before", "after")))


@st.composite
def _with_crash(draw, base, crash):
    """Every schedule contains at least one crash (a derandomized push run would otherwise start with trivial ones)."""
    first, rest = draw(base)
    i = draw(st.integers(0, len(rest)))
    return first, rest[:i] + [draw(crash)] + rest[i:]


def steps(power_cut=st.booleans()):
    step = st.one_of(
        st.tuples(st.just("publish"), SPEC),
        st.tuples(st.just("replay"), st.integers(0, 5)),
        st.tuples(st.just("tick"), st.integers(1, 3)),
        st.tuples(st.just("crash"), KILL, power_cut),
        st.tuples(st.just("crash"), KILL, power_cut),
        st.tuples(st.just("drift"), st.sampled_from(M.DEPS)),
    )
    return _with_crash(st.tuples(SPEC, st.lists(step, max_size=11)), st.tuples(st.just("crash"), KILL, power_cut))


def run_schedule(tmp_root, schedule, order=None):
    """A power cut at an `action:<kind>` "before" point also leaves a torn prefix of that record in actions.jsonl
    (the append was in flight; prop_model.InprocDevice.crash). `order`: x-flex-dependsOn pairs (EV-42)."""
    first, rest = schedule
    dev = M.InprocDevice(tmp_root, order=order)
    try:
        with M.quiet_logs():
            dev.publish(first)
            for s in rest:
                if s[0] == "publish":
                    dev.publish(s[1])
                elif s[0] == "replay":
                    dev.replay(s[1])
                elif s[0] == "tick":
                    dev.ticks(s[1])
                elif s[0] == "crash":
                    kp = M.KillPoint(s[1].event, s[1].k, s[1].when)
                    dev.crash(kp, power_cut=s[2], torn_action=s[2])
                elif s[0] == "drift":
                    dev.drift(s[1])
            dev.final_check()
    finally:
        dev.close()


@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory):
    return tmp_path_factory.mktemp("inproc")


# ---- push tier ------------------------------------------------------------------------------------------------

@budget(push=30, nightly=30)
@given(schedule=steps())
def test_inproc_crash_restart_converges(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


@budget(push=30, nightly=30)
@given(schedule=steps(power_cut=st.just(True)))
def test_inproc_power_cut_converges(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


# EV-42 ordering: c depends on b, b on a (healthy). Same schedules; in addition the executor never starts a new
# release of a dependent ahead of its dependency (also after crashes / power cuts), and a dependent held behind a
# failed dependency keeps its previous release
ORDER = {"b": ("a",), "c": ("b",)}


@budget(push=20, nightly=100)
@given(schedule=steps())
def test_inproc_ordering_holds_across_crashes(tmp_root, schedule):
    run_schedule(tmp_root, schedule, order=ORDER)


@settings(phases=[Phase.explicit])
@example(schedule=({"a": "1.0.0", "b": "1.0.0"}, [("publish", {"a": "2.0.0", "b": "1.1.0"}), ("tick", 2),
                                                   ("publish", {"a": "1.0.0", "b": "1.1.0"})]))
@example(schedule=({"a": "1.0.0", "b": "1.0.0"}, [("tick", 1), ("publish", {"a": "1.0.0", "b": "1.1.0"}),
                                                   ("crash", M.KillPoint("drv:apply", 1, "after"), False),
                                                   ("publish", {"a": "2.1.0", "b": "1.1.0"})]))
@example(schedule=({"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}, [("tick", 1), ("publish", {}),
                                                                ("crash", M.KillPoint("action:remove", 2, "after"), True)]))
@given(schedule=steps())
def test_ordering_regressions(tmp_root, schedule):
    run_schedule(tmp_root, schedule, order=ORDER)


# ---- nightly tier ---------------------------------------------------------------------------------------------

@pytest.mark.slow
@budget(push=0, nightly=150)
@given(schedule=steps())
def test_inproc_crash_restart_converges_nightly(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


@pytest.mark.slow
@budget(push=0, nightly=100)
@given(schedule=steps(power_cut=st.just(True)))
def test_inproc_power_cut_converges_nightly(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


# ---- regressions of the bugs these properties found (EV-13), with their minimal schedules ------------------
# Each ran as a strict xfail until its fix landed; the broad properties above cover the triggers again.

KP = M.KillPoint


# stale install journal: an install journal left by a crash whose (dep, digest) the next desired state no longer
# asks for is resolved (committed version restored / deployment removed); supervise() repairs drift by digest
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "1.1.0"}), ("crash", KP("drv:apply", 1, "after"),
                                                                              False), ("publish", {"a": "1.0.0"})]))
@example(schedule=({"a": "1.0.0"}, [("crash", KP("drv:apply", 1, "after"), False), ("publish", {})]))
@example(schedule=({"a": "1.0.0"}, [("crash", KP("action:commit", 1, "before"), False), ("publish", {"b": "1.0.0"})]))
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "1.1.0"}), ("crash", KP("drv:apply", 1, "after"),
                                                                              False), ("publish", {"a": "2.3.0"})]))
@given(schedule=steps())
def test_stale_journal_resolved(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


# a crash after the engine declared a release failed (rollback / "failed first install" remove) resumes the
# rollback from the persisted journal and never starts the known-bad release again
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "2.1.0"}, [("crash", KP("action:remove", 1, "after"), False)]))
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "2.0.0"}), ("crash", KP("drv:apply", 2, "after"),
                                                                              False)]))
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "2.2.0"}), ("crash", KP("action:rollback", 1,
                                                                                              "before"), True)]))
@given(schedule=steps())
def test_no_reapply_after_crash_in_rollback(tmp_root, schedule):
    run_schedule(tmp_root, schedule)


# a power cut inside the actions.jsonl append leaves a torn last line; the engine cuts it off at the start, so the
# next (fsynced) record stays parseable
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "1.0.0"}, [("crash", KP("action:apply", 1, "before"), True)]))
@given(schedule=steps())
def test_torn_action_line_repaired(tmp_root, schedule):
    run_schedule(tmp_root, schedule)

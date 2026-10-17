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


def run_schedule(tmp_root, schedule, known=O.KNOWN_BUGS, torn_action: bool = False):
    first, rest = schedule
    dev = M.InprocDevice(tmp_root)
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
                    dev.crash(kp, power_cut=s[2], torn_action=torn_action)
                elif s[0] == "drift":
                    dev.drift(s[1])
            dev.final_check(known=known)
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


# ---- bugs found by these properties: minimal schedules, strict oracle --------------------------------------
# Each test runs only its explicit examples (Phase.explicit) without the tolerance of its bug, so it XPASSes --
# and fails, strict -- once the engine is fixed; then drop the entry from prop_oracle.KNOWN_BUGS.

KP = M.KillPoint


@pytest.mark.xfail(strict=True, reason="bug: an install journal left by a crash is never resolved when the next "
                   "desired state no longer asks for that (dep, digest); the uncommitted workload keeps running "
                   "(after a revert the engine even believes the old version runs: supervise() checks presence, "
                   "not the digest)")
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "1.1.0"}), ("crash", KP("drv:apply", 1, "after"),
                                                                              False), ("publish", {"a": "1.0.0"})]))
@example(schedule=({"a": "1.0.0"}, [("crash", KP("drv:apply", 1, "after"), False), ("publish", {})]))
@example(schedule=({"a": "1.0.0"}, [("crash", KP("action:commit", 1, "before"), False), ("publish", {"b": "1.0.0"})]))
@given(schedule=steps())
def test_bug_stale_journal_orphan_workload(tmp_root, schedule):
    run_schedule(tmp_root, schedule, known=O.KNOWN_BUGS - {"stale-journal"})


@pytest.mark.xfail(strict=True, reason="bug: a crash after the engine declared a release failed (rollback or "
                   "'failed first install' remove) but before the Failed report is saved re-applies the known-bad "
                   "release after the restart (state['failed'] is only persisted with the report)")
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "2.1.0"}, [("crash", KP("action:remove", 1, "after"), False)]))
@example(schedule=({"a": "1.0.0"}, [("tick", 1), ("publish", {"a": "2.0.0"}), ("crash", KP("drv:apply", 2, "after"),
                                                                              False)]))
@given(schedule=steps())
def test_bug_reapply_after_crash_in_rollback(tmp_root, schedule):
    run_schedule(tmp_root, schedule, known=O.KNOWN_BUGS - {"reapply-after-rollback-crash"})


@pytest.mark.xfail(strict=True, reason="bug: a power cut inside the actions.jsonl append leaves a torn last line; the "
                   "next (fsynced) record is appended to it and becomes unparseable (Engine.action never repairs "
                   "the tail)")
@settings(phases=[Phase.explicit])
@example(schedule=({"a": "1.0.0"}, [("crash", KP("action:apply", 1, "before"), True)]))
@given(schedule=steps())
def test_bug_torn_action_line_hides_next_record(tmp_root, schedule):
    run_schedule(tmp_root, schedule, torn_action=True)

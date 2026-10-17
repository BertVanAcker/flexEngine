"""EV-13 bundle-only properties (UC1: no link at all): the device only ever sees offline bundles (desired state +
every blob, exported by the reference fake cloud), imported at engine start (`--import-bundle`), with SIGKILLs at
random points (also inside the import: CAS writes, state save), older bundles replayed after newer ones; at the end
the link returns once.

Schedule generator: 2-3 random desired states are planned for the device on the cloud and a bundle exported after
each (increasing manifestVersion); then 1-4 imports, each = (bundle index, optional kill point).

Invariants: the accepted manifestVersion never goes down (an older bundle is refused) and equals the highest
imported bundle whose import completed; the device converges offline to that desired state (executor == applied ==
the outbox's reports, journal closed, no orphan); <= 1 apply per (deployment, digest) run unless a crash left it
incomplete; once the link returns the device onboards, delivers its whole offline outbox (seq order, deduplicated)
and converges to the cloud's current desired state.
"""

from __future__ import annotations

import signal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import prop_lab as L
import prop_oracle as O
from prop_lab import budget

CLOSED = "http://127.0.0.1:9"
VERSIONS = st.sampled_from(L.GOOD * 3 + ("1.2.0", "1.3.0"))
SPEC = st.dictionaries(st.sampled_from(L.DEPS), VERSIONS, min_size=1, max_size=2)
KILLS = st.one_of(st.none(), st.builds(lambda e, k, w: f"{e}:{k}:{w}",
                                       st.sampled_from(("write:cas", "write:state.json", "action:apply",
                                                        "action:rollback", "action:remove", "drv:apply", "gate")),
                                       st.integers(1, 6), st.sampled_from(("before", "after"))))
SCHEDULE = st.tuples(st.lists(SPEC, min_size=2, max_size=3),
                     st.lists(st.tuples(st.integers(0, 2), KILLS), min_size=1, max_size=4))


def run_bundles(lab, schedule):
    specs, imports = schedule
    dev = L.SubDev(lab, api_url=CLOSED, prefix="b")
    try:
        bundles = []
        for spec in specs:
            mv = lab.publish(dev.dev, spec)
            p = dev.dir / f"bundle-{mv}.tar"
            p.write_bytes(lab.bundle(dev.dev))
            bundles.append((mv, p))
        cloud_mv = bundles[-1][0]
        for idx, kill in imports:
            mv, path = bundles[idx % len(bundles)]
            before = dev.state().get("manifestVersion", 0)
            dev.start(kill=kill, bundle=path)
            r = dev.run_until(lambda: not dev.converged(online=False), 15)
            if r == "killed":
                assert dev.proc.returncode == -signal.SIGKILL, dev.context()
                dev.after_kill(label=kill)
            else:
                dev.stop(signal.SIGTERM)
                dev.observe()
            now = dev.state().get("manifestVersion", 0)
            if r != "killed":   # the import completed: the higher of the two wins, never the older
                assert now == max(before, mv), f"accepted v{now} after importing v{mv} over v{before}\n{dev.context()}"
            elif now not in (before, mv):
                raise O.Violation(f"accepted v{now} after a killed import of v{mv} over v{before}")
        dev.start()   # offline, no import: must converge to what was accepted
        errs = dev.settle(25, online=False)
        st_ = dev.state()
        accepted = st_.get("desired") or {}
        if accepted:
            want = [m for m, _ in bundles if m == accepted.get("manifestVersion")]
            O.check(bool(want), f"accepted manifest v{accepted.get('manifestVersion')} is no bundle", errs)
        offline_outbox = [r["seq"] for r in st_.get("outbox") or []]
        O.raise_if(errs + dev.final_errors(), dev.context())
        # the link returns: onboard, deliver the offline outbox, converge to the cloud's current desired state
        dev.stop(signal.SIGTERM)
        dev.start(apiUrl=lab.url)
        errs = dev.settle(30, cloud_mv=cloud_mv)
        got = {r["seq"] for r in lab.device(dev.dev)["statusLog"]} | {
            e["seq"] for e in lab.srv.state.s.get("events", []) if e.get("deviceId") == dev.dev}
        O.check(set(offline_outbox) <= got, f"offline reports never delivered: {sorted(set(offline_outbox) - got)}",
                errs)
        O.raise_if(errs + dev.final_errors(), dev.context())
    finally:
        dev.cleanup()


@budget(push=3, nightly=3, shrink=False)
@example(schedule=([{"dl-a": "1.0.0"}, {"dl-a": "1.1.0", "dl-b": "1.2.0"}], [(1, "write:cas:3:after"), (0, None)]))
@given(schedule=SCHEDULE)
def test_bundle_only_converges_and_replays_refused(lab, schedule):
    run_bundles(lab, schedule)


@pytest.mark.slow
@budget(push=0, nightly=20, shrink=False)
@given(schedule=SCHEDULE)
def test_bundle_only_converges_and_replays_refused_nightly(lab, schedule):
    run_bundles(lab, schedule)

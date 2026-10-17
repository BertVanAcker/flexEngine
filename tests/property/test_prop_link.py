"""EV-13 link properties: one engine subprocess behind a NetShim (testing.netshim), the reference fake cloud behind
it; random desired-state changes interleaved with link cuts at random request lines and 503 storms.

Schedule generator: 1-3 phases; each publishes a random desired state, then applies 0-2 network events:
  cut    at the K-th request line of a category (GET desired state, GET /v2 manifests/blobs/referrers, POST status,
         PUT capabilities, POST device event), either dropping that request or delivering it and losing the reply
         ("ack lost"), then the link stays down for 0-2 s
  storm  every device request answers 503 + Retry-After for 0.5-2.5 s
After the last event the link returns for good.

Invariants: convergence within 25 s of the link returning; every report the device persisted reaches flexCloud
exactly once as itself (resends after a lost ack are deduplicated by seq <= last seen), seq strictly increasing,
cloud lastSeq == device seq, the cloud's view of what runs == the device's; no crash happened, so strictly <= 1
apply per (deployment, digest) run; manifestVersion never goes down; no orphan workload.
Onboarding with a lost reply is excluded here (test_bug_onboarding_ack_lost).
"""

from __future__ import annotations

import re
import time

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st

import prop_lab as L
import prop_oracle as O
from flexengine2.testing.netshim import NetShim
from prop_lab import budget

CATEGORIES = {
    "desired": r"^GET /api/v1/devices/[^/]+/deployments ",
    "registry": r"^GET /v2/",
    "status": r"^POST /api/v1/devices/[^/]+/deployments/[^/]+/status ",
    "capabilities": r"^PUT /api/v1/devices/[^/]+/capabilities ",
    "event": r"^POST /api/v1/devices/[^/]+/events ",
    "onboarding": r"^POST /api/v1/onboarding ",
}
VERSIONS = st.sampled_from(L.GOOD * 3 + ("1.2.0", "1.3.0"))
SPEC = st.dictionaries(st.sampled_from(L.DEPS), VERSIONS, max_size=2)
CUT = st.tuples(st.just("cut"), st.sampled_from(sorted(set(CATEGORIES) - {"onboarding"})), st.integers(1, 3),
                st.booleans(), st.floats(0, 2).map(lambda x: round(x, 2)))
STORM = st.tuples(st.just("storm"), st.floats(0.5, 2.5).map(lambda x: round(x, 2)))
PHASES = st.lists(st.tuples(SPEC, st.lists(st.one_of(CUT, CUT, STORM), max_size=2)), min_size=1, max_size=3)


def kth(pattern: str, k: int):
    rx, n = re.compile(pattern), [0]

    def pred(line: str) -> bool:
        if rx.search(line):
            n[0] += 1
            return n[0] == k
        return False
    return pred


def run_link(lab, schedule, first_event=None, bound: float = 25):
    shim = NetShim(lab.port)
    dev = L.SubDev(lab, api_url=shim.url)
    try:
        if first_event:   # an event armed before the engine's first request (onboarding)
            _arm(shim, first_event)
        dev.start()
        mv = 0
        for spec, events in schedule:
            mv = lab.publish(dev.dev, spec)
            for ev in events:
                if ev[0] == "storm":
                    lab.storm(ev[1])
                    time.sleep(ev[1])
                else:
                    _arm(shim, ev)
                    L.wait(lambda: shim.down, 4)
                    time.sleep(ev[4])
                    shim.restore()
            if first_event:
                L.wait(lambda: shim.down, 4)
                time.sleep(first_event[4])
                shim.restore()
            time.sleep(0.3)
        lab.storm(0)
        shim.restore()
        errs = dev.settle(bound, cloud_mv=mv)
        errs += dev.final_errors()
        O.raise_if(errs, dev.context() + f"\nshim log tail: {shim.log[-12:]}")
    finally:
        lab.storm(0)
        dev.cleanup(cuts=sum(1 for _, _, v in shim.log if v in ("cut", "deliver-then-cut")),
                    dropped=sum(1 for _, _, v in shim.log if v == "dropped"))
        shim.close()


def _arm(shim, ev):
    _, cat, k, deliver, _down = ev
    shim.cut_on(kth(CATEGORIES[cat], k), deliver_request=deliver)


# ---- push / nightly -------------------------------------------------------------------------------------------

@budget(push=4, nightly=4, shrink=False)
@example(schedule=[({"dl-a": "1.0.0"}, [("cut", "status", 1, True, 1.0)]),
                   ({"dl-a": "1.1.0", "dl-b": "1.0.0"}, [("storm", 1.5), ("cut", "registry", 2, False, 0.5)])])
@given(schedule=PHASES)
def test_link_cuts_and_503_storms_converge(lab, schedule):
    run_link(lab, schedule)


@pytest.mark.slow
@budget(push=0, nightly=35, shrink=False)
@given(schedule=PHASES)
def test_link_cuts_and_503_storms_converge_nightly(lab, schedule):
    run_link(lab, schedule)


@pytest.mark.xfail(strict=True, reason="bug: onboarding is not idempotent: when the onboarding reply is lost the "
                   "cloud has consumed the token, every retry gets 403 'Invalid or used onboarding token' "
                   "(Rejected) and the device never onboards, pulls or reports")
@settings(phases=[Phase.explicit])
@example(schedule=[({"dl-a": "1.0.0"}, [])])
@given(schedule=PHASES)
def test_bug_onboarding_ack_lost(lab, schedule):
    run_link(lab, schedule, first_event=("cut", "onboarding", 1, True, 0.5), bound=8)

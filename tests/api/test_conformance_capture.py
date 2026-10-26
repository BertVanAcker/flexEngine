"""EV-11 API conformance (layer 3, docs/flexengine-v2-plan.md §7.2): every request flexEngineV2 makes, and every
response it gets, captured on the wire and validated against `openapi-southbound.yaml` and the CP-1/CP-2/CP-3/CP-4
schemas it references.

Real engines run against the reference fake cloud (flexengine2.testing.fakecloud) through a capturing reverse proxy:

* engine A, in process, with a version-aware FakeDriver: onboarding, capabilities, pull 200/304, artefact fetch,
  install + health gate, failed update + automatic rollback, removal, a refused (downgraded) desired state reported as
  a device event, a link cut + reconnect, a 503 window, another device's resources (403), the offline bundle;
* engine B, a subprocess (`python -m flexengine2.testing.single`) with the real native process driver: onboarding,
  capabilities, pull 200/304, install, failed update (health gate) + rollback, removal.

Checked per exchange: method + path template of exactly one operation, path/header parameters, request signature
headers (and the signature itself against the registered device key), request body schema, documented response
status, response headers, JSON response body schema. Then coverage: every device operation of the spec was used and
the expected (operation, status) pairs were seen. Finally the Margo subset report M1-M11
(flexengine/results/margo-subset-report.md) is derived from the same traffic and the contracts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.cloud.client import Offline, Rejected
from flexengine2.core.reconciler import Engine
from flexengine2.identity import crypto

from _api_helpers import FLEXENGINE, bundle_layout_problems
from _conformance import (CLOUD_PUB, CaptureProxy, Exchange, Southbound, VersionFakeDriver, admin, deploy, engine_cfg,
                          inject_desired, publish, start_cloud, token, wait)

REPORT = FLEXENGINE / "results" / "margo-subset-report.md"

# The device operations V2 uses (all southbound operations of the spec; the test fails when the spec grows).
ENGINE_OPS = {"onboard", "putCapabilities", "getDesiredState", "postStatus", "postDeviceEvent", "getBundle",
              "getManifest", "getBlob", "getReferrers"}
# (operation, status) pairs the lifecycle must exercise.
REQUIRED = {("onboard", 201), ("putCapabilities", 200), ("getDesiredState", 200), ("getDesiredState", 304),
            ("getDesiredState", 403), ("getDesiredState", 503), ("postStatus", 200), ("postDeviceEvent", 200),
            ("getBundle", 200), ("getManifest", 200), ("getBlob", 200), ("getReferrers", 200)}

DEV_A, DEV_B = "conf-a", "conf-b"


def _status(srv, dev) -> dict:
    return admin(srv, "GET", f"/api/v1/admin/devices/{dev}")[1]["status"]


def _ticks(eng, n=1):
    for _ in range(n):
        eng.tick()


def _run_subprocess_engine(tmp: Path, srv, proxy) -> dict:
    """Engine B: real process, native process driver (build_drivers), P2 stand-in app."""
    publish(srv, "conf_native", "1.0.0", runnable=True)
    publish(srv, "conf_native", "1.1.0", runnable=True, fail_health=True, timeout=2)
    d = tmp / "b"
    d.mkdir()
    safe = d / "safe.json"
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    cfg = engine_cfg(d / "data", DEV_B, proxy.url, onboardingToken=token(srv, DEV_B), safeStateFile=str(safe),
                     drivers={"native": True})
    (d / "engine.json").write_text(json.dumps(cfg))
    log = open(d / "engine.log", "ab")
    proc = subprocess.Popen([sys.executable, "-m", "flexengine2.testing.single", "--config", str(d / "engine.json")],
                            cwd=d, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    out = {}
    try:
        proxy.tag = "B"
        wait(lambda: (srv.state.s["devices"].get(DEV_B) or {}).get("capabilities"), 30, what="B onboarded")
        deploy(srv, DEV_B, [{"id": "logger", "module": "conf_native", "version": "1.0.0", "driver": "native"}])
        wait(lambda: (_status(srv, DEV_B).get("logger") or {}).get("state") == "Installed", 40, what="B installed")
        wait(lambda: srv.state.counters.get(f"{DEV_B}:desired304", 0) >= 1, 10, what="B 304")
        deploy(srv, DEV_B, [{"id": "logger", "module": "conf_native", "version": "1.1.0", "driver": "native"}])
        r = wait(lambda: (x := _status(srv, DEV_B).get("logger") or {}).get("state") == "Failed" and x, 40,
                 what="B rollback")
        out["rollback"] = r
        deploy(srv, DEV_B, [])
        wait(lambda: (_status(srv, DEV_B).get("logger") or {}).get("state") == "Removed", 30, what="B removed")
        out["units"] = json.loads((d / "data" / "native" / "units.json").read_text())
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        units = d / "data" / "native" / "units.json"
        for u in (json.loads(units.read_text()) if units.exists() else {}).values():
            try:
                os.killpg(u["pid"], signal.SIGKILL)
            except (ProcessLookupError, TypeError, PermissionError, KeyError):
                pass
    out["actions"] = [json.loads(l) for l in (d / "data" / "actions.jsonl").read_text().splitlines()]
    return out


def _run_in_process_engine(tmp: Path, srv, proxy) -> dict:
    """Engine A: in process, FakeDriver; the whole lifecycle incl. refusal, link cut, 503, 403 and the bundle."""
    publish(srv, "vec_native", "1.1.0")
    drv = VersionFakeDriver("native", bad_versions={"1.1.0"})
    d = tmp / "a"
    d.mkdir()
    safe = d / "safe.json"
    safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
    eng = Engine(engine_cfg(d / "data", DEV_A, proxy.url, onboardingToken=token(srv, DEV_A), safeStateFile=str(safe)),
                 {"native": drv})
    out = {"driver": drv}
    app = {"id": "app", "module": "vec_native", "driver": "native", "parameters": {"mode": "fast", "rate_hz": 10}}

    proxy.tag = "A:onboard"
    _ticks(eng)                                            # onboarding, capabilities, desired state v0
    assert eng.state["onboarded"]
    proxy.tag = "A:steady"
    _ticks(eng)                                            # 304
    proxy.tag = "A:install"
    deploy(srv, DEV_A, [{**app, "version": "1.0.0"}])
    _ticks(eng, 2)
    assert _status(srv, DEV_A)["app"]["state"] == "Installed"
    proxy.tag = "A:fail-rollback"
    deploy(srv, DEV_A, [{**app, "version": "1.1.0"}])
    _ticks(eng, 2)
    out["rollback"] = _status(srv, DEV_A)["app"]
    proxy.tag = "A:refusal"
    accepted = eng.state["manifestVersion"]
    kept = srv.state.s["desired"][DEV_A]
    inject_desired(srv, DEV_A, accepted - 1, [])           # rollback attack: older manifestVersion
    _ticks(eng, 2)
    out["events"] = list(srv.state.s.get("events", []))
    inject_desired(srv, DEV_A, kept["manifestVersion"], kept["deployments"])
    _ticks(eng)
    proxy.tag = "A:removal"
    deploy(srv, DEV_A, [])
    _ticks(eng, 2)
    assert _status(srv, DEV_A)["app"]["state"] == "Removed"
    proxy.tag = "A:offline"
    proxy.down = True
    deploy(srv, DEV_A, [{**app, "version": "1.0.0"}])
    _ticks(eng, 2)
    assert "app" not in drv.running
    proxy.down = False
    proxy.tag = "A:reconnect"
    _ticks(eng, 3)
    assert _status(srv, DEV_A)["app"]["state"] == "Installed" and drv.running["app"]["version"] == "1.0.0"
    proxy.tag = "A:503"
    admin(srv, "POST", "/api/v1/admin/faults", {"unavailableSeconds": 1})
    _ticks(eng)
    time.sleep(1.2)
    _ticks(eng)
    proxy.tag = "A:other-device"
    with pytest.raises(Rejected) as ei:
        eng.client.request("GET", f"/api/v1/devices/{DEV_B}/deployments")
    assert ei.value.status == 403
    proxy.tag = "A:bundle"
    st, hdrs, data = eng.client.request("GET", f"/api/v1/devices/{DEV_A}/bundle")
    out["bundle"] = data
    out["actions"] = [json.loads(l) for l in (d / "data" / "actions.jsonl").read_text().splitlines()]
    out["state"] = eng.state
    return out


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("conformance")
    srv = start_cloud(tmp / "cloud")
    proxy = CaptureProxy(srv.port).start()
    try:
        b = _run_subprocess_engine(tmp, srv, proxy)
        a = _run_in_process_engine(tmp, srv, proxy)
        sb = Southbound()
        checked = [(ex, sb.check(ex)) for ex in proxy.exchanges]
        devices = {k: dict(v) for k, v in srv.state.s["devices"].items()}
        yield {"a": a, "b": b, "sb": sb, "checked": checked, "exchanges": list(proxy.exchanges), "devices": devices,
               "srv": srv, "proxy": proxy}
    finally:
        proxy.stop()
        srv.stop()


# ---- lifecycle outcomes (the traffic below is only meaningful if the engines did what they should) --------------

def test_lifecycle_outcomes(lifecycle):
    a, b = lifecycle["a"], lifecycle["b"]
    ra = a["rollback"]
    assert ra["state"] == "Failed" and ra["rolledBackTo"] == "1.0.0" and ra["failFast"] and ra["reasonCode"] == "FE-REC-004"
    assert [e["reasonCode"] for e in a["events"]] == ["FE-DS-003"]
    assert ("remove", "app") in a["driver"].calls
    rb = b["rollback"]
    assert rb["rolledBackTo"] == "1.0.0" and rb["rollbackHealthy"] and rb["reason"] == "health check failed", rb
    assert [x["action"] for x in b["actions"]] == ["apply", "apply", "rollback", "remove"], b["actions"]
    assert [x["action"] for x in a["actions"]] == ["apply", "apply", "rollback", "remove", "apply"], a["actions"]
    assert b["units"] == {} or all(not u.get("wantRunning") for u in b["units"].values())
    assert bundle_layout_problems(a["bundle"]) == []


# ---- per-exchange conformance -------------------------------------------------------------------------------------

def test_every_exchange_matches_an_operation_and_validates(lifecycle):
    bad = [f"[{ex.tag}] {ex.method} {ex.path} -> {ex.status}: {c.problems}" for ex, c in lifecycle["checked"] if c.problems]
    assert not bad, "\n".join(bad)
    assert len(lifecycle["checked"]) > 40
    assert {ex.device for ex, _ in lifecycle["checked"]} == {DEV_A, DEV_B, None}   # None: unsigned onboarding


def test_only_device_operations_and_both_engines(lifecycle):
    sb = lifecycle["sb"]
    assert sb.device_operations() == ENGINE_OPS, "southbound spec changed: update ENGINE_OPS / REQUIRED"
    ops = {c.op for _, c in lifecycle["checked"]}
    assert ops <= ENGINE_OPS, ops - ENGINE_OPS
    per_dev = {}
    for ex, c in lifecycle["checked"]:
        dev = ex.device or json.loads(ex.req_body)["deviceId"]
        per_dev.setdefault(dev, set()).add(c.op)
    core = {"onboard", "putCapabilities", "getDesiredState", "postStatus", "getManifest", "getBlob", "getReferrers"}
    assert per_dev[DEV_A] >= core | {"postDeviceEvent", "getBundle"} and per_dev[DEV_B] >= core


def test_coverage_of_operations_and_statuses(lifecycle):
    seen = {(c.op, c.status) for _, c in lifecycle["checked"] if c.status is not None}
    assert {c.op for _, c in lifecycle["checked"]} == ENGINE_OPS
    missing = REQUIRED - seen
    assert not missing, sorted(missing)
    dropped = [ex for ex, c in lifecycle["checked"] if c.status is None]
    assert dropped and all(ex.tag == "A:offline" for ex in dropped)


def test_request_signatures_verify_with_the_registered_device_key(lifecycle):
    devices = lifecycle["devices"]
    n = 0
    for ex, c in lifecycle["checked"]:
        if c.op == "onboard":
            assert "x-flex-signature" not in ex.req_headers
            continue
        h = ex.req_headers
        contracts.validate("trust#RequestSignatureHeaders", {k: h[k.lower()] for k in (
            "X-Flex-Device", "X-Flex-Date", "X-Flex-Nonce", "X-Flex-Signature")})
        msg = crypto.request_string(ex.method, ex.path, h["x-flex-date"], h["x-flex-nonce"], ex.req_body)
        assert crypto.verify(devices[ex.device]["publicKey"], msg, h["x-flex-signature"]), (ex.method, ex.path)
        n += 1
    nonces = [ex.req_headers["x-flex-nonce"] for ex, c in lifecycle["checked"] if c.op != "onboard"]
    assert len(nonces) == len(set(nonces)) == n          # never a reused nonce


def test_desired_state_responses_are_signed_and_etagged(lifecycle):
    served_etags = set()
    for ex, c in lifecycle["checked"]:
        if c.op != "getDesiredState" or c.status not in (200, 304):
            continue
        if c.status == 200:
            assert ex.resp_headers["etag"] == '"sha256:' + hashlib.sha256(ex.resp_body).hexdigest() + '"'
            assert crypto.verify(CLOUD_PUB, ex.resp_body, ex.resp_headers["x-flex-manifest-signature"])
            served_etags.add(ex.resp_headers["etag"])
        else:
            assert ex.req_headers.get("if-none-match") in served_etags and ex.resp_body == b""


def test_artefacts_hash_to_their_digest(lifecycle):
    for ex, c in lifecycle["checked"]:
        if c.op in ("getManifest", "getBlob") and c.status == 200:
            dig = ex.path.rsplit("/", 1)[1]
            assert "sha256:" + hashlib.sha256(ex.resp_body).hexdigest() == dig
            assert ex.resp_headers.get("docker-content-digest") == dig


def test_validator_is_not_vacuous(lifecycle):
    """Mutations of real captured exchanges are caught (wrong path, status, header, request and response bodies)."""
    sb = lifecycle["sb"]
    pick = {}
    for ex, c in lifecycle["checked"]:
        pick.setdefault((c.op, c.status), ex)
    st = pick[("postStatus", 200)]
    ds = pick[("getDesiredState", 200)]

    def mut(ex, **kw) -> list[str]:
        return sb.check(Exchange(**{**ex.__dict__, **kw})).problems

    body = json.loads(st.req_body)
    assert mut(st, path="/api/v1/devices/conf-a/statuses")                                   # no operation
    assert mut(st, status=201)                                                               # undocumented status
    assert mut(st, req_headers={k: v for k, v in st.req_headers.items() if k != "x-flex-nonce"})
    assert mut(st, req_headers={**st.req_headers, "x-flex-date": "12"})
    assert mut(st, req_body=json.dumps({**body, "state": "Exploded"}).encode())
    assert mut(st, req_body=b"")
    assert mut(st, req_headers={**st.req_headers, "content-type": "text/plain"})
    assert mut(st, path=st.path.replace("/deployments/", "/deployments/bad id!/").rsplit("/", 2)[0] + "/status")
    doc = json.loads(ds.resp_body)
    bad = copy.deepcopy(doc)
    bad["deployments"] = bad["deployments"] * 2 if bad["deployments"] else [{"metadata": {}}]
    assert mut(ds, resp_body=json.dumps(bad).encode())                                       # CP-1 (+ semantic rule)
    assert mut(ds, resp_headers={**ds.resp_headers, "etag": "W/xyz"})
    assert mut(ds, resp_headers={**ds.resp_headers, "x-flex-manifest-signature": "nope"})
    assert mut(ds, resp_headers={**ds.resp_headers, "content-type": "text/html"})
    man = pick[("getManifest", 200)]
    assert mut(man, resp_body=b'{"schemaVersion": 2}')                                       # oneOf: neither
    ob = pick[("onboard", 201)]
    assert mut(ob, req_headers={**ob.req_headers, "x-flex-signature": st.req_headers["x-flex-signature"]})


def test_503_on_artefact_and_bundle_routes_is_documented(tmp_path):
    srv = start_cloud(tmp_path / "cloud")
    proxy = CaptureProxy(srv.port).start()
    try:
        eng = Engine(engine_cfg(tmp_path / "d", "conf-c", proxy.url, onboardingToken=token(srv, "conf-c")),
                     {"native": VersionFakeDriver()})
        eng.onboard()
        admin(srv, "POST", "/api/v1/admin/faults", {"unavailableSeconds": 5})
        dig = json.loads((Path(srv.state.layout.root) / "index.json").read_text())["manifests"][0]["digest"]
        for path in (f"/v2/flexnodes/vec_native/manifests/{dig}", f"/v2/flexnodes/vec_native/blobs/{dig}",
                     f"/v2/flexnodes/vec_native/referrers/{dig}", "/api/v1/devices/conf-c/bundle"):
            with pytest.raises(Offline):
                eng.client.request("GET", path)
        sb = Southbound()
        bad = [(ex.path, sb.check(ex).problems) for ex in proxy.exchanges if sb.check(ex).problems]
        assert not bad, bad
    finally:
        proxy.stop()
        srv.stop()


# ---- Margo subset report (M1-M11) ---------------------------------------------------------------------------------

def _margo(lifecycle) -> list[tuple[str, str, str, str, str]]:
    """(id, area, margo, observed, result) per item, derived from the captured traffic and the contracts."""
    chk = lifecycle["checked"]
    by_op: dict[str, list] = {}
    for ex, c in chk:
        by_op.setdefault(c.op, []).append((ex, c))
    ok = lambda op, st=200: [ex for ex, c in by_op.get(op, []) if c.status == st]   # noqa: E731
    desired = [json.loads(ex.resp_body) for ex in ok("getDesiredState")]
    deps = [d for doc in desired for d in doc["deployments"]]
    caps = [json.loads(ex.req_body) for ex, c in by_op["putCapabilities"]]
    manifests = [json.loads(ex.resp_body) for ex in ok("getManifest")]
    rel_types = sorted({m.get("artifactType") for m in manifests if not m.get("subject")})
    statuses = [json.loads(ex.req_body) for ex, c in by_op["postStatus"]]
    signed = [ex for ex, c in chk if c.op != "onboard"]
    schemes = {lifecycle["proxy"].url.split(":", 1)[0]}
    rows = []

    # M1 identity / transport
    all_signed = all("x-flex-signature" in ex.req_headers for ex in signed)
    scoped = bool([ex for ex in ok("getDesiredState", 403)])
    rows.append(("M1", "Device identity / transport", "mTLS with SPIFFE X.509-SVIDs; requests scoped by client identity",
                 f"every device request Ed25519-signed (CP-4): {all_signed}; scoped by device id (another device's "
                 f"desired state -> 403): {scoped}; transport in this run: {'/'.join(sorted(schemes))} with the lab "
                 f"flag (https with a pinned CA in the client; no mTLS until EV-21)",
                 "deviation" if all_signed and scoped else "FAIL"))
    # M2 package format
    rows.append(("M2", "Package format", "OCI manifest `application/vnd.margo.app.v1+json` + `margo.yaml`",
                 f"release manifests fetched: artifactType {', '.join(f'`{t}`' for t in rel_types)}, flex descriptor "
                 f"config, per-profile payload layers + SBOM",
                 "pass" if rel_types == ["application/vnd.margo.app.v1+json"] else "deviation"))
    # M3 deployment profile
    types = sorted({d["spec"]["deploymentProfile"]["type"] for d in deps})
    const = contracts.load("desired-state")["$defs"]["ApplicationDeployment"]["properties"]["spec"]["properties"][
        "deploymentProfile"]["properties"]["type"]
    rows.append(("M3", "Deployment profile", "`helm` / `compose` / `custom`",
                 f"served deploymentProfile.type: {', '.join(f'`{t}`' for t in types)}; CP-1 allows only "
                 f"`{const.get('const')}` (+ `x-flex-driver`); `compose` is EV-27",
                 "deviation" if const.get("const") == "custom" else "pass"))
    # M4 state manifest
    ctypes = sorted({ex.resp_headers.get("content-type", "").split(";")[0] for ex in ok("getDesiredState")})
    bundle_ok = bundle_layout_problems(ok("getBundle")[0].resp_body) == [] if ok("getBundle") else False
    rows.append(("M4", "State manifest", "per-deployment documents with digests; bundle for onboarding / poor links",
                 f"one inline document ({', '.join(ctypes)}) with every deployment and its digest; offline bundle as a "
                 f"tar (bundle.v1alpha1 layout valid: {bundle_ok})", "deviation"))
    # M5 paths / payload shapes
    used = sorted({c.template for _, c in chk})
    comp = any("components" in s for s in statuses)
    rows.append(("M5", "Paths and payload shapes", "WFM API rc.3 paths, `deploymentStatus` with per-component states",
                 f"Margo-shaped but own paths ({len(used)} templates, e.g. `/api/v1/devices/{{deviceId}}/deployments`); "
                 f"status records per deployment, per-component states: {comp}", "deviation"))
    # M6 signing
    sig_refs = all(any(m.get("artifactType") == "application/vnd.flex.signature.v1+json"
                       for m in json.loads(ex.resp_body)["manifests"]) for ex in ok("getReferrers"))
    man_sig = all(ex.resp_headers.get("x-flex-manifest-signature") for ex in ok("getDesiredState"))
    rows.append(("M6", "Signing", "optional (cosign mentioned)",
                 f"mandatory: every referrers answer carried a release signature: {sig_refs}; every desired state "
                 f"carried X-Flex-Manifest-Signature: {man_sig}; two-person approval for the safety class",
                 "pass" if sig_refs and man_sig else "FAIL"))
    # M7 observability
    otel = sorted({str(c["properties"].get("otelCollector")).lower() for c in caps})
    rows.append(("M7", "Observability", "`otelCollector: true` required for hosting devices",
                 f"capabilities sent with otelCollector: {', '.join(otel)} (engine spans optional, EV-09; collector "
                 f"flag EV-25)", "pass" if otel == ["true"] else "deviation"))
    # M8 placement
    constraints = any("deviceConstraints" in d["spec"] for d in deps)
    rows.append(("M8", "Placement constraints", "`deviceConstraints`, eligibility rules; the device makes the final "
                 "decision", f"served deployments carry deviceConstraints: {constraints}; no device-side placement "
                 f"check (only the backend's exclusive-executor check at plan time); EV-25", "deviation"))
    # M9 polling windows
    hints = sorted({h for ex in ok("getDesiredState") for h in ex.resp_headers if h in ("cache-control", "x-flex-poll-interval")})
    ra = all(ex.resp_headers.get("retry-after") for ex in ok("getDesiredState", 503))
    rows.append(("M9", "Polling / downtime windows", "configurable by the server",
                 f"no server polling hint in desired-state answers ({', '.join(hints) or 'none'}); fixed "
                 f"pollInterval; 503 answers carried Retry-After: {ra}; per-deployment activation windows "
                 f"(`x-flex-activation`)", "deviation"))
    # M10 gateways
    pat = contracts.load("desired-state")["$defs"]["DeviceId"]["pattern"]
    dot, slash = bool(re.fullmatch(pat, "gw1.mcu0")), bool(re.fullmatch(pat, "gw1/mcu0"))
    rows.append(("M10", "Gateways", "opaque / see-thru, `gateway/child` device ids",
                 f"see-thru only, child id `<gateway>.<child>` (CP-1 DeviceId accepts `gw1.mcu0`: {dot}, "
                 f"`gw1/mcu0`: {slash}); no gateway in this run", "deviation"))
    # M11 parameters
    params = sorted({k for d in deps for k in d["spec"].get("parameters", {})})
    installed_with = any(s.get("state") == "Installed" for s in statuses if s["dep"] == "app")
    rows.append(("M11", "Parameters / configuration schema", "`configuration{sections, schema}` validated",
                 f"parameters {', '.join(f'`{p}`' for p in params)} typed by CP-1 (scalars) but not checked against "
                 f"the release descriptor (none declared there; deployment still installed: {installed_with}); EV-25",
                 "deviation"))
    return rows


def _report(lifecycle) -> str:
    rows = _margo(lifecycle)
    n = {k: sum(1 for r in rows if r[4] == k) for k in ("pass", "deviation", "not-applicable")}
    ops: dict[str, set] = {}
    tmpl = {}
    for _, c in lifecycle["checked"]:
        if c.status is not None:
            ops.setdefault(c.op, set()).add(c.status)
            tmpl[c.op] = c.template
    sb = lifecycle["sb"]
    method = {o["id"]: o["method"] for o in sb.ops}
    out = ["# Margo subset report (EV-11)", "",
           "Generated by `flexengine/tests/api/test_conformance_capture.py` (do not edit by hand: the test rewrites this "
           "file and fails when it was stale). Source: the southbound traffic of real flexEngineV2 engines (one in "
           "process with a FakeDriver, one `python -m flexengine2.testing.single` subprocess with the native process "
           "driver) against the reference fake cloud, captured by a proxy and validated against "
           "`openapi-southbound.yaml`, plus the CP contracts. Margo items from "
           "`docs/research/ws3-deployment/landscape.md` §6.1 (PR3 / WFM API 1.0.0-rc.3) and "
           "`docs/probes/p2-report.md` §8.", "",
           "| # | Area | Margo | flexEngineV2 alpha (observed) | Result |", "|---|---|---|---|---|"]
    out += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | **{r[4]}** |" for r in rows]
    out += ["", f"Summary: {n['pass']} pass, {n['deviation']} deviation, {n['not-applicable']} not applicable "
                f"(of {len(rows)}). Deviations are known and planned (EV-21 mTLS, EV-25 capabilities/placement/"
                f"parameters/polling, EV-27 Margo layout/compose/status); none is a conformance failure of the "
                f"southbound contract.", "",
            "## Southbound operations exercised", "",
            "Every exchange matched exactly one operation and validated (request and response).", "",
            "| operationId | Request | Statuses seen |", "|---|---|---|"]
    for op in sorted(ops):
        out.append(f"| `{op}` | `{method[op]} {tmpl[op]}` | {', '.join(str(s) for s in sorted(ops[op]))} |")
    notes = sorted({(c.op, n) for _, c in lifecycle["checked"] for n in c.notes if not n.startswith("dropped")})
    out += ["", "## Spec notes (not failures)", "",
            "- Requests dropped by the proxy during the link cut are excluded from validation (no response).",
            "- 503 is documented on every device route since 2026-10-17 (it was missing on onboard, getBundle and the "
            "OCI routes; found by this capture)."]
    out += [f"- `{op}`: {n}; the spec documents no response body." for op, n in notes]
    return "\n".join(out) + "\n"


def test_margo_subset_report_up_to_date(lifecycle):
    rows = _margo(lifecycle)
    assert [r[0] for r in rows] == [f"M{i}" for i in range(1, 12)]
    assert all(r[4] in ("pass", "deviation", "not-applicable") for r in rows), [r for r in rows if r[4] == "FAIL"]
    text = _report(lifecycle)
    old = REPORT.read_text() if REPORT.exists() else None
    if old != text:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text)
    assert old is None or old == text, f"{REPORT} was stale and has been regenerated; review and commit it"

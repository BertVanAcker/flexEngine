"""Scenarios (a)-(j) of probe P2, against real Docker and real processes."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import time

import pytest

from .p2lab import item, metric, running_version
from flexengine2.identity import crypto

PG, DL, CAM = "perception_guard", "detection_logger", "gige_camera"


def v1(dl=True):
    out = [item(PG, "1.0.0", "container", parameters={"score_threshold": 0.6})]
    if dl:
        out.append(item(DL, "1.0.0", "native"))
    return out


def applies(e, dep=None, digest=None, kind="apply"):
    return [a for a in e.actions() if a["action"] == kind and (dep is None or a["dep"] == dep)
            and (digest is None or a["digest"] == digest)]


def digest_of(env, module, version):
    return env.registry["info"][(module, version)]["digest"]


# ---- (a) fresh deploy -----------------------------------------------------------------------------

def test_a_fresh_deploy_two_nodes(env):
    a = env.engine("a").start()
    b = env.engine("b").start()
    env.cloud.wait(lambda: all((env.cloud.device(x.dev)["device"] or {}).get("capabilities") for x in (a, b)),
                   20, what="onboarding + capabilities")
    pa = env.cloud.deploy(a.dev, v1())
    pb = env.cloud.deploy(b.dev, [item(CAM, "1.0.0", "container")])
    ta = env.cloud.wait_converged(a.dev, {"perception-guard": "1.0.0", "detection-logger": "1.0.0"})
    tb = env.cloud.wait_converged(b.dev, {"gige-camera": "1.0.0"})
    metric("a_fresh_deploy_2_nodes_seconds", round(ta - pa["publishedAt"], 3))
    metric("a_fresh_deploy_1_node_seconds", round(tb - pb["publishedAt"], 3))
    assert running_version(a.dev, "perception-guard") == "1.0.0"
    assert running_version(b.dev, "gige-camera") == "1.0.0"
    assert a.units()["detection-logger"]["version"] == "1.0.0"
    assert len(applies(a)) == 2 and len(applies(b)) == 1
    caps = env.cloud.device(a.dev)["device"]["capabilities"]
    assert caps["properties"]["supportedRuntimes"] == ["oci"] and "native" in caps["x-flex"]["drivers"]
    # parameters reach the workload
    env_out = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Env}}", f"flex_{a.dev}_perception-guard"],
                             capture_output=True, text=True).stdout
    assert "FLEX_PARAM_SCORE_THRESHOLD=0.6" in env_out
    # steady state: further polls are answered with 304 (ETag)
    time.sleep(2)
    c = env.cloud.device(a.dev)["counters"]
    assert c.get(f"{a.dev}:desired304", 0) >= 2
    metric("a_engine_rss_kb", a.rss_kb())
    # per-device scoping: device b's key cannot read device a's desired state
    key_b = crypto.load_private(b.dir / "data" / "device.key")
    path = f"/api/v1/devices/{a.dev}/deployments"
    hdr = crypto.sign_request(key_b, b.dev, "GET", path, b"", int(time.time() * 1000))
    conn = http.client.HTTPConnection("127.0.0.1", env.cloud.srv.port, timeout=5)
    conn.request("GET", path, headers={**hdr, "Connection": "close"})
    assert conn.getresponse().status == 403


# ---- (b) update -------------------------------------------------------------------------------------

def test_b_update_v1_to_v2(env):
    e = env.engine("b").start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0", "detection-logger": "1.0.0"})
    pid_v1 = e.units()["detection-logger"]["pid"]
    p = env.cloud.deploy(e.dev, [item(PG, "1.1.0", "container"), item(DL, "1.1.0", "native")])
    assert {d["op"] for d in p["plan"]["diff"]} == {"update"}
    t = env.cloud.wait_converged(e.dev, {"perception-guard": "1.1.0", "detection-logger": "1.1.0"})
    metric("b_update_2_nodes_seconds", round(t - p["publishedAt"], 3))
    assert running_version(e.dev, "perception-guard") == "1.1.0"
    assert e.units()["detection-logger"]["pid"] != pid_v1
    st = e.state()
    assert st["applied"]["perception-guard"]["previous"]["version"] == "1.0.0"
    assert st["manifestVersion"] == 2
    for dep, ver in (("perception-guard", "1.1.0"), ("detection-logger", "1.1.0")):
        assert len([a for a in applies(e, dep) if a["version"] == ver]) == 1
    rep = env.cloud.status(e.dev)["perception-guard"]
    metric("b_container_apply_seconds", rep["applySeconds"])
    metric("b_container_health_gate_seconds", rep["healthGateSeconds"])
    metric("b_native_apply_seconds", env.cloud.status(e.dev)["detection-logger"]["applySeconds"])


V1 = {"perception-guard": "1.0.0", "detection-logger": "1.0.0"}
V2 = {"perception-guard": "1.1.0", "detection-logger": "1.1.0"}


def v2(**params):
    return [item(PG, "1.1.0", "container", parameters=params), item(DL, "1.1.0", "native", parameters=params)]


# ---- (c) network cut mid-rollout ----------------------------------------------------------------

def test_c1_network_cut_during_artefact_download(env):
    e = env.engine("c1", via_shim=True).start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, V1)
    n0 = len(e.actions())
    e.shim.cut_on(lambda line: "/blobs/" in line)  # link drops when the v2 download starts
    env.cloud.deploy(e.dev, v2())
    env.cloud.wait(lambda: e.shim.down, 20, what="cut")
    time.sleep(4)  # offline for 4 s
    assert e.actions()[n0:] == [], "nothing may be applied from a half-downloaded manifest"
    assert running_version(e.dev, "perception-guard") == "1.0.0"
    dropped = sum(1 for _, _, v in e.shim.log if v == "dropped")
    t_restore = time.time()
    e.shim.restore()
    t = env.cloud.wait_converged(e.dev, V2)
    metric("c1_offline_seconds", 4)
    metric("c1_requests_dropped_while_offline", dropped)
    metric("c1_restore_to_converged_seconds", round(t - t_restore, 3))
    for dep in V2:
        assert len(applies(e, dep, digest_of(env, PG if dep == "perception-guard" else DL, "1.1.0"))) == 1


def test_c2_ack_lost_then_link_down(env):
    """Status POST reaches the cloud, the reply is lost and the link stays down: the engine finishes
    the rollout offline (artefacts are local), queues reports, and the cloud de-duplicates on reconnect."""
    e = env.engine("c2", via_shim=True).start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, V1)
    e.shim.cut_on(lambda line: line.startswith("POST") and "/status" in line, deliver_request=True)
    env.cloud.deploy(e.dev, v2())
    env.cloud.wait(lambda: e.shim.down, 20, what="cut")
    env.cloud.wait(lambda: running_version(e.dev, "perception-guard") == "1.1.0"
                   and e.units().get("detection-logger", {}).get("version") == "1.1.0", 30, what="offline apply")
    time.sleep(2)
    assert e.state()["outbox"], "reports must be queued while offline"
    t_restore = time.time()
    e.shim.restore()
    t = env.cloud.wait_converged(e.dev, V2)
    metric("c2_restore_to_status_converged_seconds", round(t - t_restore, 3))
    cnt = env.cloud.device(e.dev)["counters"]
    assert cnt.get(f"{e.dev}:statusDuplicate", 0) >= 1
    for dep in V2:
        assert len([a for a in applies(e, dep) if a["version"] == "1.1.0"]) == 1


def test_c3_cloud_503_retry_after(env):
    e = env.engine("c3").start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, V1)
    env.cloud.admin("POST", "/api/v1/admin/faults", {"unavailableSeconds": 3})
    t0 = time.time()
    env.cloud.deploy(e.dev, v2())
    t = env.cloud.wait_converged(e.dev, V2)
    assert t - t0 >= 2.5
    assert "HTTP 503" in e.log_path.read_text()
    metric("c3_503_window_to_converged_seconds", round(t - t0, 3))


# ---- (d) failed health check -> automatic rollback -------------------------------------------------

@pytest.mark.slow
def test_d_health_fail_rollback(env):
    e = env.engine("d").start()
    env.cloud.deploy(e.dev, v2())
    env.cloud.wait_converged(e.dev, V2)
    p = env.cloud.deploy(e.dev, [item(PG, "1.2.0", "container"), item(DL, "1.2.0", "native")])

    def both_failed():
        st = env.cloud.status(e.dev)
        return all(st[d]["state"] == "Failed" and st[d].get("rolledBackTo") == "1.1.0" for d in V2) and st
    st = env.cloud.wait(both_failed, 60, what="rollback reports")
    assert st["perception-guard"]["rollbackHealthy"] and st["detection-logger"]["rollbackHealthy"]
    assert running_version(e.dev, "perception-guard") == "1.1.0"
    assert e.units()["detection-logger"]["version"] == "1.1.0"
    metric("d_health_timeout_configured_seconds", 8)
    metric("d_container_detect_seconds", st["perception-guard"]["detectSeconds"])
    metric("d_container_total_rollback_seconds", st["perception-guard"]["totalSeconds"])
    metric("d_native_total_rollback_seconds", st["detection-logger"]["totalSeconds"])
    metric("d_publish_to_both_rolled_back_seconds",
           round(max(st[d]["receivedAt"] for d in V2) - p["publishedAt"], 3))
    time.sleep(3)  # the failed digest is not retried in a loop
    assert len(applies(e, "perception-guard", digest_of(env, PG, "1.2.0"))) == 1
    assert len(applies(e, "detection-logger", digest_of(env, DL, "1.2.0"))) == 1
    assert len(applies(e, kind="rollback")) == 2
    # operator reverts the desired state to v2: device reports Installed again without any action
    n = len(e.actions())
    env.cloud.deploy(e.dev, v2())
    env.cloud.wait_converged(e.dev, V2, timeout=20)
    assert e.actions()[n:] == []


# ---- (e) removal by absence --------------------------------------------------------------------------

def test_e_removal_by_absence(env):
    e = env.engine("e").start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, V1)
    pid = e.units()["detection-logger"]["pid"]
    p = env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container", parameters={"score_threshold": 0.6})])
    assert p["plan"]["diff"] == [{"op": "remove", "id": "detection-logger", "from": p["plan"]["diff"][0]["from"]}]
    t = env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0"})
    metric("e_remove_one_seconds", round(t - p["publishedAt"], 3))
    assert "detection-logger" not in e.units()
    assert not os.path.exists(f"/proc/{pid}") or b"FLEX_UNIT" not in open(f"/proc/{pid}/environ", "rb").read()
    env.cloud.deploy(e.dev, [])
    env.cloud.wait_converged(e.dev, {})
    assert running_version(e.dev, "perception-guard") is None
    assert e.state()["applied"] == {}


# ---- (f) tampered / unsigned / wrongly signed artefacts, downgrade -------------------------------------

def test_f_tampered_and_unsigned_rejected(env):
    e = env.engine("f").start()
    env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container")])
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0"})
    layout = env.registry["layout"]
    man = layout.get_manifest(digest_of(env, PG, "1.3.0"))
    blob = layout.blob_path(man["layers"][0]["digest"])
    original = blob.read_bytes()
    bad = bytearray(original)
    bad[len(bad) // 2] ^= 0xFF
    blob.write_bytes(bytes(bad))
    reasons = {}
    try:
        for ver, expect in (("1.3.0", "digest mismatch"), ("1.4.0", "no signature"), ("1.5.0", "untrusted key")):
            env.cloud.deploy(e.dev, [item(PG, ver, "container")])
            st = env.cloud.wait(lambda: (r := env.cloud.status(e.dev)["perception-guard"])["state"] == "Failed"
                                and r.get("version") == ver and r, 30, what=f"reject {ver}")
            assert expect in st["reason"], st["reason"]
            reasons[ver] = st["reason"]
            assert running_version(e.dev, "perception-guard") == "1.0.0"
            assert not applies(e, "perception-guard", digest_of(env, PG, ver))
    finally:
        blob.write_bytes(original)
    metric("f_reject_reasons", reasons)
    # a good release afterwards converges
    env.cloud.deploy(e.dev, [item(PG, "1.1.0", "container")])
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.1.0"})
    # rollback attack: cloud (or a MITM with the old signed manifest) serves an older manifestVersion
    with env.cloud.st.lock:
        env.cloud.st.s["desired"][e.dev]["manifestVersion"] = 1
        env.cloud.st.s["desired"][e.dev]["deployments"] = []
    env.cloud.wait(lambda: e.state().get("downgradesRefused", 0) >= 1, 10, what="downgrade refused")
    assert running_version(e.dev, "perception-guard") == "1.1.0"


def test_f_bad_manifest_signature_ignored(tmp_path, keys):
    from flexengine2.core.reconciler import Engine
    from flexengine2.util import canonical_json
    cfg = {"deviceId": "x", "apiUrl": "http://127.0.0.1:9", "dataDir": str(tmp_path), "onboardingToken": "t",
           "cloudPublicKey": crypto.public_b64(keys["cloud"]), "releasePublicKeys": [crypto.public_b64(keys["release"])]}
    eng = Engine(cfg, drivers={})
    # CP-1-valid since the 2026-10-17 decision (validate before accepting); the probe used a bare document
    doc = canonical_json({"apiVersion": "flex.dev/v1alpha1", "kind": "DesiredState", "deviceId": "x", "manifestVersion": 5,
                          "deployments": []})
    assert not eng.accept_manifest(doc, crypto.sign(keys["rogue"], doc), None)
    assert not eng.accept_manifest(doc, "", None)
    assert eng.accept_manifest(doc, crypto.sign(keys["cloud"], doc), None)
    other = canonical_json({"deviceId": "y", "manifestVersion": 6, "deployments": []})
    assert not eng.accept_manifest(other, crypto.sign(keys["cloud"], other), None)


# ---- (g) activation window / safe state -----------------------------------------------------------

def test_g_activation_waits_for_safe_state(env):
    e = env.engine("g").start()
    env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container")])  # safety-relevant (model): machine is parked
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0"})
    e.set_safe(machine_parked=False, operator_confirmed=False)
    # a non-safety node has no interlock: installs while the machine is running
    env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container"), item(DL, "1.0.0", "native")])
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0", "detection-logger": "1.0.0"})
    # declaring a class other than the model's is refused by the backend (S4)
    s, b = env.cloud.admin("POST", "/api/v1/admin/plans", {"deviceId": e.dev, "deployments": [
        item(PG, "2.0.0", "container", safetyClass="none")]})
    assert s == 422 and "model" in b["title"], b
    safety = [item(PG, "2.0.0", "container", safetyClass="safety-relevant",
                   activation={"requires": ["machine_parked", "operator_confirmed"]}), item(DL, "1.0.0", "native")]
    p = env.cloud.plan(e.dev, safety)
    assert p["requiresApproval"]
    s, b = env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 403, b  # no approval yet
    s, b = env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, user="carol")
    assert s == 403, b  # requester cannot approve own plan
    s, b = env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, user="dave")
    assert s == 200, b
    s, b = env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 200, b
    env.cloud.wait(lambda: env.cloud.status(e.dev)["perception-guard"]["state"] == "Pending", 20, what="pending")
    time.sleep(2)
    assert running_version(e.dev, "perception-guard") == "1.0.0"
    e.set_safe(machine_parked=True)
    time.sleep(2)
    r = env.cloud.status(e.dev)["perception-guard"]
    assert r["state"] == "Pending" and "operator_confirmed" in r["reason"], r
    assert running_version(e.dev, "perception-guard") == "1.0.0"
    t_safe = time.time()
    e.set_safe(operator_confirmed=True)
    t = env.cloud.wait_converged(e.dev, {"perception-guard": "2.0.0", "detection-logger": "1.0.0"})
    metric("g_safe_state_to_installed_seconds", round(t - t_safe, 3))
    # a safety release signed with only one approver is refused on the device (FC-S2 enforced twice)
    env.cloud.deploy(e.dev, [item(PG, "2.0.1", "container", safetyClass="safety-relevant"),
                             item(DL, "1.0.0", "native")], approver="dave")
    r = env.cloud.wait(lambda: (x := env.cloud.status(e.dev)["perception-guard"])["state"] == "Failed" and x, 20)
    assert "two-person" in r["reason"]
    assert running_version(e.dev, "perception-guard") == "2.0.0"
    # audit chain is intact and covers the approval
    entries = env.cloud.admin("GET", "/api/v1/admin/audit")[1]["entries"]
    assert any(x["action"] == "plan.approved" and x["actor"] == "dave" for x in entries)
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev"] == prev["hash"]


def test_g_maintenance_window(env):
    e = env.engine("gw").start()
    now = time.gmtime()
    start = f"{(now.tm_hour + 2) % 24:02d}:00"
    end = f"{(now.tm_hour + 3) % 24:02d}:00"
    env.cloud.deploy(e.dev, [item(DL, "1.0.0", "native", activation={"window": {"start": start, "end": end}})])
    r = env.cloud.wait(lambda: (x := env.cloud.status(e.dev).get("detection-logger")) and x["state"] == "Pending" and x, 20)
    assert "outside activation window" in r["reason"]
    assert not e.units()


# ---- (h) engine restart: persisted state, idempotency, crash mid-rollout -------------------------------

@pytest.mark.slow
def test_h_restart_resumes_idempotently(env):
    e = env.engine("h", via_shim=True).start()
    env.cloud.deploy(e.dev, v1())
    env.cloud.wait_converged(e.dev, V1)
    started = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", f"flex_{e.dev}_perception-guard"],
                             capture_output=True, text=True).stdout
    pid = e.units()["detection-logger"]["pid"]
    n = len(e.actions())
    # 1. plain crash + restart in steady state: nothing is touched
    e.kill()
    e.start()
    time.sleep(3)
    assert e.actions()[n:] == []
    assert subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}}", f"flex_{e.dev}_perception-guard"],
                          capture_output=True, text=True).stdout == started
    assert e.units()["detection-logger"]["pid"] == pid
    # 2. crash in the middle of a rollout (after `docker run`, before the health gate commits)
    p = env.cloud.deploy(e.dev, v2(startup_delay=3))
    env.cloud.wait(lambda: running_version(e.dev, "perception-guard") == "1.1.0", 30, period=0.05, what="v2 started")
    e.kill()
    t_kill = time.time()
    st = e.state()
    assert st["journal"] and st["journal"]["dep"] == "perception-guard"
    assert st["applied"]["perception-guard"]["wl"]["version"] == "1.0.0"  # not committed
    e.start()
    t = env.cloud.wait_converged(e.dev, V2)
    metric("h_crash_restart_to_converged_seconds", round(t - t_kill, 3))
    pg_v2 = [a for a in e.actions() if a["dep"] == "perception-guard" and a["version"] == "1.1.0"]
    assert [a["action"] for a in pg_v2] == ["apply", "adopt"], pg_v2
    assert len([a for a in applies(e, "detection-logger") if a["version"] == "1.1.0"]) == 1
    # 3. restart while offline + drift: the workload vanished while the engine was down
    e.kill()
    e.shim.cut()
    subprocess.run(["docker", "rm", "-f", f"flex_{e.dev}_perception-guard"], capture_output=True)
    e.start()
    env.cloud.wait(lambda: running_version(e.dev, "perception-guard") == "1.1.0", 30, what="offline repair")
    assert e.actions()[-1]["action"] == "repair"
    e.shim.restore()


# ---- (i) offline bundle (UC1: no link at all) --------------------------------------------------------

def test_i_offline_bundle(env):
    dev = f"i-{os.urandom(3).hex()}"
    s, p = env.cloud.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": v1(),
                                                             "allowUnknownDevice": True})
    assert s == 201, p
    assert p["requiresApproval"]  # perception_guard is safety-relevant (model-derived, S4)
    assert env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, user="dave")[0] == 200
    assert env.cloud.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})[0] == 200
    s, bundle = env.cloud.admin("GET", f"/api/v1/admin/devices/{dev}/bundle")
    assert s == 200
    e = env.engine("i", dev=dev, extra={"apiUrl": "http://127.0.0.1:9"})  # nothing listens there
    bpath = e.dir / "bundle.tar"
    bpath.write_bytes(bundle)
    t0 = time.time()
    e.start("--import-bundle", str(bpath))
    env.cloud.wait(lambda: running_version(e.dev, "perception-guard") == "1.0.0"
                   and e.units().get("detection-logger", {}).get("version") == "1.0.0", 60, what="bundle install")
    metric("i_bundle_bytes", len(bundle))
    metric("i_bundle_to_running_seconds", round(time.time() - t0, 3))
    assert e.state()["outbox"]  # reports wait for a link


# ---- (j) supervision ------------------------------------------------------------------------------

def test_j_supervision_restarts(env):
    e = env.engine("j").start()
    env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container", parameters={"crash_after": 3}),
                             item(DL, "1.0.0", "native")])
    env.cloud.wait_converged(e.dev, V1)
    pid = e.units()["detection-logger"]["pid"]
    t0 = time.time()
    os.killpg(pid, signal.SIGKILL)
    env.cloud.wait(lambda: e.units()["detection-logger"].get("restarts") == 1, 15, what="native restart")
    env.cloud.wait(lambda: any(r.get("reason") == "supervisor: restarted" for r in env.cloud.device(e.dev)["statusLog"]),
                   15, what="restart reported")
    metric("j_native_restart_seconds", round(time.time() - t0, 3))
    rc = env.cloud.wait(lambda: int(subprocess.run(["docker", "inspect", "-f", "{{.RestartCount}}",
                                                    f"flex_{e.dev}_perception-guard"], capture_output=True,
                                                   text=True).stdout or 0) >= 1, 20, what="container restart")
    assert rc

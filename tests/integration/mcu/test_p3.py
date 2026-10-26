"""P3 scenarios (a)-(h) on flexEngineV2 (port of probes/p3-mcu-hotload/tests/test_p3.py, same assertions), against
QEMU mps2-an385 running MCUboot + the flexEngine-MCU base image. The engine under test is `flexengine run` (gateway
device + see-thru MCU child in one host process).

Order matters: the tests form one rollout history on one MCU (see mculab.Lab)."""

from __future__ import annotations

import struct
import time

import pytest

from .mculab import B, CHILD, GW, item, metric

pytestmark = [pytest.mark.qemu, pytest.mark.slow]


def servo(lab, version, **kw):
    return item("servo_drive", version, "servo", _interfaceHash=lab.iface["servo_drive"], open_mm=80, close_mm=20, **kw)


def gripper(lab, version="1.0.0"):
    return item("gripper_station", version, "gripper", _interfaceHash=lab.iface["gripper_station"], max_force_n=30)


def base_fw(version):
    return item("flexmcu_base", version, "base-image", driver="mcu-firmware")


def sock(st: dict, dep: str) -> dict:
    return next(s for s in st["sockets"] if s["dep"] == dep)


def running(lab, dep: str, version: str) -> dict:
    s = sock(lab.dev_status(), dep)
    assert s["running"] and s["version"] == version and s["healthy"], s
    return s


# ---- (a) ------------------------------------------------------------------------------------------
def test_a_deploy_v1(lab):
    """Gateway flexEngine deploys two generated modules (C servo_drive, Rust no_std gripper_station) to the
    MCU child; the MCU is a see-thru child device with its own capabilities."""
    metric("mcu_cold_boot_to_ready_s", round(lab.boot_s, 3))
    t0 = lab.deploy([servo(lab, "1.0.0"), gripper(lab)])
    r1 = lab.wait_state("servo", "Installed", "1.0.0")
    r2 = lab.wait_state("gripper", "Installed", "1.0.0")
    metric("a_deploy_two_modules_s", round(max(r1["receivedAt"], r2["receivedAt"]) - t0, 3))
    dm = lab.driver_metrics()
    metric("a_servo_upload", {k: dm["servo"].get(k) for k in ("upload_s", "uploaded_bytes", "rpc_ms", "device")})
    metric("a_gripper_upload", {k: dm["gripper"].get(k) for k in ("upload_s", "uploaded_bytes", "rpc_ms", "device")})
    st = lab.dev_status()
    sv, gr = sock(st, "servo"), sock(st, "gripper")
    assert sv["state"] == "confirmed" and gr["state"] == "confirmed"
    assert sv["running"] == "servo_drive" and gr["running"] == "gripper_station"
    assert sv["iface"] == lab.iface["servo_drive"][7:] and gr["iface"] == lab.iface["gripper_station"][7:]
    # local bus: the Rust gripper core consumes the C servo's JointSample and publishes GripperState
    time.sleep(0.5)
    st = lab.dev_status()
    sv, gr = sock(st, "servo"), sock(st, "gripper")
    assert gr["rx"] > 0 and gr["pubs"] > 0 and gr["errors"] == 0
    js = sv["last_msg"]
    stamp, width, force = struct.unpack_from("<qff", js, 8)
    from flexmodel.runtime import cdr  # reference codec (P0/P1)
    w = cdr.Writer(cdr.XCDR2)
    d = w.dheader_begin()
    w.prim("int64", stamp), w.prim("float32", width), w.prim("float32", force)
    w.dheader_end(d)
    assert bytes(w.buf) == js, "C module's XCDR2 JointSample differs from the reference codec"
    metric("a_llext_bytes", {"servo_drive": sv["llext_bytes"], "gripper_station": gr["llext_bytes"]})
    metric("a_local_bus", {"gripper_rx": gr["rx"], "gripper_pubs": gr["pubs"], "servo_pubs": sv["pubs"]})
    # see-thru child: own device record + capabilities; gateway lists it as a child
    caps = lab.device(CHILD)["device"]["capabilities"]
    assert caps["properties"]["roles"] == ["child-device"]
    assert caps["x-flex"]["parent"] == {"deviceId": GW, "mode": "see-thru"}
    mod = caps["x-flex"]["drivers"]["mcu-module"]
    assert mod["moduleAbi"] == "1.0" and mod["arch"] == "armv7m" and mod["floatAbi"] == "soft"
    assert set(mod["caps"]) == {"pwm", "localbus"} and mod["sockets"] == 2
    gcaps = lab.device(GW)["device"]["capabilities"]
    assert gcaps["properties"]["roles"] == ["gateway"]
    assert gcaps["x-flex"]["children"][0]["deviceId"] == CHILD
    metric("a_child_capabilities", {"roles": caps["properties"]["roles"], "parent": caps["x-flex"]["parent"],
                                    "mcu-module": mod, "mcu-firmware": caps["x-flex"]["drivers"]["mcu-firmware"]})


# ---- (b) ------------------------------------------------------------------------------------------
def test_b_hot_swap_v1_to_v2(lab):
    """Hot swap without full reflash: only the module slot is written, no reset, the other module runs on."""
    slot0 = lab.slot0_sha()
    st0 = lab.dev_status()
    g0 = sock(st0, "gripper")
    t0 = lab.deploy([servo(lab, "2.0.0"), gripper(lab)])
    r = lab.wait_state("servo", "Installed", "2.0.0", after=t0)
    st = lab.dev_status()
    sv, gr = sock(st, "servo"), sock(st, "gripper")
    assert sv["version"] == "2.0.0" and sv["state"] == "confirmed" and sv["healthy"]
    assert lab.slot0_sha() == slot0, "base image region changed: that would be a full reflash"
    assert st["boots"] == st0["boots"] and st["uptime_ms"] > st0["uptime_ms"], "MCU rebooted"
    assert gr["steps"] > g0["steps"] and gr["healthy"] and gr["errors"] == 0, "the other module was disturbed"
    # no false fail-fast revert of a healthy module (D1 gap 2)
    assert sv.get("fault_reverts", 0) == sock(st0, "servo").get("fault_reverts", 0), sv
    assert sv.get("auto_reverts", 0) == sock(st0, "servo").get("auto_reverts", 0), sv
    dm = lab.driver_metrics()["servo"]
    m = sv["m"]
    metric("b_swap", {
        "cloud_apply_to_installed_s": round(r["receivedAt"] - t0, 3),
        "engine_apply_plus_health_s": r.get("applySeconds"), "health_gate_s": r.get("healthGateSeconds"),
        "driver_upload_s": dm["upload_s"], "uploaded_bytes": dm["uploaded_bytes"], "activate_rpc_ms": dm["rpc_ms"],
        "device_at_activate": dm.get("device"),
        "device_verify_us": m["verify_us"], "device_llext_load_us": m["load_us"], "device_init_us": m["init_us"],
        "device_switch_us": m["switch_us"], "device_unload_old_us": m["unload_us"],
        "service_gap_us": m["gap_us"], "step_period_ms": 5,
        "gripper_steps_during_swap": gr["steps"] - g0["steps"], "mcu_reboots": st["boots"] - st0["boots"]})
    assert m["gap_us"] < 20000, f"service gap {m['gap_us']} us"


# ---- (c) ------------------------------------------------------------------------------------------
def test_c_v3_fails_health_reverts_to_v2(lab):
    up0 = sock(lab.dev_status(), "servo")["uploads"]
    t0 = lab.deploy([servo(lab, "3.0.0"), gripper(lab)])
    r = lab.wait_state("servo", "Failed", "3.0.0", after=t0)
    assert r.get("rolledBackTo") == "2.0.0" and r.get("rollbackHealthy") is True, r
    s = running(lab, "servo", "2.0.0")
    assert s["state"] == "confirmed" and s["uploads"] == up0 + 1, "rollback must not re-upload"
    acts = [a["action"] for a in lab.actions() if a["dep"] == "servo" and a["t"] >= t0]
    assert "rollback" in acts and acts.count("apply") == 1, acts
    dm = lab.driver_metrics()["servo"]
    # fail-fast (D1 gap 2): the base image reverts the stalling module itself (step() errors) and the gateway
    # stops waiting at once; the rollback is then a no-op on the device (or the revert RPC if it was quicker)
    assert dm["path"] in ("revert (no upload)", "already active (no device action)"), dm
    assert r.get("failFast") is True and "fault" in (r.get("failure") or ""), r
    window = float((lab.release_health("servo", "3.0.0") or {}).get("timeoutSeconds", 4))
    assert r["detectSeconds"] < window, (r["detectSeconds"], window)
    assert s.get("fault_reverts", 0) >= 1 and "fail-fast" in s.get("last_reason", ""), s
    metric("c_rollback", {"cloud_apply_to_failed_s": round(r["receivedAt"] - t0, 3), "detect_s": r.get("detectSeconds"),
                          "total_s": r.get("totalSeconds"), "window_s": window, "fail_fast": r.get("failFast"),
                          "failure": r.get("failure"), "rollback_path": dm["path"], "revert_rpc_ms": dm.get("rpc_ms"),
                          "device_switch_back_us": s["m"]["switch_us"], "reason": r.get("reason"),
                          "device_reverts": s["reverts"], "device_fault_reverts": s.get("fault_reverts")})
    # operator reverts the desired state to v2: engine reports Installed without touching the device
    n = len(lab.actions())
    t1 = lab.deploy([servo(lab, "2.0.0"), gripper(lab)])
    lab.wait_state("servo", "Installed", "2.0.0", after=t1)
    assert not [a for a in lab.actions()[n:] if a["action"] in ("apply", "rollback")]


# ---- (d) ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("version,needle", [
    ("4.0.0-abi2", "module ABI 2.0 incompatible with base ABI 1.0"),
    ("4.0.0-m33", "built for armv8m.main/hard-float"),
    ("5.0.0-iface", "interface hash mismatch"),
])
def test_d_incompatible_rejected_before_upload(lab, version, needle):
    s0 = sock(lab.dev_status(), "servo")
    t0 = lab.deploy([servo(lab, version), gripper(lab)])
    r = lab.wait_state("servo", "Failed", version, after=t0)
    assert r["reason"].startswith("artefact rejected:") and needle in r["reason"], r["reason"]
    assert r.get("stage") == "preflight"
    s = running(lab, "servo", "2.0.0")
    assert s["uploads"] == s0["uploads"], "something was sent to the MCU"
    metric(f"d_reject_{version}", {"reason": r["reason"], "cloud_apply_to_failed_s": round(r["receivedAt"] - t0, 3)})
    t1 = lab.deploy([servo(lab, "2.0.0"), gripper(lab)])
    lab.wait_state("servo", "Installed", "2.0.0", after=t1)


def raw_upload(lab, sock_id: int, data: bytes) -> dict:
    l = lab.smp()
    try:
        off, r = 0, {}
        while off < len(data):
            kw = {"len": len(data)} if off == 0 else {}
            r = l.flexmod("upload", sock=sock_id, off=off, data=data[off:off + 768], **kw)
            if r.get("rc"):
                return r
            off = r["off"]
        return r
    finally:
        l.close()


def test_d2_device_rejects_on_its_own(lab):
    """Defense in depth: the base image refuses the same modules when the gateway checks are bypassed."""
    sid = sock(lab.dev_status(), "servo")["sock"]
    out = {}
    for f, needle in [("servo_drive-abi2", "module ABI 2.0"), ("servo_drive-m33hard", "module arch 2 float 2"),
                      ("servo_drive-rogue", "bad module signature"), ("servo_drive-unsigned", "module not signed")]:
        r = raw_upload(lab, sid, (B / "modules" / f"{f}.flxm").read_bytes())
        assert r.get("rc") == -1 and needle in r["reason"], (f, r)
        out[f] = r["reason"]
    data = bytearray((B / "modules" / "servo_drive-2.0.0.flxm").read_bytes())
    data[1000] ^= 0x40
    r = raw_upload(lab, sid, bytes(data))
    assert r.get("rc") == -1 and "SHA-256" in r["reason"], r
    out["bit-flipped ELF"] = r["reason"]
    l = lab.smp()
    try:  # activating the (erased) slot must fail too
        st = l.flexmod("status")
        s = sock(st, "servo")
        r = l.flexmod("activate", sock=sid, slot=1 - s["active"], dep="servo", test=True)
        assert r["rc"] == -1, r
        out["activate erased slot"] = r["reason"]
    finally:
        l.close()
    running(lab, "servo", "2.0.0")
    metric("d2_device_side_rejections", out)


# ---- (e) ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("version,needle", [
    ("6.0.2-nosig", "release not signed by a trusted key"),
    ("6.0.3-tamper", "digest mismatch"),
    ("6.0.0-rogue", "bad module signature"),
    ("6.0.1-unsigned", "module not signed"),
])
def test_e_unsigned_or_tampered_rejected(lab, version, needle):
    if version == "6.0.3-tamper" and not lab.tampered:
        pytest.skip("could not tamper with the payload blob")
    s0 = sock(lab.dev_status(), "servo")
    t0 = lab.deploy([servo(lab, version), gripper(lab)])
    r = lab.wait_state("servo", "Failed", version, after=t0)
    assert needle in r["reason"], r["reason"]
    s = running(lab, "servo", "2.0.0")
    assert s["uploads"] == s0["uploads"]
    metric(f"e_reject_{version}", r["reason"])
    t1 = lab.deploy([servo(lab, "2.0.0"), gripper(lab)])
    lab.wait_state("servo", "Installed", "2.0.0", after=t1)


# ---- (f) ------------------------------------------------------------------------------------------
def test_f_full_image_update_confirm_and_revert(lab):
    slot0 = lab.slot0_sha()
    t0 = lab.deploy([servo(lab, "2.0.0"), gripper(lab), base_fw("1.1.0")])
    r = lab.wait_state("base-image", "Installed", "1.1.0", after=t0, timeout=180)
    info = lab.dev_info()
    assert info["image"].startswith("1.1.0") and info["confirmed"] is True and info["base"] == "1.1.0"
    assert lab.slot0_sha() != slot0
    running(lab, "servo", "2.0.0"), running(lab, "gripper", "1.0.0")  # modules survive the base update
    fm = lab.fw_metrics()["1.1.0"]
    metric("f_full_image_update", {"cloud_apply_to_installed_s": round(r["receivedAt"] - t0, 3),
                                   "upload_s": fm.get("upload_s"), "uploaded_bytes": fm.get("uploaded_bytes"),
                                   "reset_swap_boot_s": fm.get("reset_s"), "health_gate_s": r.get("healthGateSeconds"),
                                   "path": fm.get("path")})
    mark = lab.mcu.mark()
    t1 = lab.deploy([servo(lab, "2.0.0"), gripper(lab), base_fw("1.2.0")])
    r = lab.wait_state("base-image", "Failed", "1.2.0", after=t1, timeout=180)
    assert r.get("rolledBackTo") == "1.1.0" and r.get("rollbackHealthy") is True, r
    info = lab.dev_info()
    assert info["image"].startswith("1.1.0") and info["confirmed"] is True
    con = lab.mcu.console_text(mark)
    assert "Swap type: test" in con and "Swap type: revert" in con, con[-2000:]
    running(lab, "servo", "2.0.0"), running(lab, "gripper", "1.0.0")
    fm = lab.fw_metrics()
    metric("f_full_image_revert", {"cloud_apply_to_failed_s": round(r["receivedAt"] - t1, 3),
                                   "detect_s": r.get("detectSeconds"), "total_s": r.get("totalSeconds"),
                                   "upload_s": fm["1.2.0"].get("upload_s"), "revert_reset_s": fm["1.1.0"].get("reset_s"),
                                   "revert_path": fm["1.1.0"].get("path")})


# ---- (g) ------------------------------------------------------------------------------------------
def test_g_power_cycle_boots_last_committed(lab):
    lab.mcu.stop()  # power cut
    time.sleep(1.0)
    mark = lab.mcu.mark()
    t0 = time.time()
    lab.mcu.start()
    con = lab.mcu.wait_console("socket 1 -> slot", 30, since=mark)
    t_mods = time.time() - t0
    assert "image confirmed" in con and "flexEngine-MCU base 1.1.0" in con
    s = running(lab, "servo", "2.0.0")
    running(lab, "gripper", "1.0.0")
    metric("g_power_cycle", {"power_on_to_modules_running_s": round(t_mods, 3)})
    # power cut while a module is in TESTING: the base image reverts at boot, like MCUboot does for images
    lab.stop_gateway()
    sid = s["sock"]
    r = raw_upload(lab, sid, (B / "modules" / "servo_drive-1.0.0.flxm").read_bytes())
    assert r.get("rc", 0) == 0, r
    l = lab.smp()
    try:
        st = l.flexmod("status")
        slot = next(i for i, sl in enumerate(sock(st, "servo")["slots"]) if sl.get("version") == "1.0.0")
        r = l.flexmod("activate", sock=sid, slot=slot, dep="servo", test=True)
        assert r["rc"] == 0 and r["state"] == "testing", r
    finally:
        l.close()
    assert sock(lab.dev_status(), "servo")["version"] == "1.0.0"
    lab.mcu.stop()
    mark = lab.mcu.mark()
    lab.mcu.start()
    con = lab.mcu.wait_console("socket 1 -> slot", 30, since=mark)
    assert "was testing slot" in con, con
    s = running(lab, "servo", "2.0.0")
    assert s["state"] == "confirmed" and s["auto_reverts"] >= 1
    lab.start_gateway()
    t1 = lab.deploy([servo(lab, "2.0.0"), gripper(lab), base_fw("1.1.0")])
    lab.wait(lambda: all((lab.status().get(d) or {}).get("state") == "Installed" for d in ("servo", "gripper", "base-image")),
             60, what="converged after power cycles")
    metric("g_testing_power_cut", {"revert_at_boot": True, "auto_reverts": s["auto_reverts"]})


# ---- (h) ------------------------------------------------------------------------------------------
def test_h_footprint(lab):
    import json
    import re
    out = {}
    for name in ("mcuboot", "base-1.1.0"):
        log = (B / (f"{name}.build.log" if name != "mcuboot" else "mcuboot.build.log")).read_text()
        fl = re.search(r"FLASH:\s+(\d+) B", log)
        ram = re.search(r"RAM:\s+(\d+) B", log)
        out[name] = {"flash_B": int(fl.group(1)), "ram_B": int(ram.group(1))}
    out["base-1.1.0"]["ram_of_which_llext_heap_B"] = lab.dev_info()["llext_heap"]
    st = lab.dev_status()
    for f in ("servo_drive-2.0.0", "gripper_station-1.0.0"):
        j = json.loads((B / "modules" / f"{f}.json").read_text())
        out[f] = {"flxm_B": j["size"], "elf_text_B": j["elf"]["text"], "elf_data_B": j["elf"]["data"],
                  "elf_bss_B": j["elf"]["bss"], "elf_alloc_B": j["elf"]["alloc"]}
    out["servo_drive-2.0.0"]["llext_heap_used_B"] = sock(st, "servo")["llext_bytes"]
    out["gripper_station-1.0.0"]["llext_heap_used_B"] = sock(st, "gripper")["llext_bytes"]
    metric("h_footprint", out)
    assert out["base-1.1.0"]["flash_B"] < 128 * 1024

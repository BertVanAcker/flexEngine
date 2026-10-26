"""D1 part A, IG-05/IG-07/IG-08 on flexEngineV2 (port of demo/edge/tests/test_edge.py, same assertions): the one-process
host (`flexengine run`) against the reference cloud fake, with releases from `flex release`.

(1) plc-verify over ADS (CX stand-in) and Modbus (Opta stand-in), the hold gate for GP releases, fail-fast on a
    hash mismatch (plan step 13 "fault C: skew"), re-release after provisioning;
(2) the Portenta child on QEMU mps2/an385: multi-arch module releases (the gateway picks the cortex-m3 layer), the
    M7-only release refused before upload (M11), and the IG-07 relay (status -> rmw_zenoh keys at <= 10 Hz, inject
    and the GripCommand service onto the MCU local bus)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import pytest
import yaml

from flexengine2.testing.edge_lab import clean_env

from .edgelab import DP, P3, P3B, Lab, admin, metric

PLC_T = [{"name": "cx_bridge", "kind": "plc", "domain": "panda/cx", "version": "1.0.0"},
         {"name": "opta_stack_light", "kind": "plc", "device": "opta", "version": "1.0.0"}]
ADS_CONN = {"via": "ads", "amsNetId": "127.0.0.1.1.1", "ip": "127.0.0.1", "amsPort": 851, "localNetId": "127.0.0.1.1.2",
            "timeout": 1.0}


def plan(lab: Lab, dev: str, items: list[dict]) -> float:
    """Plan + (second-person approval when the stub asks for it) + apply."""
    s, p = admin(lab.cloud, "POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items})
    assert s == 201, p
    if p["requiresApproval"]:
        s, b = admin(lab.cloud, "POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, user="dave")
        assert s == 200, b
    s, b = admin(lab.cloud, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 200, b
    return time.time()


def item(dep_id, module, version, driver, **kw):
    return {"id": dep_id, "module": module, "version": version, "driver": driver, **kw}


@pytest.fixture
def plcs():
    from sim.ads_plc import AdsPlc
    from sim.modbus_plc import ModbusPlc

    made = []

    def make(cx_hash, opta_hash, mb_port=15022):
        a = AdsPlc(cx_hash).start()
        m = ModbusPlc(opta_hash, port=mb_port).start()
        made.extend([a, m])
        return a, m
    yield make
    for x in made:
        x.stop()


def bridge_changed_model(tmp) -> object:
    """Model copy whose CX bridge layout differs (a new interlock parameter adds to params_interlock)."""
    m = tmp / "model-v2"
    shutil.copytree(DP, m)
    p = m / "modules" / "cell_interlock.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["spec"]["parameters"].append({"name": "door_debounce_ms", "type": "uint32", "default": 20, "min": 0, "max": 500})
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return m


def test_plc_verify_hold_gate_and_skew(tmp_path, plcs):
    lab = Lab("plc")
    try:
        rel = lab.release(PLC_T)
        cx1, opta = rel["cx_bridge"]["interfaceHash"], rel["opta_stack_light"]["interfaceHash"]
        ads, mb = plcs(cx1, opta)
        lab.native_release("cell_controller", "1.0.0")
        lab.native_release("cell_controller", "1.1.0")
        safe = lab.dir / "safe.json"   # the CX release carries the safety gateway: implicit safe-state interlock
        safe.write_text(json.dumps({"machine_parked": True, "operator_confirmed": True}))
        cfg = lab.edge_config([{"deviceId": "panda", "role": "gateway", "onboardingToken": lab.token("panda"),
                                "safeStateFile": str(safe),
                                "drivers": {"native": True, "plc-verify": {"plcs": {
                                    "panda/cx": ADS_CONN, "opta": {"via": "modbus", "host": "127.0.0.1",
                                                                    "port": 15022, "timeout": 1.0}}}}}])
        lab.start_edge(cfg)
        lab.wait(lambda: "panda" in [d["deviceId"] for d in admin(lab.cloud, "GET", "/api/v1/admin/devices")[1]["devices"]],
                 30, what="onboarded")
        caps = lab.wait(lambda: lab.device("panda")["device"].get("capabilities"), 20, what="capabilities")
        assert caps["x-flex"]["drivers"]["plc-verify"] == {"profile": "plc-verify", "driver": "plc-verify",
                                                            "download": False, "plcs": {"opta": "modbus", "panda/cx": "ads"}}
        # v1.0 steady state: both PLCs verified, the cell (needs the CX at bridge hash cx1) follows
        t0 = plan(lab, "panda", [
            item("cx-bridge", "pick_cell_bridge", "1.0.0", "plc-verify"),
            item("opta-stack", "stack_light", "1.0.0", "plc-verify"),
            item("cell", "cell_controller", "1.0.0", "native", activation={"requires": [f"plc:cx-bridge@{cx1}"]})])
        r_cx = lab.wait_state("panda", "cx-bridge", "Installed", "1.0.0", after=t0)
        r_op = lab.wait_state("panda", "opta-stack", "Installed", "1.0.0", after=t0)
        r_ce = lab.wait_state("panda", "cell", "Installed", "1.0.0", after=t0)
        metric("plc_v1_apply_to_installed_s", {"cx-bridge (ADS)": round(r_cx["receivedAt"] - t0, 3),
                                               "opta-stack (Modbus)": round(r_op["receivedAt"] - t0, 3),
                                               "cell (held until CX verified)": round(r_ce["receivedAt"] - t0, 3)})
        assert r_cx["interfaceHash"] == cx1 and ads.calls >= 1
        acts = [a["action"] for a in lab.actions("panda") if a["dep"] == "cx-bridge"]
        assert acts == ["apply"], acts            # verify only: no download, no commit, nothing else

        # fault C: CX bridge 1.1.0 (new layout) released, the CX still runs the old program -> Failed at once,
        # expectation rolled back, the cell release that needs the new layout is held
        v2 = bridge_changed_model(tmp_path)
        rel2 = lab.release([{**PLC_T[0], "version": "1.1.0"}], model=v2)
        cx2 = rel2["cx_bridge"]["interfaceHash"]
        assert cx2 != cx1
        t1 = plan(lab, "panda", [
            item("cx-bridge", "pick_cell_bridge", "1.1.0", "plc-verify"),
            item("opta-stack", "stack_light", "1.0.0", "plc-verify"),
            item("cell", "cell_controller", "1.1.0", "native", activation={"requires": [f"plc:cx-bridge@{cx2}"]})])
        r = lab.wait_state("panda", "cx-bridge", "Failed", "1.1.0", after=t1)
        assert r["failFast"] is True and r["detectSeconds"] < 3.0, r
        assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True and "program not provisioned" in r["failure"]
        metric("plc_skew_fault_c", {"apply_to_failed_s": round(r["receivedAt"] - t1, 3), "detect_s": r["detectSeconds"],
                                    "total_s": r["totalSeconds"], "failFast": r["failFast"], "failure": r["failure"]})
        p = lab.wait_state("panda", "cell", "Pending", "1.1.0", after=t1)
        assert "held: PLC not verified" in p["reason"]
        time.sleep(1.5)
        assert lab.status("panda")["cell"]["state"] == "Pending"        # still held, 1.0.0 keeps running
        assert not [a for a in lab.actions("panda") if a["dep"] == "cell" and a["t"] >= t1]

        # the CX is provisioned by hand (XAE) with the new program; a re-release verifies and releases the hold
        ads.hash = cx2
        lab.release([{**PLC_T[0], "version": "1.1.1"}], model=v2, force=True)  # hash unchanged: forced re-release
        t2 = plan(lab, "panda", [
            item("cx-bridge", "pick_cell_bridge", "1.1.1", "plc-verify"),
            item("opta-stack", "stack_light", "1.0.0", "plc-verify"),
            item("cell", "cell_controller", "1.1.0", "native", activation={"requires": [f"plc:cx-bridge@{cx2}"]})])
        lab.wait_state("panda", "cx-bridge", "Installed", "1.1.1", after=t2)
        lab.wait_state("panda", "cell", "Installed", "1.1.0", after=t2, timeout=60)

        # drift: the Opta program is replaced behind flexEngine's back -> reported unhealthy (supervision)
        mb.set_hash("FLEX01_" + "00" * 32)
        lab.wait(lambda: lab.status("panda")["opta-stack"].get("health") == "unhealthy", 20, what="opta unhealthy")
    finally:
        lab.close()
    lab.check_clean()   # the engine under test was flexengine2 only


# ---------------------------------------------------------------------------------------------------- MCU + relay
def have_qemu() -> bool:
    return shutil.which("qemu-system-arm") is not None and (P3B / "base-1.0.0.signed.bin").exists() \
        and (P3B / "mcuboot" / "zephyr" / "zephyr.bin").exists() and (P3B / "edk" / "flexmcu-edk").exists()


@pytest.mark.skipif(not have_qemu(), reason="P3 QEMU build missing (probes/p3-mcu-hotload/tools/build_all.sh)")
def test_mcu_child_multiarch_and_relay(tmp_path):
    import zenoh

    sys.path.insert(0, str(P3))  # QEMU control + SMP oracle (stand-ins)
    from flexp3 import flxm
    from flexp3.qemu import Mcu, free_port, provision
    from flexp3.smp_link import SmpLink

    from flexengine2.gateway.relay import Codec, mcu_ports
    from flexmodel.runtime import cdr
    from flexmodel.workspace import Workspace

    lab = Lab("mcu")
    mcu = None
    session = None
    try:
        srcs = {"src": str(P3 / "agent_src" / "servo_drive"), "defines": ["SERVO_GAIN=0.05f"]}
        rel = lab.release([
            {"name": "servo_drive", "kind": "mcu-module", "instance": "servo", "version": "2.0.0", **srcs},
            {"name": "gripper_station", "kind": "mcu-module", "instance": "gripper", "version": "1.0.0",
             "rust_src": str(P3.parent / "p1-model-to-targets" / "agent_src" / "rust-nostd" / "src")}])
        rel_m7 = lab.release([{"name": "servo_m7", "kind": "mcu-module", "instance": "servo", "version": "2.0.1",
                               "profiles": ["cortex-m7-hard"], **srcs}])
        flash = lab.dir / "mcu" / "flash.img"
        flash.parent.mkdir(parents=True)
        provision(flash, P3B / "mcuboot" / "zephyr" / "zephyr.bin", P3B / "base-1.0.0.signed.bin")
        mcu = Mcu(lab.dir / "mcu", flash)
        mcu.start()
        zport = free_port()
        pub = flxm.raw_public(flxm.load_private(P3 / "work" / "keys" / "module-ec-p256.pem")).hex()
        cfg = lab.edge_config([
            {"deviceId": "panda", "role": "gateway", "onboardingToken": lab.token("panda"), "drivers": {}},
            {"deviceId": "panda.portenta", "parent": "panda", "modelDevice": "portenta",
             "onboardingToken": lab.token("panda.portenta"), "link": "smp/uart (QEMU tcp)",
             "mcu": {"smp": {"host": "127.0.0.1", "port": mcu.port}, "modulePublicKey": pub},
             "relay": {"enabled": True, "maxRateHz": 10, "listen": f"tcp/127.0.0.1:{zport}", "connect": None}}])
        lab.start_edge(cfg)
        child = "panda.portenta"
        iface = {n: rel[n]["interfaceHash"] for n in ("servo_drive", "gripper_station")}
        servo = item("servo", "servo_drive", "2.0.0", "mcu-module",
                     parameters={"_interfaceHash": iface["servo_drive"], "open_mm": 80, "close_mm": 20})
        grip = item("gripper", "gripper_station", "1.0.0", "mcu-module",
                    parameters={"_interfaceHash": iface["gripper_station"], "max_force_n": 30})
        lab.wait(lambda: child in [d["deviceId"] for d in admin(lab.cloud, "GET", "/api/v1/admin/devices")[1]["devices"]],
                 40, what="child onboarded")
        t0 = plan(lab, child, [servo, grip])
        r_s = lab.wait_state(child, "servo", "Installed", "2.0.0", after=t0, timeout=120)
        r_g = lab.wait_state(child, "gripper", "Installed", "1.0.0", after=t0, timeout=120)
        metric("mcu_deploy_two_modules_s", round(max(r_s["receivedAt"], r_g["receivedAt"]) - t0, 3))
        # per-arch selection: the gateway uploaded the cortex-m3 / soft-float layer of the two-layer release
        m = json.loads((lab.dir / "data" / child / "mcu-module-driver.json").read_text())["metrics"]
        sizes = {l["cpuProfile"]: l["bytes"] for l in rel["servo_drive"]["layers"]}
        assert m["servo"]["uploaded_bytes"] == sizes["cortex-m3"] != sizes["cortex-m7-hard"]
        caps = lab.device(child)["device"]["capabilities"]
        assert caps["x-flex"]["drivers"]["mcu-module"]["floatAbi"] == "soft"
        assert caps["x-flex"]["parent"] == {"deviceId": "panda", "mode": "see-thru"}

        # M11: a release with only the M7 hard-float layer is refused before anything reaches the MCU
        t1 = plan(lab, child, [{**servo, "version": "2.0.1"}, grip])
        r = lab.wait_state(child, "servo", "Failed", "2.0.1", after=t1)
        assert r["stage"] == "preflight" and "hard-float" in r["reason"], r
        metric("mcu_m7_only_refused", {"apply_to_failed_s": round(r["receivedAt"] - t1, 3), "reason": r["reason"]})
        t2 = plan(lab, child, [servo, grip])
        lab.wait_state(child, "servo", "Installed", "2.0.0", after=t2)

        # IG-07 relay: gripper state on its rmw_zenoh key, XCDR1, <= 10 Hz
        ws = Workspace.load(DP)
        ports = {(p.instance, p.port): p for p in mcu_ports(ws, "d_pick_lab", "portenta")}
        state_key = ports[("gripper", "state")].key
        assert state_key.startswith("0/gripper_station/state/uc2::msg::dds_::GripperState_/RIHS01_")
        codec = Codec(DP, lab.dir / "codec")
        GS = codec.cls("uc2/GripperState")
        got = []
        conf = {"mode": "peer", "scouting": {"multicast": {"enabled": False}},
                "connect": {"endpoints": [f"tcp/127.0.0.1:{zport}"]}, "listen": {"endpoints": ["tcp/127.0.0.1:0"]}}
        session = zenoh.open(zenoh.Config.from_json5(json.dumps(conf)))
        session.declare_subscriber(state_key, lambda s: got.append((time.time(), bytes(s.payload.to_bytes()))))
        lab.wait(lambda: len(got) >= 3, 20, what="relayed gripper state")
        n0, t_0 = len(got), time.time()
        time.sleep(3.0)
        rate = (len(got) - n0) / (time.time() - t_0)
        metric("relay_state_rate_hz", round(rate, 2))
        # <= 10 Hz cap; the floor is what QEMU's emulated UART allows under load: one `flexmod status` takes 0.3-0.6 s,
        # and 3 s hold only 5-8 samples (1.7-2.7 Hz measured; D1 SIL harness runs alongside on the same VM)
        assert 1.0 <= rate <= 11.0, rate
        metric("mcu_layer_bytes", {"uploaded (cortex-m3)": m["servo"]["uploaded_bytes"], "release layers": sizes})
        msg = cdr.decode(GS, got[-1][1])
        assert got[-1][1][:2] == b"\x00\x01" and msg.phase is not None      # XCDR1 (CDR_LE) for ROS 2

        # inject: a JointSample put on the gripper's subscription key reaches the module on the MCU
        def dev_status():
            l = SmpLink("127.0.0.1", mcu.port, timeout=10).connect()
            try:
                return l.flexmod("status")
            finally:
                l.close()
        # (the oracle shares the single QEMU UART with the engine: take turns)
        JS = codec.cls("uc2/JointSample")
        session.put(ports[("gripper", "joint")].key, cdr.encode(JS(stamp_ns=1, width_mm=55.0, force_n=1.0), cdr.XCDR1))
        # ROS 2 service call into the MCU: GripCommand(close) -> relay receipt, module acts on the local bus
        GC, GA = codec.cls("uc2/GripCommand"), codec.cls("uc2/GripAck")
        req = cdr.encode(GC(seq=7, mode=codec.cls("uc2/GripMode").close, max_force_n=25.0), cdr.XCDR1)
        replies = list(session.get(ports[("gripper", "command")].key, payload=req, timeout=5.0))
        ok = [x.ok for x in replies if x.ok is not None]
        assert ok, replies
        ack = cdr.decode(GA, bytes(ok[0].payload.to_bytes()))
        assert ack.seq == 7 and ack.accepted and ack.reason == "relayed"
        t_cmd = time.time()
        lab.wait(lambda: any(cdr.decode(GS, b).phase.name in ("closing", "holding", "fault") for _, b in got[-5:]),
                 15, what="gripper reacts to the injected GripCommand")
        metric("relay_command_to_state_change_s", round(time.time() - t_cmd, 3))
        lab.stop_edge()
        st = dev_status()
        g = next(s for s in st["sockets"] if s["dep"] == "gripper")
        assert g["injected"] >= 2, g     # JointSample + GripCommand delivered by `flexmod inject`

        # the CLI path (scenario driver): one inject without the engine (`flexengine` has no relay-inject yet: the
        # test-support entry runs the same V2 McuRelay code path as `python -m flexedge relay-inject`)
        out = subprocess.run([sys.executable, "-m", "flexengine2.testing.edge_lab", "relay-inject",
                              "--smp", f"127.0.0.1:{mcu.port}", "--model", str(DP),
                              "--instance", "gripper", "--port", "joint", "--work", str(lab.dir / "cli"),
                              "--json", json.dumps({"stamp_ns": 2, "width_mm": 50.0, "force_n": 2.0})],
                             cwd=lab.dir, capture_output=True, text=True, timeout=120, env=clean_env())
        assert out.returncode == 0, out.stderr[-2000:]
        assert json.loads(out.stdout.strip().splitlines()[-1])["delivered"] == 1
    finally:
        if session is not None:
            session.close()
        lab.close()
        if mcu is not None:
            mcu.stop()
    lab.check_clean()   # the engine under test was flexengine2 only

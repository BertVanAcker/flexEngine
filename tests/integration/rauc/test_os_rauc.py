"""EV-17 engine flow on a simulated A/B device: install -> reboot (engine killed) -> adopt -> health gate ->
mark-good -> Installed; bad images roll back to the old slot; power cuts converge; preflight refuses a data dir
outside the shared partition. L3-P2 s9 acc-7..9 / EV-HW-11 run the same steps on the LattePanda eMMC."""

from __future__ import annotations

import time

from .rauclab import DEP


def booted(dev) -> tuple[str, str | None]:
    st = dev.lab.show()
    return st["booted"], dev.lab.booted_version()


def test_install_reboot_adopt_commit_then_update(make_device):
    dev = make_device()
    dev.release("1.0.0")
    dev.release("2.0.0")
    dev.start()
    dev.deploy("1.0.0")
    dev.wait_reboot_request()
    pre = dev.engine_state()
    assert pre["journal"]["phase"] == "applying" and pre["applied"] == {}
    mv = pre["manifestVersion"]
    assert any(r["state"] == "Installing" for r in pre["outbox"]) or dev.report()["state"] == "Installing"
    dev.reboot()
    r = dev.wait_state("Installed", "1.0.0")
    assert r["health"] == "healthy"
    assert booted(dev) == ("B", "1.0.0")
    st = dev.lab.show()
    assert st["slots"]["B"]["ok"] and st["slots"]["B"]["tries"] == 0, st["slots"]["B"]   # rauc status mark-good
    acts = dev.actions()
    assert acts.count("apply") == 1 and "adopt" in acts and "commit" in acts, acts      # adopted after the reboot
    es = dev.engine_state()
    assert es["manifestVersion"] == mv and es["journal"] is None                          # survived the slot switch
    log = [x["state"] for x in dev.cloud.log(dev.dev) if x["deploymentId"] == DEP]
    assert log[:1] == ["Installing"] and log[-1] == "Installed", log                      # outbox survived
    assert dev.lab.installs() == 1
    # update: the other slot, same dance
    dev.install_and_reboot("2.0.0")
    assert booted(dev) == ("A", "2.0.0")
    assert dev.lab.installs() == 2


def test_health_gate_fails_reboots_back_without_commit(make_device):
    """New OS boots but its health check fails: no mark-good; mark-bad + reboot into the old slot; Failed."""
    dev = make_device()
    dev.release("1.0.0")
    dev.release("3.0.0", healthy=False)
    dev.start()
    dev.install_and_reboot("1.0.0")
    dev.deploy("3.0.0")
    dev.wait_reboot_request()
    dev.reboot()                                   # into slot A with 3.0.0: adopt, gate fails, rollback reboot
    dev.wait_reboot_request(60)
    st = dev.lab.show()
    assert st["primary"] == "B" and not st["slots"]["A"]["ok"], st   # mark-bad booted + mark-active other
    dev.reboot()                                   # back into slot B (1.0.0)
    r = dev.wait_state("Failed", "3.0.0")
    assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True, r
    # reported by the engine on the old slot from the journal (the rollback reboot ended the gate's engine)
    assert r["reason"] == "health check failed" and r["failure"].startswith("timeout after"), r
    assert r["reasonCode"] == "FE-REC-004" and r["detectSeconds"] >= 6, r
    assert "rollback-resume" in dev.actions() and dev.engine_state()["journal"] is None
    assert booted(dev) == ("B", "1.0.0")
    assert dev.lab.installs() == 2                  # 3.0.0 not installed a second time
    time.sleep(2)
    assert dev.report()["state"] == "Failed" and dev.lab.installs() == 2 and booted(dev)[0] == "B"


def test_bootloader_falls_back_when_the_image_does_not_boot(make_device):
    """A rootfs without the engine never comes up: the boot counter runs out, the bootloader boots the old slot,
    the engine there reports Failed (fail-fast) and keeps the old OS."""
    dev = make_device()
    dev.release("1.0.0")
    dev.release("4.0.0", bootable=False)
    dev.start()
    dev.install_and_reboot("1.0.0")
    dev.deploy("4.0.0")
    dev.wait_reboot_request()
    st = dev.reboot()
    assert st["booted"] == "B" and st["slots"]["A"]["tries"] >= st["maxTries"], st      # fell back
    r = dev.wait_state("Failed", "4.0.0")
    assert r["rolledBackTo"] == "1.0.0" and r["failFast"] is True and r["reason"] == "apply failed", r
    assert "fell back" in r["failure"] and r["reasonCode"] == "FE-REC-003", r
    assert booted(dev) == ("B", "1.0.0")
    assert dev.lab.installs() == 2


def test_power_cut_during_install_converges(make_device):
    dev = make_device()
    dev.release("1.0.0")
    dev.release("2.0.0")
    dev.start()
    dev.install_and_reboot("1.0.0")
    dev.lab.set(installDelay=4.0)
    dev.deploy("2.0.0")
    dev.cloud.wait(dev.lab.install_started, 30, what="rauc install of 2.0.0 started")
    time.sleep(1.0)
    st = dev.power_cut()                           # torn slot A, primary still B
    assert st["booted"] == "B", st
    dev.lab.set(installDelay=0)
    dev.start()
    dev.wait_reboot_request()
    dev.reboot()
    dev.wait_state("Installed", "2.0.0")
    assert booted(dev) == ("A", "2.0.0")


def test_power_cut_before_mark_good_converges(make_device):
    """Killed during the health gate soak (before mark-good): the boot counter still allows the new slot, the engine
    adopts again after the power cycle and commits."""
    dev = make_device()
    dev.release("1.0.0")
    dev.release("2.0.0", health={"type": "os-rauc", "periodSeconds": 0.25, "successThreshold": 1,
                                 "timeoutSeconds": 30, "minSeconds": 3})
    dev.start()
    dev.install_and_reboot("1.0.0")
    dev.deploy("2.0.0")
    dev.wait_reboot_request()
    dev.reboot()
    dev.cloud.wait(lambda: "adopt" in dev.actions()[-3:], 30, what="adopted after the reboot")
    st = dev.power_cut()                           # no mark-good yet: tries 2 of 3, still the primary
    assert st["booted"] == "A", st
    dev.start()
    dev.wait_state("Installed", "2.0.0")
    assert booted(dev) == ("A", "2.0.0") and dev.lab.show()["slots"]["A"]["tries"] == 0


def test_data_dir_outside_shared_partition_is_refused(make_device, tmp_path):
    dev = make_device(shared=tmp_path / "elsewhere")
    dev.release("1.0.0")
    dev.start()
    dev.deploy("1.0.0")
    r = dev.wait_state("Failed", "1.0.0", 30)
    assert r["reason"].startswith("artefact rejected: engine data dir") and "shared data partition" in r["reason"], r
    assert r["reasonCode"] == "FE-REC-002" and r.get("stage") == "preflight", r
    assert dev.lab.installs() == 0 and dev.lab.show()["rebootRequested"] == 0

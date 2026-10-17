"""EV-38 engine flow with the reference fake cloud, a real engine process and a simulated flexNode-arduino board:
install v1 -> update v2 -> a bad v3 (boots, reports ERROR) fails its gate at once and the engine re-serves v2;
a corrupt image is rejected by the board's decompress step, the board keeps its firmware and the deployment is
Failed fail-fast; a release for another board is refused before anything is sent. The desk runbook on a Portenta /
Opta repeats these steps once the upstream remote-OTA change is approved (S7)."""

from __future__ import annotations

import json

from .conftest import DEP, HEALTH


def test_install_update_bad_rollback_reserves_previous(board_device):
    dev = board_device
    for v in ("1.0.0", "2.0.0"):
        dev.release(v)
    dev.release("3.0.0", healthy=False)
    dev.start()
    dev.deploy("1.0.0")
    r = dev.wait_state("Installed", "1.0.0")
    assert r["health"] == "healthy" and dev.sim.firmware["version"] == "1.0.0"
    dev.deploy("2.0.0")
    dev.wait_state("Installed", "2.0.0")
    assert dev.sim.firmware["version"] == "2.0.0" and dev.sim.downloads == 2
    dev.deploy("3.0.0")
    r = dev.wait_state("Failed", "3.0.0")
    assert r["rolledBackTo"] == "2.0.0" and r["rollbackHealthy"] is True, r
    assert r["reason"] == "health check failed" and r["failure"] == "OTA_01 reports status ERROR on firmware 3.0.0", r
    assert r["detectSeconds"] < HEALTH["timeoutSeconds"], r                       # fail-fast, not the gate timeout
    # the rollback re-served the previous release's .ota: v1, v2, v3, v2 again
    assert dev.sim.firmware["version"] == "2.0.0" and dev.sim.downloads == 4
    versions = [c["params"]["version"] for c in dev.sim.commands]
    assert versions == ["1.0.0", "2.0.0", "3.0.0", "2.0.0"], versions
    acts = dev.actions()
    assert acts == ["apply", "apply", "apply", "rollback"], acts                 # no commit step on the board
    hb = json.loads(_get(dev, dev.sim.k["heartbeat"]))
    assert hb["version"] == "2.0.0" and hb["status"] == "RUNNING"


def test_corrupt_image_keeps_old_firmware_and_fails_fast(board_device):
    dev = board_device
    dev.release("1.0.0")
    dev.release("2.0.0", corrupt=True)
    dev.start()
    dev.deploy("1.0.0")
    dev.wait_state("Installed", "1.0.0")
    dev.deploy("2.0.0")
    r = dev.wait_state("Failed", "2.0.0")
    assert r["reason"] == "apply failed" and r["reasonCode"] == "FE-REC-003", r
    assert r["failure"].startswith("apply failed: OTA failed on OTA_01, it keeps firmware 1.0.0: decompress() failed"), r
    assert r["rolledBackTo"] == "1.0.0" and r["rollbackHealthy"] is True, r
    assert dev.sim.firmware["version"] == "1.0.0"
    assert dev.sim.downloads == 2                       # the rollback found 1.0.0 still running: nothing re-sent
    ota = json.loads(_get(dev, dev.sim.k["ota"]))
    assert ota["state"] == "failed" and ota["running"] == "1.0.0"


def test_release_for_another_board_is_refused_before_sending(board_device):
    from flexengine2.testing import arduino_sim as asim

    dev = board_device
    dev.release("1.0.0")
    img = asim.write_image(dev.payloads / "opta.ota", "5.0.0", fqbn=asim.OPTA)
    dev.releases["5.0.0"] = asim.make_arduino_release(dev.cloud.layout, dev.keys["release"], img, version="5.0.0",
                                                      fqbn=asim.OPTA, health=HEALTH)
    dev.start()
    dev.deploy("1.0.0")
    dev.wait_state("Installed", "1.0.0")
    dev.deploy("5.0.0")
    r = dev.wait_state("Failed", "5.0.0")
    assert r["reason"].startswith("artefact rejected: firmware is for arduino:mbed_opta:opta, board p1 is "
                                  "arduino:mbed_portenta:envie_m7"), r
    assert len(dev.sim.commands) == 1 and dev.sim.firmware["version"] == "1.0.0"


def _get(dev, key: str) -> str | None:
    from flexengine2.drivers.arduino_wire import RedisClient
    return RedisClient("127.0.0.1", dev.lab.redis_port, db=dev.lab.redis_db).get(key)

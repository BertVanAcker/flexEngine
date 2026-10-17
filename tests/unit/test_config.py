"""EV-04: engine config schema, validation, conversion, `flexengine config check`, generated reference."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from flexengine2.config import load as cl
from flexengine2.config import refdoc
from flexengine2.identity import crypto
from flexengine2.local import cli

FLEXENGINE = Path(__file__).resolve().parents[2]
REPO = FLEXENGINE.parent
K = crypto.b64(bytes(range(32)))
K2 = "B" * 43 + "="
HEX = "ab" * 64


def base() -> dict:
    return {
        "cloud": {"apiUrl": "http://127.0.0.1:8700", "cloudPublicKey": K, "releasePublicKeys": [K]},
        "dataDir": "/tmp/fe-data", "model": "examples/models/demo-pick", "deployment": "d_pick_lab",
        "devices": [
            {"deviceId": "panda", "role": "gateway", "onboardingToken": "t",
             "drivers": {"container": {"runtime": "docker"},
                         "os-rauc": {"sharedDataPath": "/tmp/fe-data"},
                         "arduino-ota": {"boards": {"p1": {
                             "fqbn": "arduino:mbed_portenta:envie_m7", "connectorId": "flexIA@demo-1fd420",
                             "applicationId": "OTA_01", "commandKeyFile": "/etc/flexengine/keys/OTA_01.key"}},
                             "mqtt": {"host": "192.168.1.101"}, "redis": {"host": "192.168.1.101", "db": 15},
                             "serve": {"publicUrl": "http://192.168.1.50:8470", "allowPlainHttp": True}},
                         "plc-verify": {"plcs": {
                             "panda/cx": {"via": "ads", "amsNetId": "127.0.0.1.1.1", "ip": "127.0.0.1"},
                             "opta": {"via": "modbus", "host": "127.0.0.1"},
                             "sim": {"via": "shm"}}}}},
            {"deviceId": "panda.portenta", "parent": "panda", "modelDevice": "portenta", "onboardingToken": "t",
             "mcu": {"smp": {"host": "127.0.0.1", "port": 5555}, "modulePublicKey": HEX},
             "relay": {"enabled": True, "connect": "tcp/127.0.0.1:7447"}},
            {"deviceId": "unoq", "onboardingToken": "t", "drivers": {"applab": {"board": "unoq"}}},
        ]}


def ankaios() -> dict:
    c = base()
    c["devices"][0]["drivers"]["container"] = {"runtime": "ankaios", "ankBin": "/usr/bin/ank",
                                               "serverUrl": "http://127.0.0.1:25551", "agent": "agent_A"}
    return c


def serial() -> dict:
    c = base()
    c["devices"][1]["mcu"]["smp"] = {"tty": "/dev/ttyUSB0"}
    return c


BASES = {"std": base, "ankaios": ankaios, "serial": serial}


def put(cfg: dict, ptr: str, value):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]
    cur = cfg
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    if isinstance(cur, list):
        cur[int(parts[-1])] = value
    else:
        cur[parts[-1]] = value
    return cfg


def paths_of(errors):
    return [e["path"] for e in errors]


D0, D1, D2 = "/devices/0", "/devices/1", "/devices/2"
CX, OPTA, SIM = f"{D0}/drivers/plc-verify/plcs/panda~1cx", f"{D0}/drivers/plc-verify/plcs/opta", f"{D0}/drivers/plc-verify/plcs/sim"
CONT, APP, MCU, RELAY = f"{D0}/drivers/container", f"{D2}/drivers/applab", f"{D1}/mcu", f"{D1}/relay"
NAT = f"{D0}/drivers/native"
RAU = f"{D0}/drivers/os-rauc"
AO = f"{D0}/drivers/arduino-ota"
AOB = f"{AO}/boards/p1"

# (documented key, base, pointer, valid value, invalid value) -- one row per key of the generated reference
CASES = [
    ("cloud", "std", "/cloud", {"stackInfo": "/s.json"}, "http://x"),
    ("cloud.apiUrl", "std", "/cloud/apiUrl", "https://lattepanda.lab:8700", "ftp://x"),
    ("cloud.cloudPublicKey", "std", "/cloud/cloudPublicKey", K2, "not-a-key"),
    ("cloud.stackInfo", "std", "/cloud/stackInfo", "/var/lib/flex/stack/stack.json", 5),
    ("cloud.releasePublicKeys", "std", "/cloud/releasePublicKeys", [K, K2], ["nope"]),
    ("cloud.releaseKeyFiles", "std", "/cloud/releaseKeyFiles", ["keys/release.key"], "keys/release.key"),
    ("cloud.pollInterval", "std", "/cloud/pollInterval", 0.5, 0),
    ("cloud.backoff", "std", "/cloud/backoff", [0.1, 2], [0.1]),
    ("cloud.httpTimeout", "std", "/cloud/httpTimeout", 3, "5"),
    ("cloud.healthEveryTicks", "std", "/cloud/healthEveryTicks", 1, 0),
    ("cloud.safeStateFile", "std", "/cloud/safeStateFile", "/var/lib/flex/safe.json", ""),
    # EV-05/EV-06/EV-09 keys
    ("cloud.caFile", "std", "/cloud/caFile", "/etc/flex/ca.crt", ""),
    ("cloud.tlsServerName", "std", "/cloud/tlsServerName", "cloud.lab", 1),
    ("cloud.allowSystemTrust", "std", "/cloud/allowSystemTrust", True, "yes"),
    ("cloud.allowPlainHttp", "std", "/cloud/allowPlainHttp", True, "yes"),
    ("cloud.capsEveryTicks", "std", "/cloud/capsEveryTicks", 10, 0),
    ("cloud.fixPermissions", "std", "/cloud/fixPermissions", False, 1),
    ("cloud.outboxMax", "std", "/cloud/outboxMax", 50, 0),
    ("logFormat", "std", "/logFormat", "text", "color"),
    ("otel", "std", "/otel", {}, {"bogus": 1}),
    ("otel.enabled", "std", "/otel", {"enabled": True}, {"enabled": "on"}),
    ("otel.endpoint", "std", "/otel", {"endpoint": "http://127.0.0.1:4318"}, {"endpoint": "127.0.0.1:4318"}),
    ("otel.file", "std", "/otel", {"file": "/var/log/flexengine/spans.jsonl"}, {"file": ""}),
    ("historyMax", "std", "/historyMax", 100, 0),
    ("shutdownGrace", "std", "/shutdownGrace", 5, -1),
    ("dataDir", "std", "/dataDir", "/var/lib/flexengine", 1),
    ("model", "std", "/model", "/var/lib/flex/runs/current/model", ""),
    ("deployment", "std", "/deployment", "d_other", ["d"]),
    ("devices", "std", "/devices", [{"deviceId": "solo", "drivers": {"native": True}}], []),
    ("local", "std", "/local", {}, "/run/flexengine/api.sock"),
    ("local.enabled", "std", "/local", {"enabled": False}, {"enabled": "no"}),
    ("local.socket", "std", "/local", {"socket": "/run/fe/api.sock"}, {"socket": ""}),
    ("local.group", "std", "/local", {"group": "flex"}, {"group": 0}),
    ("local.tcp", "std", "/local", {"tcp": {"bind": "127.0.0.1:8781", "tokenFile": "/etc/flexengine/api.token"}},
     {"tcp": {"bind": "127.0.0.1:8781"}}),
    ("local.tcp.bind", "std", "/local", {"tcp": {"bind": "[::1]:8781", "tokenFile": "/t"}},
     {"tcp": {"bind": "8781", "tokenFile": "/t"}}),
    ("local.tcp.tokenFile", "std", "/local", {"tcp": {"bind": "localhost:8781", "tokenFile": "t"}},
     {"tcp": {"bind": "localhost:8781", "tokenFile": ""}}),
    ("devices[].deviceId", "std", f"{D2}/deviceId", "unoq-2", "has space"),
    ("devices[].role", "std", f"{D0}/role", "standalone-device", "child-device"),
    ("devices[].parent", "std", f"{D1}/parent", "panda", "nobody"),
    ("devices[].modelDevice", "std", f"{D1}/modelDevice", "portenta2", ""),
    ("devices[].link", "std", f"{D1}/link", "smp/uart (QEMU tcp)", 3),
    ("devices[].onboardingToken", "std", f"{D0}/onboardingToken", "tok-1", ""),
    ("devices[].onboardingTokenFile", "std", f"{D0}/onboardingTokenFile", "/etc/flexengine/tokens/panda", 1),
    ("devices[].dataDir", "std", f"{D0}/dataDir", "/var/lib/fe/panda", ""),
    ("devices[].safeStateFile", "std", f"{D0}/safeStateFile", "/var/lib/flex/safe.json", 1),
    ("devices[].pollInterval", "std", f"{D0}/pollInterval", 2, -1),
    ("devices[].backoff", "std", f"{D0}/backoff", [1, 2], [1, 2, 3]),
    ("devices[].httpTimeout", "std", f"{D0}/httpTimeout", 1, 0),
    ("devices[].healthEveryTicks", "std", f"{D0}/healthEveryTicks", 5, 1.5),
    ("devices[].drivers", "std", f"{D2}/drivers", {}, "applab"),
    ("devices[].drivers.container", "std", CONT, {"runtime": "podman"}, {}),
    ("devices[].drivers.container.runtime", "std", f"{CONT}/runtime", "podman", "quadlet"),
    ("devices[].drivers.container.ankBin", "ankaios", f"{CONT}/ankBin", "/opt/ank", ""),
    ("devices[].drivers.container.serverUrl", "ankaios", f"{CONT}/serverUrl", "http://h:1", 1),
    ("devices[].drivers.container.agent", "ankaios", f"{CONT}/agent", "agent_B", ""),
    ("devices[].drivers.container.allowedRunOptions", "std", f"{CONT}/allowedRunOptions", {"devices": ["/dev/dri"]}, []),
    ("devices[].drivers.container.allowedRunOptions.devices", "std", f"{CONT}/allowedRunOptions", {"devices": ["/dev/dri"]}, {"devices": "/dev/dri"}),
    ("devices[].drivers.container.allowedRunOptions.gpus", "std", f"{CONT}/allowedRunOptions", {"gpus": ["all"]}, {"gpus": [1]}),
    ("devices[].drivers.container.allowedRunOptions.runtime", "std", f"{CONT}/allowedRunOptions", {"runtime": ["nvidia"]}, {"runtime": "nvidia"}),
    ("devices[].drivers.container.allowedRunOptions.groupAdd", "std", f"{CONT}/allowedRunOptions", {"groupAdd": ["video"]}, {"groupAdd": True}),
    ("devices[].drivers.native", "std", f"{D0}/drivers/native", True, "yes"),
    # EV-19: native driver on systemd
    ("devices[].drivers.native.runtime", "std", NAT, {"runtime": "systemd"}, {"runtime": "rkt"}),
    ("devices[].drivers.native.scope", "std", NAT, {"runtime": "systemd", "scope": "user"}, {"scope": "session"}),
    ("devices[].drivers.native.unitDir", "std", NAT, {"unitDir": "/etc/systemd/system"}, {"unitDir": ""}),
    ("devices[].drivers.native.user", "std", NAT, {"user": "flex"}, {"user": "Not A User"}),
    # EV-36/37: payload interpreter (venvs, platform())
    ("devices[].drivers.native.python", "std", NAT, {"runtime": "process", "python": "/usr/bin/python3.12"}, {"python": ""}),
    ("devices[].drivers.plc-verify", "std", f"{D0}/drivers/plc-verify", {}, []),
    ("devices[].drivers.plc-verify.plcs", "std", f"{D0}/drivers/plc-verify/plcs", {}, []),
    ("devices[].drivers.plc-verify.plcs.<name>.via", "std", f"{SIM}/via", "shm", "opcua"),
    ("devices[].drivers.plc-verify.plcs.<name>.amsNetId", "std", f"{CX}/amsNetId", "192.168.50.30.1.1", "192.168.50.30"),
    ("devices[].drivers.plc-verify.plcs.<name>.ip", "std", f"{CX}/ip", "192.168.50.30", ""),
    ("devices[].drivers.plc-verify.plcs.<name>.amsPort", "std", f"{CX}/amsPort", 852, 70000),
    ("devices[].drivers.plc-verify.plcs.<name>.localNetId", "std", f"{CX}/localNetId", "192.168.50.20.1.1", "lp"),
    ("devices[].drivers.plc-verify.plcs.<name>.timeout", "std", f"{OPTA}/timeout", 1.0, 0),
    ("devices[].drivers.plc-verify.plcs.<name>.instance", "std", f"{CX}/instance", "MAIN.fbOther", ""),
    ("devices[].drivers.plc-verify.plcs.<name>.host", "std", f"{OPTA}/host", "192.168.50.40", ""),
    ("devices[].drivers.plc-verify.plcs.<name>.port", "std", f"{OPTA}/port", 15020, "502"),
    ("devices[].drivers.plc-verify.plcs.<name>.unitId", "std", f"{OPTA}/unitId", 2, 300),
    ("devices[].drivers.plc-verify.plcs.<name>.path", "std", f"{SIM}/path", "/dev/shm/flex_bridge", ""),
    ("devices[].drivers.plc-verify.cacheSeconds", "std", f"{D0}/drivers/plc-verify/cacheSeconds", 0, -1),
    ("devices[].drivers.applab", "std", APP, {}, "unoq"),
    ("devices[].drivers.applab.board", "std", f"{APP}/board", "unoq", ""),
    ("devices[].drivers.applab.cli", "std", f"{APP}/cli", "/usr/bin/arduino-app-cli", 1),
    ("devices[].drivers.applab.routerSocket", "std", f"{APP}/routerSocket", "/run/r.sock", ""),
    ("devices[].drivers.applab.healthVia", "std", f"{APP}/healthVia", "logs", "http"),
    ("devices[].drivers.applab.takeoverForeignApps", "std", f"{APP}/takeoverForeignApps", True, "no"),
    ("devices[].drivers.applab.keepReleases", "std", f"{APP}/keepReleases", 3, 0),
    ("devices[].drivers.applab.cliTimeout", "std", f"{APP}/cliTimeout", 10, 0),
    ("devices[].drivers.applab.ssh", "std", f"{APP}/ssh", "arduino@unoq.local", ""),
    ("devices[].drivers.applab.sshCmd", "std", f"{APP}/sshCmd", ["ssh", "-p", "2222"], []),
    ("devices[].drivers.applab.scpCmd", "std", f"{APP}/scpCmd", ["scp"], "scp"),
    ("devices[].drivers.applab.remoteStaging", "std", f"{APP}/remoteStaging", "/tmp/x", ""),
    ("devices[].drivers.os-rauc", "std", RAU, {"sharedDataPath": "/data"}, {}),
    ("devices[].drivers.os-rauc.rauc", "std", f"{RAU}/rauc", "/usr/bin/rauc", ""),
    ("devices[].drivers.os-rauc.conf", "std", f"{RAU}/conf", "/etc/rauc/system.conf", 1),
    ("devices[].drivers.os-rauc.keyring", "std", f"{RAU}/keyring", "/etc/rauc/keyring.pem", ""),
    ("devices[].drivers.os-rauc.rebootCommand", "std", f"{RAU}/rebootCommand", ["systemctl", "reboot"], []),
    ("devices[].drivers.os-rauc.sharedDataPath", "std", f"{RAU}/sharedDataPath", "/data", ""),
    ("devices[].drivers.os-rauc.bootTimeoutSeconds", "std", f"{RAU}/bootTimeoutSeconds", 300, 0),
    ("devices[].drivers.os-rauc.installTimeoutSeconds", "std", f"{RAU}/installTimeoutSeconds", 600, -1),
    ("devices[].drivers.os-rauc.cliTimeout", "std", f"{RAU}/cliTimeout", 10, "10"),
    ("devices[].drivers.os-rauc.bootIdFile", "std", f"{RAU}/bootIdFile", "/run/boot_id", ""),
    ("devices[].drivers.os-rauc.healthChecks", "std", f"{RAU}/healthChecks", [["systemctl", "is-system-running"]],
     ["systemctl is-system-running"]),
    ("devices[].drivers.arduino-ota", "std", AO, {"boards": {"b": {"fqbn": "arduino:mbed_opta:opta", "connectorId": "c", "applicationId": "a",
                                             "commandKeyFile": "/k"}}, "serve": {}}, {"serve": {}}),
    ("devices[].drivers.arduino-ota.boards", "std", f"{AO}/boards", {"opta-1": {"fqbn": "arduino:mbed_opta:opta", "connectorId": "c",
                                                    "applicationId": "a", "commandKeyFile": "/k"}}, {}),
    ("devices[].drivers.arduino-ota.boards.<name>.fqbn", "std", f"{AOB}/fqbn", "arduino:mbed_opta:opta", "esp32:esp32:m5stack_atoms3"),
    ("devices[].drivers.arduino-ota.boards.<name>.connectorId", "std", f"{AOB}/connectorId", "flexIA@x", ""),
    ("devices[].drivers.arduino-ota.boards.<name>.applicationId", "std", f"{AOB}/applicationId", "OTA_02", ""),
    ("devices[].drivers.arduino-ota.boards.<name>.commandKeyFile", "std", f"{AOB}/commandKeyFile", "/k", 1),
    ("devices[].drivers.arduino-ota.boards.<name>.address", "std", f"{AOB}/address", "192.168.1.60", ""),
    ("devices[].drivers.arduino-ota.boards.<name>.mqtt", "std", f"{AOB}/mqtt", {"host": "10.0.0.2"}, {"hots": "x"}),
    ("devices[].drivers.arduino-ota.boards.<name>.mqtt.host", "std", f"{AOB}/mqtt", {"host": "10.0.0.2"}, {"host": ""}),
    ("devices[].drivers.arduino-ota.boards.<name>.mqtt.port", "std", f"{AOB}/mqtt", {"port": 8883}, {"port": 0}),
    ("devices[].drivers.arduino-ota.boards.<name>.mqtt.username", "std", f"{AOB}/mqtt", {"username": "u"}, {"username": 1}),
    ("devices[].drivers.arduino-ota.boards.<name>.mqtt.passwordFile", "std", f"{AOB}/mqtt", {"passwordFile": "/p"}, {"passwordFile": ""}),
    ("devices[].drivers.arduino-ota.boards.<name>.redis", "std", f"{AOB}/redis", {"db": 14}, {"database": 14}),
    ("devices[].drivers.arduino-ota.boards.<name>.redis.host", "std", f"{AOB}/redis", {"host": "10.0.0.3"}, {"host": 1}),
    ("devices[].drivers.arduino-ota.boards.<name>.redis.port", "std", f"{AOB}/redis", {"port": 6380}, {"port": 70000}),
    ("devices[].drivers.arduino-ota.boards.<name>.redis.db", "std", f"{AOB}/redis", {"db": 15}, {"db": "15"}),
    ("devices[].drivers.arduino-ota.boards.<name>.redis.passwordFile", "std", f"{AOB}/redis", {"passwordFile": "/p"}, {"passwordFile": 1}),
    ("devices[].drivers.arduino-ota.mqtt", "std", f"{AO}/mqtt", {"host": "192.168.1.101", "port": 1883}, []),
    ("devices[].drivers.arduino-ota.mqtt.host", "std", f"{AO}/mqtt/host", "192.168.1.101", ""),
    ("devices[].drivers.arduino-ota.mqtt.port", "std", f"{AO}/mqtt/port", 1883, "1883"),
    ("devices[].drivers.arduino-ota.mqtt.username", "std", f"{AO}/mqtt/username", "engine", ""),
    ("devices[].drivers.arduino-ota.mqtt.passwordFile", "std", f"{AO}/mqtt/passwordFile", "/etc/flexengine/mqtt.pw", ""),
    ("devices[].drivers.arduino-ota.redis", "std", f"{AO}/redis", {"host": "192.168.1.101", "db": 15}, "redis://x"),
    ("devices[].drivers.arduino-ota.redis.host", "std", f"{AO}/redis/host", "192.168.1.101", ""),
    ("devices[].drivers.arduino-ota.redis.port", "std", f"{AO}/redis/port", 6379, 0),
    ("devices[].drivers.arduino-ota.redis.db", "std", f"{AO}/redis/db", 15, 16),
    ("devices[].drivers.arduino-ota.redis.passwordFile", "std", f"{AO}/redis/passwordFile", "/etc/flexengine/redis.pw", ""),
    ("devices[].drivers.arduino-ota.serve", "std", f"{AO}/serve", {"host": "192.168.1.50"}, {"bind": "x"}),
    ("devices[].drivers.arduino-ota.serve.host", "std", f"{AO}/serve/host", "192.168.1.50", ""),
    ("devices[].drivers.arduino-ota.serve.port", "std", f"{AO}/serve/port", 0, -1),
    ("devices[].drivers.arduino-ota.serve.publicUrl", "std", f"{AO}/serve/publicUrl", "https://panda.lan:8470", "panda.lan:8470"),
    ("devices[].drivers.arduino-ota.serve.certFile", "std", f"{AO}/serve/certFile", "/etc/flexengine/ota.crt", ""),
    ("devices[].drivers.arduino-ota.serve.keyFile", "std", f"{AO}/serve/keyFile", "/etc/flexengine/ota.key", ""),
    ("devices[].drivers.arduino-ota.serve.allowPlainHttp", "std", f"{AO}/serve/allowPlainHttp", False, "yes"),
    ("devices[].drivers.arduino-ota.acceptTimeoutSeconds", "std", f"{AO}/acceptTimeoutSeconds", 30, 0),
    ("devices[].drivers.arduino-ota.updateTimeoutSeconds", "std", f"{AO}/updateTimeoutSeconds", 600, -5),
    ("devices[].drivers.arduino-ota.pollSeconds", "std", f"{AO}/pollSeconds", 1, 0),
    ("devices[].drivers.arduino-ota.timeout", "std", f"{AO}/timeout", 3, "3"),
    ("devices[].drivers.arduino-ota.healthyStatus", "std", f"{AO}/healthyStatus", ["RUNNING", "CLOUDLINK_OK"], []),
    ("devices[].drivers.arduino-ota.failedStatus", "std", f"{AO}/failedStatus", ["ERROR", "STOPPED"], "ERROR"),
    ("devices[].drivers.arduino-ota.requireHeartbeat", "std", f"{AO}/requireHeartbeat", False, "no"),
    ("devices[].mcu", "std", MCU, {"smp": {"host": "h", "port": 1}, "modulePublicKey": HEX}, {}),
    ("devices[].mcu.smp", "std", f"{MCU}/smp", {"tty": "/dev/ttyACM0", "baud": 115200}, {}),
    ("devices[].mcu.smp.tty", "serial", f"{MCU}/smp/tty", "/dev/ttyACM0", "ttyACM0"),
    ("devices[].mcu.smp.baud", "serial", f"{MCU}/smp/baud", 9600, 0),
    ("devices[].mcu.smp.host", "std", f"{MCU}/smp/host", "10.0.0.2", ""),
    ("devices[].mcu.smp.port", "std", f"{MCU}/smp/port", 1, 0),
    ("devices[].mcu.modulePublicKey", "std", f"{MCU}/modulePublicKey", "CD" * 64, "abc"),
    ("devices[].mcu.timeout", "std", f"{MCU}/timeout", 5, 0),
    ("devices[].relay", "std", RELAY, {}, "on"),
    ("devices[].relay.enabled", "std", f"{RELAY}/enabled", False, "true"),
    ("devices[].relay.maxRateHz", "std", f"{RELAY}/maxRateHz", 5, 11),
    ("devices[].relay.connect", "std", f"{RELAY}/connect", None, "127.0.0.1:7447"),
    ("devices[].relay.listen", "std", f"{RELAY}/listen", "tcp/127.0.0.1:0", "x"),
    ("devices[].relay.mode", "std", f"{RELAY}/mode", "client", "router"),
    ("devices[].relay.zenoh", "std", f"{RELAY}/zenoh", False, 1),
    ("devices[].relay.model", "std", f"{RELAY}/model", "/m", ""),
    ("devices[].relay.deployment", "std", f"{RELAY}/deployment", "d", ""),
    ("devices[].relay.modelDevice", "std", f"{RELAY}/modelDevice", "portenta", ""),
]
IDS = [c[0] for c in CASES]


def test_bases_are_valid():
    for name, mk in BASES.items():
        assert cl.validate(mk()) == [], name


def test_every_documented_key_has_a_case():
    documented = [r[0] for r in refdoc.rows()]
    assert sorted(documented) == sorted(IDS)
    assert len(documented) == len(set(documented))


@pytest.mark.parametrize("key,b,ptr,good,bad", CASES, ids=IDS)
def test_key_valid(key, b, ptr, good, bad):
    assert cl.validate(put(BASES[b](), ptr, good)) == []


@pytest.mark.parametrize("key,b,ptr,good,bad", CASES, ids=IDS)
def test_key_invalid(key, b, ptr, good, bad):
    errs = cl.validate(put(BASES[b](), ptr, copy.deepcopy(bad)))
    assert errs, f"{key}={bad!r} accepted"
    assert any(p == ptr or p.startswith(ptr + "/") for p in paths_of(errs)), errs
    assert all(e["message"] and "\n" not in e["message"] for e in errs)


REQUIRED = [("/cloud", ""), ("/dataDir", ""), ("/devices", ""), (f"{D0}/deviceId", D0),
            (f"{CONT}/runtime", CONT), (f"{CX}/via", CX), (f"{CX}/amsNetId", CX), (f"{CX}/ip", CX),
            (f"{OPTA}/host", OPTA), (f"{MCU}/smp", MCU), (f"{MCU}/modulePublicKey", MCU),
            (f"{MCU}/smp/host", f"{MCU}/smp"), (f"{MCU}/smp/port", f"{MCU}/smp"), (f"{RAU}/sharedDataPath", RAU),
            (f"{AO}/boards", AO), (f"{AO}/serve", AO), (f"{AOB}/fqbn", AOB), (f"{AOB}/connectorId", AOB),
            (f"{AOB}/applicationId", AOB), (f"{AOB}/commandKeyFile", AOB)]


@pytest.mark.parametrize("ptr,parent", REQUIRED, ids=[r[0] for r in REQUIRED])
def test_required_key_missing(ptr, parent):
    cfg = base()
    parts = ptr.split("/")
    cur = cfg
    for p in [q.replace("~1", "/") for q in parts[1:-1]]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    del cur[parts[-1]]
    errs = cl.validate(cfg)
    assert {"path": parent, "message": f"missing required key {parts[-1]!r}"} in errs, errs


def test_ankaios_requires_its_keys_and_refuses_run_options():
    c = base()
    c["devices"][0]["drivers"]["container"] = {"runtime": "ankaios"}
    errs = cl.validate(c)
    assert {f"missing required key {k!r}" for k in ("ankBin", "serverUrl", "agent")} <= {e["message"] for e in errs}
    c = ankaios()
    c["devices"][0]["drivers"]["container"]["allowedRunOptions"] = {}
    assert paths_of(cl.validate(c)) == [f"{CONT}/allowedRunOptions"]


# ---- unknown keys at every level -------------------------------------------------------------------------------------
LEVELS = [("std", ""), ("std", "/cloud"), ("std", D0), ("std", f"{D0}/drivers"), ("std", CONT),
          ("ankaios", CONT), ("std", f"{CONT}/allowedRunOptions"), ("std", f"{D0}/drivers/plc-verify"),
          ("std", CX), ("std", OPTA), ("std", SIM), ("std", APP), ("std", MCU), ("std", f"{MCU}/smp"),
          ("serial", f"{MCU}/smp"), ("std", RELAY), ("std", NAT), ("std", RAU), ("std", AO),
          ("std", AOB), ("std", f"{AO}/serve")]


@pytest.mark.parametrize("b,ptr", LEVELS, ids=[f"{b}:{p or '/'}" for b, p in LEVELS])
def test_unknown_key_rejected(b, ptr):
    cfg = BASES[b]()
    if ptr == f"{CONT}/allowedRunOptions":
        cfg["devices"][0]["drivers"]["container"]["allowedRunOptions"] = {}
    if ptr == NAT:
        cfg["devices"][0]["drivers"]["native"] = {}
    put(cfg, f"{ptr}/bogusKey", 1)
    errs = cl.validate(cfg)
    assert [e["path"] for e in errs] == [f"{ptr}/bogusKey"], errs
    assert errs[0]["message"].startswith("unknown key 'bogusKey'")


def test_unknown_key_message_lists_allowed_keys():
    errs = cl.validate(put(base(), "/cloud/apiURL", "http://x"))
    assert errs[0]["message"].startswith("unknown key 'apiURL' (allowed: allowPlainHttp, allowSystemTrust, apiUrl, ")
    errs = cl.validate(put(base(), NAT, {"runtme": "systemd"}))
    assert errs == [{"path": f"{NAT}/runtme", "message": "unknown key 'runtme' (allowed: python, runtime, scope, unitDir, user)"}]


def test_native_systemd_user_needs_system_scope():
    assert msgs(put(base(), NAT, {"runtime": "systemd", "scope": "user", "user": "flex"})) == [
        f"{NAT}/user: User= is only available with scope: system (the user manager runs units as its own user)"]
    assert msgs(put(base(), NAT, {"runtime": "systemd", "scope": "system", "user": "flex"})) == []


# ---- cross-key rules -------------------------------------------------------------------------------------------------
def msgs(cfg):
    return [f"{e['path']}: {e['message']}" for e in cl.validate(cfg)]


def test_cloud_needs_api_url_and_key_or_stack_info():
    c = base()
    c["cloud"] = {"releasePublicKeys": []}
    assert msgs(c) == ["/cloud: set apiUrl (or stackInfo)", "/cloud: set cloudPublicKey (or stackInfo)"]
    c["cloud"] = {"stackInfo": "/s.json"}
    assert msgs(c) == []


def test_duplicate_device_ids():
    c = base()
    c["devices"][2]["deviceId"] = "panda"
    assert msgs(c) == ["/devices/2/deviceId: duplicate deviceId 'panda' (also devices/0)"]


def test_mcu_child_rules():
    c = base()
    c["devices"][1]["drivers"] = {}
    c["devices"][1]["role"] = "gateway"
    assert set(msgs(c)) == {"/devices/1/drivers: an MCU child (mcu) has no drivers: it gets mcu-module and mcu-firmware",
                            "/devices/1/role: an MCU child's role is always child-device; remove role"}
    c = base()
    del c["devices"][1]["parent"]
    assert any("'mcu' requires 'parent'" in m for m in msgs(c))
    c = base()
    c["devices"][2]["parent"] = "panda"
    assert any("'parent' requires 'mcu'" in m for m in msgs(c))
    c = base()
    c["devices"][0]["relay"] = {"enabled": False}
    assert any("'relay' requires 'mcu'" in m for m in msgs(c))
    c = base()
    c["devices"].append({"deviceId": "grandchild", "parent": "panda.portenta",
                         "mcu": {"smp": {"host": "h", "port": 2}, "modulePublicKey": HEX}})
    assert msgs(c) == ["/devices/3/parent: parent 'panda.portenta' is an MCU child, not a gateway"]


def test_smp_forms_are_mutually_exclusive():
    c = base()
    c["devices"][1]["mcu"]["smp"] = {"host": "127.0.0.1", "port": 5555, "tty": "/dev/ttyACM0"}
    assert msgs(c) == ["/devices/1/mcu/smp: use either {host, port} (TCP) or {tty, baud} (serial), not both"]
    c["devices"][1]["mcu"]["smp"] = {"tty": "/dev/ttyACM0", "baud": 115200}
    assert msgs(c) == []
    c["devices"][1]["mcu"]["smp"] = {"baud": 115200}
    assert msgs(c) == ["/devices/1/mcu/smp: missing required key 'tty'"]


def test_enabled_relay_needs_model_deployment_model_device():
    c = base()
    del c["model"], c["deployment"], c["devices"][1]["modelDevice"]
    assert len(msgs(c)) == 3
    c["devices"][1]["relay"].update(model="/m", deployment="d", modelDevice="portenta")
    assert msgs(c) == []
    c["devices"][1]["relay"] = {"enabled": False}
    del c["devices"][1]["relay"]
    assert msgs(c) == []


def test_not_a_mapping():
    assert cl.validate([]) == [{"path": "", "message": "the config must be a mapping, got a list"}]


# ---- ${VAR} expansion and file resolution ----------------------------------------------------------------------------
def write(tmp_path, text, name="engine.yaml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


MIN = """\
cloud: {apiUrl: "http://127.0.0.1:8700", cloudPublicKey: "%s"}
dataDir: ${FE_T}/data
devices:
  - {deviceId: panda, drivers: {applab: {cli: "${FE_CLI}"}}}
""" % K


def test_env_expansion_before_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("FE_T", str(tmp_path))
    monkeypatch.setenv("FE_CLI", "/opt/cli")
    cfg = cl.load(write(tmp_path, MIN))
    assert cfg["dataDir"] == f"{tmp_path}/data" and cfg["devices"][0]["drivers"]["applab"]["cli"] == "/opt/cli"
    monkeypatch.setenv("FE_PORT", "5555")
    p = write(tmp_path, "cloud: {stackInfo: s}\ndataDir: d\ndevices:\n  - {deviceId: gw, drivers: {}}\n"
                        "  - {deviceId: c, parent: gw, mcu: {smp: {host: h, port: ${FE_PORT}}, modulePublicKey: %s}}\n" % HEX)
    assert cl.parse(p)["devices"][1]["mcu"]["smp"]["port"] == 5555     # expanded, then typed by YAML


def test_unset_env_var_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("FE_T", raising=False)
    monkeypatch.setenv("FE_CLI", "/opt/cli")
    r = cl.check(write(tmp_path, MIN))
    assert not r["valid"] and r["errors"] == [{"path": "/dataDir", "message": "environment variable FE_T is not set"}]
    with pytest.raises(cl.ConfigError, match="FE_T is not set"):
        cl.load(write(tmp_path, MIN))
    # inside a YAML flow mapping the unexpanded reference breaks the YAML itself: still named
    p = write(tmp_path, "devices: [{deviceId: x, mcu: {smp: {host: h, port: ${FE_UNSET_PORT}}}}]\n")
    r = cl.check(p)
    assert r["errors"][0] == {"path": "", "message": "environment variable FE_UNSET_PORT is not set (line 1)"}


def test_file_resolution(tmp_path, monkeypatch):
    key = tmp_path / "release.key"
    crypto.load_or_create(key)
    (tmp_path / "stack.json").write_text(json.dumps({"internal": {"p2": "http://127.0.0.1:8700"},
                                                     "urls": {"p2": "https://lattepanda.lab:9700"},
                                                     "cloudPublicKey": K2}))
    (tmp_path / "tok").write_text("tok-123\n")
    monkeypatch.setenv("T", str(tmp_path))
    p = write(tmp_path, "cloud: {stackInfo: ${T}/stack.json, releaseKeyFiles: [${T}/release.key]}\n"
                        "dataDir: ${T}/data\ndevices:\n  - {deviceId: panda, onboardingTokenFile: ${T}/tok}\n")
    cfg = cl.load(p)
    assert cfg["cloud"]["apiUrl"] == "http://127.0.0.1:8700" and cfg["cloud"]["cloudPublicKey"] == K2
    assert cfg["cloud"]["releasePublicKeys"] == [crypto.public_b64(crypto.load_private(key))]
    assert "onboardingToken" not in cfg["devices"][0]  # EV-06: the engine consumes the file at onboarding
    assert cl.validate(cfg) == []
    from flexengine2.host import load_config

    assert load_config(p) == cfg
    assert cl.check(p, resolve_files=True)["valid"]
    (tmp_path / "tok").unlink()
    r = cl.check(p, resolve_files=True)
    assert [e["path"] for e in r["errors"]] == ["/devices/0/onboardingTokenFile"]
    assert cl.check(p)["valid"]                   # without --resolve the files are not read


def test_explicit_api_url_wins_over_stack_info(tmp_path):
    (tmp_path / "stack.json").write_text(json.dumps({"urls": {"p2": "http://a:1"}, "cloudPublicKey": K2}))
    c = {"cloud": {"stackInfo": str(tmp_path / "stack.json"), "apiUrl": "http://shim:8690"}, "dataDir": "d",
         "devices": [{"deviceId": "x"}]}
    p = write(tmp_path, json.dumps(c), "engine.json")
    cfg = cl.load(p)
    assert cfg["cloud"]["apiUrl"] == "http://shim:8690" and cfg["cloud"]["cloudPublicKey"] == K2


def test_host_load_config_raises_on_invalid(tmp_path):
    from flexengine2.host import load_config

    p = write(tmp_path, "cloud: {apiUrl: 'http://x', cloudPublicKey: '%s', bogus: 1}\ndataDir: d\ndevices: [{deviceId: a}]\n" % K)
    with pytest.raises(cl.ConfigError) as ei:
        load_config(p)
    assert ei.value.errors == [{"path": "/cloud/bogus", "message": ei.value.errors[0]["message"]}]
    assert "/cloud/bogus: unknown key 'bogus'" in str(ei.value)


def test_unreadable_and_malformed_files(tmp_path):
    assert cl.check(tmp_path / "missing.yaml")["errors"][0]["message"].startswith("cannot read")
    assert "not valid JSON" in cl.check(write(tmp_path, "{", "e.json"))["errors"][0]["message"]
    assert cl.check(write(tmp_path, "- a\n"))["errors"] == [
        {"path": "", "message": f"{tmp_path / 'engine.yaml'}: the config must be a mapping, got a list"}]


# ---- converter: plan §3.6 form -> canonical --------------------------------------------------------------------------
def plan_example() -> dict:
    text = (REPO / "docs" / "flexengine-v2-plan.md").read_text()
    block = re.search(r"### 3\.6.*?```yaml\n(.*?)```", text, re.S).group(1)
    fills = {"<b64 Ed25519>": K, "<b64>": K2, "<cx>": "192.168.50.30.1.1", "<cx-ip>": "192.168.50.30",
             "<lp>": "192.168.50.20.1.1", "<opta-ip>": "192.168.50.40", "<hex>": HEX}
    for k, v in fills.items():
        block = block.replace(k, v)
    return yaml.safe_load(block)


def test_plan_example_converts():
    doc = plan_example()
    assert cl.is_plan_form(doc)
    cfg, notes = cl.from_plan_v2(doc)
    assert cfg["cloud"]["releasePublicKeys"] == [K2]
    assert cfg["cloud"]["safeStateFile"] == "/var/lib/flexengine/safe-state.json"
    assert cfg["devices"][0]["drivers"]["native"] == {"runtime": "systemd"}  # EV-19
    assert "os-rauc" not in cfg["devices"][0]["drivers"]
    assert cfg["devices"][1]["mcu"]["smp"] == {"tty": "/dev/ttyACM0", "baud": 115200}
    assert {n.split(" ")[0] for n in notes} == {"/local", "/safeState/staleAfter", "/devices/0/drivers/os-rauc"}
    # EV-06 / EV-09: caFile is canonical now; observability maps to logFormat / historyMax (empty otlpEndpoint = off)
    assert cfg["cloud"]["caFile"] == "/etc/flex/ca.crt"
    assert cfg["logFormat"] == "json" and cfg["historyMax"] == 1000 and "otel" not in cfg
    # the plan example names no deployment for the relay anywhere (inconsistency reported in EV-04)
    assert msgs(cfg) == ["/devices/1/relay: an enabled relay needs a deployment (relay.deployment or top-level "
                         "deployment)"]
    cfg["devices"][1]["relay"]["deployment"] = "d_pick_lab"
    assert cl.validate(cfg) == []


def test_plan_form_is_converted_by_load(tmp_path):
    doc = plan_example()
    doc["devices"][1]["relay"]["deployment"] = "d_pick_lab"
    p = write(tmp_path, yaml.safe_dump(doc))
    r = cl.check(p)
    assert r["valid"] and len(r["warnings"]) == 3
    assert "apiVersion" not in cl.parse(p)


@pytest.mark.parametrize("mutate,where", [
    (lambda d: d.update(apiVersion="flexengine/v3"), "/apiVersion"),
    (lambda d: d["cloud"]["releaseTrust"].update(cosign=["k"]), "/cloud/releaseTrust/cosign"),
    (lambda d: d.update(identity={"keyStore": "tpm2"}), "/identity"),
    (lambda d: d["safeState"].update(provider="plc"), "/safeState/provider"),
])
def test_plan_conversion_refuses_what_alpha_cannot_honour(mutate, where):
    doc = plan_example()
    mutate(doc)
    with pytest.raises(cl.ConfigError) as ei:
        cl.from_plan_v2(doc)
    assert [e["path"] for e in ei.value.errors] == [where]


def test_plan_native_process_mode_maps_to_true():
    doc = plan_example()
    doc["devices"][0]["drivers"]["native"] = {"mode": "process"}
    cfg, _ = cl.from_plan_v2(doc)
    assert cfg["devices"][0]["drivers"]["native"] is True


def test_canonical_form_is_not_converted():
    assert not cl.is_plan_form(base())


# ---- the existing configs -------------------------------------------------------------------------------------------
EDGE_ENV = {"FLEX_DEMO_DATA": "/var/lib/flex/stack", "FLEX_EDGE_TOKENS": "/var/lib/flex/edge/tokens",
            "FLEX_MCU_SMP_PORT": "5555", "FLEX_MODULE_PUBKEY_HEX": HEX}


def test_edge_example_validates(monkeypatch):
    for k, v in EDGE_ENV.items():
        monkeypatch.setenv(k, v)
    p = REPO / "demo" / "edge" / "edge.example.yaml"
    r = cl.check(p)
    assert r == {"valid": True, "errors": [], "warnings": []}
    assert cl.parse(p)["devices"][1]["mcu"]["smp"]["port"] == 5555


def test_edge_example_without_env_names_the_variables(monkeypatch):
    for k in EDGE_ENV:
        monkeypatch.delenv(k, raising=False)
    msgs_ = [e["message"] for e in cl.check(REPO / "demo" / "edge" / "edge.example.yaml")["errors"]]
    assert any("FLEX_MCU_SMP_PORT is not set" in m for m in msgs_)


def _labinv():
    sys.path.insert(0, str(REPO / "demo" / "lab"))
    try:
        import labinv
    finally:
        sys.path.pop(0)
    return labinv


LAB_ENV = {"FLEX_MODULE_PUBKEY_HEX": HEX, "FLEX_CLOUD_PUBKEY": K, "FLEX_RELEASE_PUBKEY": K2}


@pytest.mark.parametrize("unoq_engine", ["remote", "board"])
def test_labinv_rendered_configs_validate(tmp_path, monkeypatch, unoq_engine):
    for k, v in LAB_ENV.items():
        monkeypatch.setenv(k, v)
    labinv = _labinv()
    inv = labinv.load(REPO / "demo" / "lab" / "inventory.example.yaml")
    inv["hosts"]["unoq"]["engine"] = unoq_engine
    files = labinv.render(inv, tmp_path)
    engine_cfgs = [n for n, t in files.items() if n.endswith(".yaml") and "devices:" in t and "cloud:" in t]
    assert sorted(engine_cfgs) == ["edge-unoq.yaml", "edge.yaml"]
    for name in engine_cfgs:
        r = cl.check(tmp_path / name)
        assert r["valid"], (name, r["errors"])
    devs = [d["deviceId"] for d in cl.parse(tmp_path / "edge.yaml")["devices"]]
    assert devs == (["panda", "panda.portenta", "unoq"] if unoq_engine == "remote" else ["panda", "panda.portenta"])


def test_labinv_cli_render_validates(tmp_path, monkeypatch):
    out = tmp_path / "etc"
    subprocess.run([sys.executable, str(REPO / "demo" / "lab" / "labinv.py"), "render", "--inventory",
                    str(REPO / "demo" / "lab" / "inventory.example.yaml"), "--out", str(out)], check=True,
                   capture_output=True)
    for k, v in LAB_ENV.items():
        monkeypatch.setenv(k, v)
    for name in ("edge.yaml", "edge-unoq.yaml"):
        assert cl.check(out / name)["valid"], name


def test_labinv_flex_env_leaves_the_module_key_empty(tmp_path, monkeypatch):
    """flex.env renders FLEX_MODULE_PUBKEY_HEX= (lattepanda-install.sh fills it in): until then edge.yaml is invalid."""
    monkeypatch.setenv("FLEX_MODULE_PUBKEY_HEX", "")
    labinv = _labinv()
    labinv.render(labinv.load(REPO / "demo" / "lab" / "inventory.example.yaml"), tmp_path)
    assert [e["path"] for e in cl.check(tmp_path / "edge.yaml")["errors"]] == ["/devices/1/mcu/modulePublicKey"]


SIL_SCRIPT = r"""
import json, sys, types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import harness
work = Path(sys.argv[2])
fake_zenoh = work / "zenoh_fake" / "__init__.py"
fake_zenoh.parent.mkdir(parents=True, exist_ok=True); fake_zenoh.write_text("")
z = types.ModuleType("zenoh"); z.__file__ = str(fake_zenoh); sys.modules["zenoh"] = z
ns = types.SimpleNamespace
self = ns(dir=work, shim={"url": "http://127.0.0.1:8690"}, stack={"cloudPublicKey": sys.argv[3]}, mb_port=15020,
          mcu=ns(port=5555), module_pub=sys.argv[4],
          board=ns(sock=work / "router.sock", tty=work / "tty", sim=work / "sim"),
          admin=lambda m, p, b=None: {"token": "tok-" + b["deviceId"]}, spawn=lambda *a, **k: None)
harness.Run.start_edge(self)
print((work / "edge" / "edge.yaml").read_text())
"""


def test_sil_harness_edge_config_validates(tmp_path):
    """demo/sil/harness.py Run.start_edge() with its I/O stubbed (admin tokens, spawn, zenoh import), in a
    subprocess because importing the harness puts the demo/probe packages on sys.path."""
    p = subprocess.run([sys.executable, "-c", SIL_SCRIPT, str(REPO / "demo" / "sil"), str(tmp_path), K, HEX],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0 and "ModuleNotFoundError" in p.stderr:
        pytest.skip(f"demo/sil harness not importable here: {p.stderr.strip().splitlines()[-1]}")
    assert p.returncode == 0, p.stderr
    cfg_path = tmp_path / "edge" / "edge.yaml"
    r = cl.check(cfg_path)
    assert r["valid"], r["errors"]
    cfg = yaml.safe_load(cfg_path.read_text())
    assert [d["deviceId"] for d in cfg["devices"]] == ["panda", "panda.portenta", "unoq"]
    assert cfg["devices"][1]["relay"]["listen"] == "tcp/127.0.0.1:0"


# ---- `flexengine config check` -------------------------------------------------------------------------------------
def test_cli_config_check(tmp_path, capsys):
    good = write(tmp_path, json.dumps(base()), "good.json")
    assert cli.main(["config", "check", str(good)]) == 0
    assert capsys.readouterr().out.strip() == f"{good}: valid"
    bad = copy.deepcopy(base())
    bad["devices"][0]["drivers"]["container"]["runtime"] = "quadlet"
    bad["cloud"]["x"] = 1
    badp = write(tmp_path, json.dumps(bad), "bad.json")
    assert cli.main(["config", "check", str(badp)]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["/cloud/x: unknown key 'x' (allowed: allowPlainHttp, allowSystemTrust, apiUrl, backoff, caFile, capsEveryTicks, cloudPublicKey, "
                   "fixPermissions, healthEveryTicks, httpTimeout, outboxMax, pollInterval, releaseKeyFiles, "
                   "releasePublicKeys, safeStateFile, stackInfo, tlsServerName)",
                   "/devices/0/drivers/container/runtime: must be one of docker, podman, ankaios; got 'quadlet'"]
    assert cli.main(["config", "check", str(badp), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["valid"] is False and set(doc) == {"valid", "errors"}
    assert [e["path"] for e in doc["errors"]] == ["/cloud/x", "/devices/0/drivers/container/runtime"]
    assert cli.main(["config", "check", str(good), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "errors": []}


def test_cli_config_check_prints_conversion_warnings(tmp_path, capsys):
    doc = plan_example()
    doc["devices"][1]["relay"]["deployment"] = "d"
    assert cli.main(["config", "check", str(write(tmp_path, yaml.safe_dump(doc)))]) == 0
    assert capsys.readouterr().err.count("warning: ") == 3


# ---- schema + generated reference ------------------------------------------------------------------------------------
def test_schema_is_valid_2020_12():
    from jsonschema import Draft202012Validator

    s = cl.schema()
    Draft202012Validator.check_schema(s)
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema" and "canonical" in s["$comment"].lower()


def test_committed_reference_is_up_to_date():
    committed = FLEXENGINE / "docs" / "config-reference.md"
    assert committed.read_text() == refdoc.render(), "regenerate: python -m flexengine2.config.refdoc"
    assert refdoc.main(["--check", "--out", str(committed)]) == 0


def test_reference_refuses_an_undocumented_key():
    s = copy.deepcopy(cl.schema())
    del s["properties"]["dataDir"]["description"]
    with pytest.raises(refdoc.UndocumentedKey, match="dataDir"):
        refdoc.rows(s)


def test_vars_in_yaml_comments_are_not_expanded(tmp_path, monkeypatch):
    from flexengine2.config import load as L

    monkeypatch.delenv("FLEX_UNSET_IN_COMMENT", raising=False)
    monkeypatch.setenv("FLEX_SET_VAR", "x")
    text = "# uses ${FLEX_UNSET_IN_COMMENT}\na: ${FLEX_SET_VAR}  # also ${FLEX_UNSET_IN_COMMENT}\nb: 'not # a comment ${FLEX_SET_VAR}'\n"
    out = L.expand(text, yaml_comments=True)
    assert out == "# uses ${FLEX_UNSET_IN_COMMENT}\na: x  # also ${FLEX_UNSET_IN_COMMENT}\nb: 'not # a comment x'\n"

"""flexEngineV2 host process (from demo/edge, IG-05): builds every device engine of one host from one config and runs
them.

Config (JSON or YAML; documented in demo/INTERFACES.md):

    {
      "cloud": {"apiUrl": "http://127.0.0.1:8700", "cloudPublicKey": "<b64>", "releasePublicKeys": ["<b64>"],
                "pollInterval": 1.0, "backoff": [0.25, 4.0], "httpTimeout": 5},
      "dataDir": "demo/edge/work/data",
      "model": "examples/models/demo-pick", "deployment": "d_pick_lab",
      "devices": [
        {"deviceId": "panda", "role": "gateway", "onboardingToken": "...",
         "drivers": {"container": {"runtime": "podman"},
                     "plc-verify": {"plcs": {"panda/cx": {"via": "ads", "amsNetId": "...", "ip": "...", "amsPort": 851,
                                                           "localNetId": "..."},
                                             "opta": {"via": "modbus", "host": "...", "port": 502}}}}},
        {"deviceId": "panda.portenta", "parent": "panda", "modelDevice": "portenta", "onboardingToken": "...",
         "mcu": {"smp": {"host": "127.0.0.1", "port": 5555}, "modulePublicKey": "<hex>"},
         "relay": {"enabled": true, "maxRateHz": 10, "connect": "tcp/127.0.0.1:7447"}},
        {"deviceId": "unoq", "onboardingToken": "...",
         "drivers": {"applab": {"board": "unoq", "cli": "arduino-app-cli", "ssh": "arduino@unoq.local"}}}
      ]
    }

Every device gets the P2 engine (merged SPI). A device with `children`/`parent` gets the P3 gateway/child roles in
its capabilities. `safeStateFile` and the other P2 engine keys may be set per device or under `cloud`.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import yaml

from flexengine2.core.reconciler import Engine, build_drivers
from flexengine2.drivers.base import DriverError

def load_config(path: str | Path) -> dict:
    """JSON or YAML; ${VAR} references are expanded from the environment; relative paths are taken as given (run
    from the repo root). Resolves the coordination files of the other demo components:

      cloud.stackInfo        demo/cloud's stack.json -> apiUrl (its internal P2 URL) and cloudPublicKey, when unset
      cloud.releaseKeyFiles  Ed25519 release key files (flex release `release_key`) -> releasePublicKeys
      devices[].onboardingTokenFile   one-time onboarding token written by whoever issued it
    """
    text = os.path.expandvars(Path(path).read_text())
    cfg = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
    cl = cfg.setdefault("cloud", {})
    if cl.get("stackInfo"):
        info = json.loads(Path(cl["stackInfo"]).read_text())
        cl.setdefault("apiUrl", (info.get("internal") or {}).get("p2") or info["urls"]["p2"])
        cl.setdefault("cloudPublicKey", info["cloudPublicKey"])
    keys = list(cl.get("releasePublicKeys") or [])
    for f in cl.get("releaseKeyFiles") or []:
        from flexengine2.identity import crypto

        keys.append(crypto.public_b64(crypto.load_private(f)))
    cl["releasePublicKeys"] = keys
    for dev in cfg.get("devices", []):
        if dev.get("onboardingTokenFile") and not dev.get("onboardingToken"):
            dev["onboardingToken"] = Path(dev["onboardingTokenFile"]).read_text().strip()
    return cfg


CLOUD_KEYS = ("apiUrl", "cloudPublicKey", "releasePublicKeys", "pollInterval", "backoff", "httpTimeout",
              "healthEveryTicks", "safeStateFile")


def device_cfg(cfg: dict, dev: dict) -> dict:
    data = Path(dev.get("dataDir") or Path(cfg["dataDir"]) / dev["deviceId"])
    data.mkdir(parents=True, exist_ok=True)
    out = {**{k: v for k, v in cfg["cloud"].items() if k in CLOUD_KEYS},
           **{k: v for k, v in dev.items() if k not in ("drivers", "mcu", "relay")}, "dataDir": str(data)}
    out["drivers"] = {}  # P2's build_drivers: only what the device asks for
    return out


def build(cfg: dict, log=print) -> tuple[list[Engine], list]:
    """(engines, relays) of one host."""

    engines, relays = [], []
    children = {}
    for dev in cfg["devices"]:
        if dev.get("parent"):
            children.setdefault(dev["parent"], []).append(dev)
    for dev in cfg["devices"]:
        dcfg = device_cfg(cfg, dev)
        data = Path(dcfg["dataDir"])
        drv_cfg = dev.get("drivers") or {}
        if "mcu" in dev:
            from flexengine2.drivers.mcu.driver import LinkPool, McuFirmwareDriver, McuModuleDriver

            m = dev["mcu"]
            links = LinkPool(m["smp"]["host"], int(m["smp"]["port"]), timeout=float(m.get("timeout", 10)))
            mods = McuModuleDriver(links, data, bytes.fromhex(m["modulePublicKey"]), dev["deviceId"])
            drivers = {"mcu-module": mods, "mcu-firmware": McuFirmwareDriver(links, data, dev["deviceId"], mods)}
            try:
                info = links.call(lambda l: l.flexmod("info"))
            except DriverError:
                info = {}
            eng = Engine(dcfg, drivers, role="child-device", parent=dev["parent"], mcu=info)
            rc = dev.get("relay") or {}
            if rc.get("enabled", False):
                from flexengine2.gateway.relay import McuRelay

                relays.append(McuRelay(links, Path(rc.get("model") or cfg["model"]),
                                       rc.get("deployment") or cfg.get("deployment"),
                                       rc.get("modelDevice") or dev.get("modelDevice"), data / "relay",
                                       max_rate_hz=float(rc.get("maxRateHz", 10)), connect=rc.get("connect"),
                                       listen=rc.get("listen"), mode=rc.get("mode", "peer"),
                                       zenoh_enabled=rc.get("zenoh", True), log=eng.log))
        else:
            p2cfg = {"drivers": {k: v for k, v in drv_cfg.items() if k in ("container",)}}
            p2cfg["drivers"]["native"] = bool(drv_cfg.get("native", False))
            drivers = build_drivers({**dcfg, **p2cfg}, data)
            if "plc-verify" in drv_cfg:
                from flexengine2.drivers.plc.verify import PlcVerifyDriver

                drivers["plc-verify"] = PlcVerifyDriver(dev["deviceId"], data, drv_cfg["plc-verify"])
            if "applab" in drv_cfg:
                from flexengine2.drivers.applab.driver import ApplabDriver

                drivers["applab"] = ApplabDriver(dev["deviceId"], data, drv_cfg["applab"])
            kids = [{"deviceId": c["deviceId"], "mode": "see-thru", "link": c.get("link", "smp/uart"), "kind": "mcu"}
                    for c in children.get(dev["deviceId"], [])]
            eng = Engine(dcfg, drivers, role=dev.get("role") or ("gateway" if kids else "standalone-device"),
                             children=kids)
        engines.append(eng)
    return engines, relays


def _otel_hook(engines):
    """IG-13 (demo/obs, merged from demo/obs/patches/edge-engine-otel.patch): with FLEX_OTEL=1 every device engine
    emits engine.install / driver.apply / engine.health-gate spans in the plan's trace and puts traceparent +
    correlationId in its status reports (FLEX_OTEL_FILE = JSONL, OTEL_EXPORTER_OTLP_ENDPOINT = collector).
    Off (default) or without the OTel packages = no behaviour change."""
    if os.environ.get("FLEX_OTEL") != "1":
        return None
    import sys

    try:
        from flexengine2.obs import otel
        from flexengine2.obs.instrument import instrument_engine
    except ImportError as ex:
        print(f"flexengine: FLEX_OTEL=1 but OpenTelemetry is not installed ({ex}); running without spans",
              file=sys.stderr)
        return None
    otel.setup(True)
    for e in engines:
        instrument_engine(e)
    return otel


def run(cfg: dict, ticks: int = 0, stop=None) -> None:
    engines, relays = build(cfg)
    otel = _otel_hook(engines)
    stop = stop or {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))
    nxt = [0.0] * len(engines)
    for e in engines:
        e.log(f"edge engine start ({e.role}): drivers={sorted(e.drivers)} manifestVersion={e.state['manifestVersion']} "
              f"applied={sorted(e.state['applied'])}")
    n = 0
    try:
        while not stop["v"]:
            now = time.time()
            ran = False
            for i, e in enumerate(engines):
                if now >= nxt[i]:
                    ran = True
                    try:
                        nxt[i] = time.time() + e.tick()
                    except Exception as ex:  # one device never takes the others down
                        e.log(f"engine error: {ex!r}")
                        nxt[i] = time.time() + 1.0
            for r in relays:
                if r.due():
                    try:
                        r.tick()
                    except Exception as ex:  # noqa: BLE001
                        r.log(f"relay error: {ex!r}")
            if ran:
                n += 1
                if ticks and n >= ticks:
                    break
            time.sleep(0.01)
    finally:
        for r in relays:
            r.close()
        for e in engines:
            e.log("edge engine stop")
        if otel is not None:
            otel.flush()

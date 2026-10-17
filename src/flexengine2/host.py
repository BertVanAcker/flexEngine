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

Threading (EV-05, plan §3.4): one worker thread per device engine (tick -> sleep, never blocked by another
device), one thread per MCU relay, and the main thread for signals, sd_notify and the watchdog. The SMP link of an
MCU child is shared by its engine and its relay; `LockedLinkPool` serialises it (one lock held for a whole driver
session, so an upload is never interleaved with a relay poll). SIGTERM/SIGINT: stop polling, let a running
apply/health gate finish within `shutdownGrace` (default 30 s), save every engine's state, close the relays, exit.
A device whose state.json is corrupt (core/state.py) is refused at start (logged, `Host.refused`); the others run.

Hook for the local API (EV-07):

    def on_ready(host: Host):          # called once from the main thread after READY=1; must not block
        start_api_server(host)         # e.g. in its own daemon thread
    run(cfg, on_ready=on_ready)

`Host` gives: `engines` (list of core.reconciler.Engine), `relays`, `refused` ({deviceId: error text}),
`data_dirs` ({deviceId: Path}, refused devices included), `engine(device_id)`, `devices()` (one summary dict per
device: deviceId, role, dataDir, running, refused, metrics), `stop_event` (threading.Event, set on shutdown) and
`stop()` (request a clean shutdown). Engine state must only be changed on the engine's own worker thread: use
`engine.submit(fn, *args)` (returns a concurrent.futures.Future) e.g. for a bundle import; reading `engine.state`,
`engine.metrics()` and `flexengine2.obs.history.read(data_dir, since)` is safe from any thread.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

from flexengine2.core.reconciler import Engine, build_drivers
from flexengine2.core.state import StateError
from flexengine2.drivers.base import DriverError
from flexengine2.local import sdnotify
from flexengine2.obs import logging as flog
from flexengine2.obs import tracing

SHUTDOWN_GRACE = 30.0
READY_WAIT = 30.0   # READY=1 after every engine's first round, or after this long at most


def load_config(path: str | Path) -> dict:
    """JSON or YAML, validated against flexengine2/config/schema.json (raises config.load.ConfigError with every
    error); ${VAR} references are expanded from the environment; relative paths are taken as given (run from the repo
    root); cloud.stackInfo and cloud.releaseKeyFiles are resolved. devices[].onboardingTokenFile is NOT read here:
    the engine consumes it at onboarding (EV-06: read, onboard, then overwrite + delete). See flexengine2.config.load."""
    from flexengine2.config.load import ConfigError, load, validate

    cfg = load(path)
    errors = validate(cfg)
    if errors:
        raise ConfigError(errors)
    return cfg


CLOUD_KEYS = ("apiUrl", "cloudPublicKey", "releasePublicKeys", "pollInterval", "backoff", "httpTimeout",
              "healthEveryTicks", "safeStateFile", "caFile", "tlsServerName", "allowSystemTrust", "allowPlainHttp", "capsEveryTicks", "fixPermissions",
              "outboxMax")
HOST_KEYS = ("historyMax",)  # top-level keys every device engine gets


def device_cfg(cfg: dict, dev: dict) -> dict:
    data = Path(dev.get("dataDir") or Path(cfg["dataDir"]) / dev["deviceId"])
    data.mkdir(parents=True, exist_ok=True)
    out = {**{k: v for k, v in cfg["cloud"].items() if k in CLOUD_KEYS},
           **{k: cfg[k] for k in HOST_KEYS if k in cfg},
           **{k: v for k, v in dev.items() if k not in ("drivers", "mcu", "relay")}, "dataDir": str(data)}
    out["drivers"] = {}  # P2's build_drivers: only what the device asks for
    return out


def device_data_dir(cfg: dict, dev: dict) -> Path:
    return Path(dev.get("dataDir") or Path(cfg["dataDir"]) / dev["deviceId"])


def _locked_link_pool():
    from flexengine2.drivers.mcu.driver import LinkPool

    class LockedLinkPool(LinkPool):
        """LinkPool shared by an MCU child's engine thread and its relay thread (plan §3.4): every call, and a whole
        `session()` (upload + activate), holds one re-entrant lock."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.lock = threading.RLock()

        def call(self, fn, retries: int = 1):
            with self.lock:
                return super().call(fn, retries)

        def _drop(self):
            with self.lock:
                return super()._drop()

        def session(self):
            inner, lock = super().session(), self.lock

            class _S:
                def __enter__(self):
                    lock.acquire()
                    try:
                        return inner.__enter__()
                    except BaseException:
                        lock.release()
                        raise

                def __exit__(self, *exc):
                    try:
                        return inner.__exit__(*exc)
                    finally:
                        lock.release()
            return _S()

    return LockedLinkPool


def build(cfg: dict, log=print, refused: dict | None = None) -> tuple[list[Engine], list]:
    """(engines, relays) of one host. With `refused` (a dict), a device whose state cannot be used (StateError:
    corrupt / too new state.json) is left out and recorded there as {deviceId: message}; without it, it raises."""

    engines, relays = [], []
    children = {}
    for dev in cfg["devices"]:
        if dev.get("parent"):
            children.setdefault(dev["parent"], []).append(dev)
    for dev in cfg["devices"]:
        try:
            eng, rel = _build_device(cfg, dev, children)
        except StateError as e:
            if refused is None:
                raise
            refused[dev["deviceId"]] = str(e)
            flog.emit("error", str(e), device=dev["deviceId"], event="state.refused")
            continue
        engines.append(eng)
        relays += rel
    return engines, relays


def _build_device(cfg: dict, dev: dict, children: dict) -> tuple[Engine, list]:
    relays = []
    dcfg = device_cfg(cfg, dev)
    data = Path(dcfg["dataDir"])
    drv_cfg = dev.get("drivers") or {}
    if "mcu" in dev:
        from flexengine2.drivers.mcu.driver import McuFirmwareDriver, McuModuleDriver

        m = dev["mcu"]
        links = _locked_link_pool()(m["smp"]["host"], int(m["smp"]["port"]), timeout=float(m.get("timeout", 10)))
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
        p2cfg = {"drivers": {k: v for k, v in drv_cfg.items() if k in ("container", "os-rauc")}}
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
    return eng, relays


def _otel_hook(engines):
    """Compatibility shim (IG-13): spans are built into the engine since EV-09 (obs/tracing.py; cfg `otel` or
    FLEX_OTEL=1). With FLEX_OTEL=1 this enables them for the given engines and returns the otel module; an object that
    is not a flexengine2 Engine (a foreign engine of the P2 shape) still gets the legacy wrapper (obs/instrument.py).
    Off (default) or without the OTel packages: None, nothing changes."""
    if os.environ.get("FLEX_OTEL") != "1" or not tracing.configure(None):
        return None
    from flexengine2.obs import otel

    for e in engines:
        if isinstance(e, Engine):
            e.tracer = tracing.EngineTracer(e.device)
        else:
            from flexengine2.obs.instrument import instrument_engine

            instrument_engine(e)
    return otel


class Host:
    """Every device engine and relay of one host, with their threads (see the module docstring for the API hook)."""

    def __init__(self, cfg: dict, engines: list[Engine], relays: list, refused: dict[str, str] | None = None,
                 stop_event: threading.Event | None = None):
        self.cfg, self.engines, self.relays = cfg, engines, relays
        self.refused: dict[str, str] = dict(refused or {})
        self.stop_event = stop_event or threading.Event()
        self.threads: dict[str, threading.Thread] = {}
        self.relay_threads: list[threading.Thread] = []
        self.first_round = {e.device: threading.Event() for e in engines}
        self.ready = threading.Event()
        self.started_at: float | None = None

    @classmethod
    def from_config(cls, cfg: dict, stop_event: threading.Event | None = None) -> "Host":
        refused: dict[str, str] = {}
        engines, relays = build(cfg, refused=refused)
        return cls(cfg, engines, relays, refused, stop_event)

    # ---- queries (any thread) ------------------------------------------------------------------
    @property
    def data_dirs(self) -> dict[str, Path]:
        out = {e.device: Path(e.dir) for e in self.engines}
        for dev in self.cfg.get("devices", []):
            out.setdefault(dev["deviceId"], device_data_dir(self.cfg, dev))
        return out

    def engine(self, device_id: str) -> Engine | None:
        return next((e for e in self.engines if e.device == device_id), None)

    def running(self, device_id: str) -> bool:
        t = self.threads.get(device_id)
        return bool(t and t.is_alive())

    def devices(self) -> list[dict]:
        out = []
        dirs = self.data_dirs
        for dev in self.cfg.get("devices", []):
            d = dev["deviceId"]
            e = self.engine(d)
            out.append({"deviceId": d, "role": e.role if e else None, "dataDir": str(dirs[d]),
                        "running": self.running(d), "refused": self.refused.get(d),
                        "metrics": e.metrics() if e else None})
        return out

    def last_error(self) -> tuple[str, dict] | None:
        best = None
        for e in self.engines:
            le = e.metrics()["lastError"]
            if le and (best is None or le["t"] > best[1]["t"]):
                best = (e.device, le)
        return best

    def status_text(self) -> str:
        n = len(self.cfg.get("devices", []))
        run = sum(1 for e in self.engines if self.running(e.device))
        txt = f"{run}/{n} devices running"
        if self.refused:
            txt += f", refused: {', '.join(sorted(self.refused))}"
        le = self.last_error()
        if le:
            dev, err = le
            txt += f"; last error: {dev} {err['phase']}: {err['type']}: {err['message'][:80]}"
        return txt

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self, ticks: int = 0) -> None:
        self.started_at = time.time()
        for e in self.engines:
            e.log(f"edge engine start ({e.role}): drivers={sorted(e.drivers)} manifestVersion={e.state['manifestVersion']} "
                  f"applied={sorted(e.state['applied'])}", event="engine.start", role=e.role)
            t = threading.Thread(target=self._worker, args=(e, ticks), name=f"engine-{e.device}", daemon=True)
            e.worker = t
            self.threads[e.device] = t
            t.start()
        for i, r in enumerate(self.relays):
            t = threading.Thread(target=self._relay_worker, args=(r,), name=f"relay-{i}", daemon=True)
            self.relay_threads.append(t)
            t.start()

    def _worker(self, e: Engine, ticks: int) -> None:
        n = 0
        try:
            while not self.stop_event.is_set() and not e.stopping:
                e.run_pending()
                delay = e.tick()  # never raises (typed per-phase errors)
                n += 1
                self.first_round[e.device].set()
                if ticks and n >= ticks:
                    break
                e.sleep(delay)
        except Exception as ex:  # noqa: BLE001 - last line of defence: this device stops, the others go on
            e.log(f"engine worker crashed: {type(ex).__name__}: {ex}", "error", "engine.crashed",
                  errorType=type(ex).__name__)
        finally:
            self.first_round[e.device].set()
            e.shutdown()

    def _relay_worker(self, r) -> None:
        while not self.stop_event.is_set():
            if r.due():
                try:
                    r.tick()
                except Exception as ex:  # noqa: BLE001
                    try:
                        r.log(f"relay error: {ex!r}")
                    except Exception:  # noqa: BLE001
                        pass
            self.stop_event.wait(0.01)

    def workers_alive(self) -> bool:
        return any(t.is_alive() for t in self.threads.values())

    def stop(self) -> None:
        """Request a clean shutdown (any thread, signal handlers)."""
        self.stop_event.set()
        for e in self.engines:
            e.request_stop()

    def shutdown(self, grace: float = SHUTDOWN_GRACE) -> list[str]:
        """Stop, join the workers within `grace` seconds, close the relays. Returns the devices that did not stop."""
        self.stop()
        deadline = time.monotonic() + grace
        stuck = []
        for dev, t in self.threads.items():
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                stuck.append(dev)
                e = self.engine(dev)
                flog.emit("warning", f"engine did not stop within {grace:g} s (busy in {e._dep if e else '?'}); "
                                     "its state is as of the last transition", device=dev, event="engine.stop-timeout")
        for t in self.relay_threads:
            t.join(max(0.5, deadline - time.monotonic()))
        for r in self.relays:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass
        return stuck


def run(cfg: dict, ticks: int = 0, stop=None, on_ready=None, grace: float | None = None) -> Host:
    """Run every device engine of this host until SIGTERM/SIGINT (or `stop`), or for `ticks` rounds per device.

    stop      threading.Event (becomes Host.stop_event) or the legacy {"v": bool} dict polled by the main loop
    on_ready  callable(host) run once on the main thread after READY=1 (local API hook; must not block)
    grace     seconds a running apply/gate may take to finish at shutdown (default cfg shutdownGrace, else 30)

    systemd (Type=notify): READY=1 after the engines are built and every engine finished its first round (at most
    READY_WAIT s), WATCHDOG=1 at half of WatchdogSec (or every second when it is off), STATUS=<n devices running,
    last error> on change, STOPPING=1 at shutdown. Without $NOTIFY_SOCKET every notification is a no-op."""
    flog.configure(cfg.get("logFormat"))
    flog.bridge_stdlib()
    tracing.configure(cfg.get("otel"))
    grace = float(cfg.get("shutdownGrace", SHUTDOWN_GRACE) if grace is None else grace)
    host = Host.from_config(cfg, stop if isinstance(stop, threading.Event) else None)
    legacy = stop if isinstance(stop, dict) else None

    def stop_requested() -> bool:
        return host.stop_event.is_set() or bool(legacy and legacy.get("v"))

    old = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, lambda *a: host.stop())
    wd = sdnotify.watchdog_interval()
    period = min(wd, 1.0) if wd else 1.0
    if ticks:
        period = min(period, 0.05)
    try:
        host.start(ticks)
        end = time.monotonic() + READY_WAIT
        while not stop_requested() and time.monotonic() < end and \
                not all(ev.is_set() for ev in host.first_round.values()):
            if wd:
                sdnotify.watchdog()
            host.stop_event.wait(min(period, 0.05))
        status = host.status_text()
        sdnotify.ready(status)
        host.ready.set()
        if on_ready is not None and not stop_requested():
            try:
                on_ready(host)
            except Exception as ex:  # noqa: BLE001 - the engines keep running without the hook
                flog.emit("error", f"on_ready hook failed: {type(ex).__name__}: {ex}", event="host.on-ready-failed")
        while not stop_requested():
            if ticks and not host.workers_alive():
                break
            if wd:
                sdnotify.watchdog()
            st = host.status_text()
            if st != status:
                status = st
                sdnotify.status(st)
            host.stop_event.wait(period)
    finally:
        sdnotify.stopping(f"stopping: {host.status_text()}")
        host.shutdown(grace)
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, TypeError):
                pass
        tracing.flush()
    return host

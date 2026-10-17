"""Local operator API (CP-7, EV-07): HTTP/1.1 over a Unix socket, optional read-only TCP bind with a bearer token.

Contract: flexengine2/api/local-api.v1.openapi.yaml (+ cli-status.v1.schema.json for GET /v1/status).

    run(cfg, on_ready=lambda host: serve_host(host, config_file=path))   # host.run hook; stops with the host

    provider = EngineProvider(engines, cfg, config_file="engine.yaml")   # or by hand, from a list of engines
    api = LocalApi.from_config(cfg, provider).start()                     # socket (+ TCP when local.tcp is set)
    ...
    api.stop()

    LocalApi(OfflineProvider(cfg)).handle("GET", "/v1/status")            # no server, engine not running

The API reads; it never changes the desired state. The two write operations (socket only) import a signed offline
bundle (the same path the engine takes for a cloud answer, so the signature and manifestVersion rules apply) and
nudge the engines to reconcile now.

A *provider* tells the API what runs. It must offer ``devices()`` -> ``[{deviceId, dataDir, engine?, role?, parent?,
drivers?, online?, etagAt?}]``; ``engine`` is the live ``core.reconciler.Engine`` (absent = offline mode: everything
is read from the data directory: state.json, actions.jsonl, history.jsonl). Optional members, each used when present:

    history(data_dir, since) -> [record | {record, delivered?}]   status ring buffer (obs/history.read)
    metrics() -> dict                                             GET /v1/metrics
    health() -> {ok, ticks, lastTickAgeSeconds?}                  GET /v1/health
    reconcile() -> [deviceId]                                     POST /v1/reconcile
    import_bundle(device_id, data: bytes) -> {deviceId, manifestVersion, accepted}   POST /v1/bundle
    log_files(device_id, data_dir) -> [Path]                      extra log files for GET /v1/logs
    started_at: float, config_file: str
"""

from __future__ import annotations

import contextlib
import copy
import grp
import hmac
import http.client
import http.server
import io
import json
import os
import re
import select
import socket
import socketserver
import stat
import sys
import tarfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from flexengine2 import __version__

DEFAULT_SOCKET = "/run/flexengine/api.sock"
# sizeof(sockaddr_un.sun_path) minus the terminating NUL: 108 on Linux, 104 on macOS and the BSDs
SUN_PATH_MAX = 107 if sys.platform.startswith("linux") else 103
API_VERSION = "flexengine.local/v1"
ID_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
MAX_BODY = 1 << 30            # 1 GiB offline bundle
LOG_TAIL = 200                # default lines of GET /v1/logs without follow
FOLLOW_POLL = 0.2
BUNDLE_TIMEOUT = 600.0       # the engine thread may be inside a health gate
STALE_MIN = 120.0             # a tick older than this (or 2x the engine's longest sleep) = stuck


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(f"{status} {title}: {detail}")
        self.status, self.title, self.detail = status, title, detail

    def problem(self) -> dict:
        return {"type": "about:blank", "title": self.title, "status": self.status, "detail": self.detail}


class Response:
    def __init__(self, status: int, body=b"", ctype: str = "application/json", headers: dict | None = None):
        self.status, self.ctype, self.headers = status, ctype, headers or {}
        self.body = body          # bytes, or an iterator of bytes (chunked stream)

    @property
    def streaming(self) -> bool:
        return not isinstance(self.body, (bytes, bytearray))

    def json(self):
        return json.loads(self.body)


def _json(status: int, doc, ctype: str = "application/json") -> Response:
    return Response(status, json.dumps(doc, sort_keys=True).encode(), ctype)


# ---- config helpers ---------------------------------------------------------------------------------------------
def _profiles() -> set[str]:
    from flexengine2 import contracts

    return set(contracts.load("desired-state")["$defs"]["DriverProfile"]["enum"])


def config_profiles(dev: dict) -> list[str]:
    """Driver profiles a device of the config gets (host.build's rules)."""
    if "mcu" in dev:
        return ["mcu-module", "mcu-firmware"]
    drv = dev.get("drivers") or {}
    out = [p for p in ("container", "plc-verify", "applab") if p in drv]
    if drv.get("native") or isinstance(drv.get("native"), dict):  # true, or {runtime: ...} (EV-19; {} = systemd)
        out.append("native")
    return out


def config_devices(cfg: dict) -> list[dict]:
    """Provider entries (no engine) for every device of a config; same data dirs as host.device_cfg."""
    parents = {d.get("parent") for d in cfg.get("devices", []) if d.get("parent")}
    out = []
    for dev in cfg.get("devices", []):
        if "mcu" in dev:
            role = "child-device"
        else:
            role = dev.get("role") or ("gateway" if dev["deviceId"] in parents else "standalone-device")
        out.append({"deviceId": dev["deviceId"], "dataDir": str(dev.get("dataDir") or Path(cfg["dataDir"]) / dev["deviceId"]),
                    "role": role, "parent": dev.get("parent"), "drivers": config_profiles(dev)})
    return out


def default_socket(cfg: dict | None) -> str:
    """No `local.socket` configured: /run/flexengine/api.sock when that directory exists (systemd RuntimeDirectory of
    the packaged service), else <dataDir>/api.sock, so dev and test engines on one host never share a socket."""
    if Path(DEFAULT_SOCKET).parent.is_dir() or not (cfg or {}).get("dataDir"):
        return DEFAULT_SOCKET
    return str(Path(cfg["dataDir"]) / "api.sock")


def socket_path_fits(path: str | os.PathLike) -> bool:
    """True when `path` fits an AF_UNIX address (sun_path) on this platform."""
    return len(os.fsencode(path)) <= SUN_PATH_MAX


def local_settings(cfg: dict | None) -> dict:
    loc = (cfg or {}).get("local") or {}
    tcp = loc.get("tcp") or {}
    return {"socket_path": loc.get("socket") or default_socket(cfg), "group": loc.get("group"),
            "tcp_bind": tcp.get("bind"), "token_file": tcp.get("tokenFile")}


def bundle_device(data: bytes) -> str | None:
    """deviceId of the desired state inside an offline bundle (None if it has none)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            m = tf.getmember("desired.json")
            return json.loads(tf.extractfile(m).read()).get("deviceId")
    except (KeyError, tarfile.TarError, ValueError, AttributeError):
        return None


def accept_bundle(engine, data: bytes) -> dict:
    """Import an offline bundle into one engine (blobs into its artefact store, then the signed desired state through
    accept_manifest: signature, deviceId and manifestVersion rules as for a cloud answer)."""
    submit = getattr(engine, "submit", None)
    if callable(submit):  # run on the engine's own thread, between two ticks
        return submit(_accept_bundle, engine, data).result(timeout=BUNDLE_TIMEOUT)
    return _accept_bundle(engine, data)


def _accept_bundle(engine, data: bytes) -> dict:
    from flexengine2.artefacts.store import VerificationError
    from flexengine2.identity import crypto

    try:
        desired, sig = engine.store.import_bundle(data, accept=lambda d, s: crypto.verify(engine.cloud_pub, d, s))
    except (VerificationError, tarfile.TarError, ValueError) as e:
        raise ApiError(422, "bundle refused", f"FE-BUN-001: {e}") from None
    if not crypto.verify(engine.cloud_pub, desired, sig):
        raise ApiError(422, "bundle refused", "FE-BUN-001: desired state signature does not verify")
    try:
        doc = json.loads(desired)
        mv = int(doc["manifestVersion"])
    except (ValueError, KeyError, TypeError):
        raise ApiError(422, "bundle refused", "FE-DS-001: desired state is not a valid document") from None
    if doc.get("deviceId") != engine.device:
        raise ApiError(422, "bundle refused", f"bundle is for device {doc.get('deviceId')!r}, not {engine.device!r}")
    changed = engine.accept_manifest(desired, sig, None)
    st = engine.state
    same = st.get("manifestVersion") == mv and st.get("desired") == doc
    if not (changed or same):
        raise ApiError(422, "bundle refused",
                       f"manifestVersion {mv} is older than the accepted {st.get('manifestVersion')} (downgrade)")
    if changed:
        engine.log("desired state imported from offline bundle (local API)")
    return {"deviceId": engine.device, "manifestVersion": mv, "accepted": True, "changed": bool(changed)}


# ---- providers ---------------------------------------------------------------------------------------------------
class EngineProvider:
    """A running host: the device engines from host.build()."""

    def __init__(self, engines, cfg: dict | None = None, *, config_file: str | None = None,
                 started_at: float | None = None, history=None, metrics=None, reconcile=None, import_bundle=None,
                 health=None, log_files=None):
        self.engines = list(engines)
        self.cfg = cfg
        self.config_file = config_file
        self.started_at = started_at or time.time()
        for name, fn in (("history", history), ("metrics", metrics), ("reconcile", reconcile),
                         ("import_bundle", import_bundle), ("health", health), ("log_files", log_files)):
            if fn is not None:
                setattr(self, name, fn)

    def devices(self) -> list[dict]:
        return [{"deviceId": e.device, "dataDir": str(e.dir), "engine": e, "role": e.role, "parent": e.parent,
                 "drivers": list(e.drivers)} for e in self.engines]

    def import_bundle(self, device_id: str, data: bytes) -> dict:
        for e in self.engines:
            if e.device == device_id:
                return accept_bundle(e, data)
        raise ApiError(404, "unknown device", device_id)

    def reconcile(self) -> list[str]:
        """Wake every engine from its poll sleep (nudge()/wake(), else a no-op submit(), which sets the wake event);
        the woken tick pulls and reconciles. An engine busy in a tick reconciles right after it."""
        out = []
        for e in self.engines:
            fn = getattr(e, "nudge", None) or getattr(e, "wake", None)
            if callable(fn):
                fn()
                out.append(e.device)
            elif callable(getattr(e, "submit", None)):
                e.submit(lambda: None)
                out.append(e.device)
        if not out:
            raise ApiError(503, "not supported", "this host's engines cannot be nudged; they reconcile every tick")
        return out


class HostProvider(EngineProvider):
    """host.Host (host.run's on_ready hook): its running engines plus the devices it refused (corrupt state.json),
    which are read from their data directories."""

    def __init__(self, host, *, config_file: str | None = None, **kw):
        super().__init__(host.engines, host.cfg, config_file=config_file,
                         started_at=getattr(host, "started_at", None), **kw)
        self.host = host

    def devices(self) -> list[dict]:
        live = {d["deviceId"]: d for d in super().devices()}
        out = []
        for d in config_devices(self.host.cfg):
            out.append(live.pop(d["deviceId"], None) or d)
        return out + list(live.values())


def serve_host(host, config_file: str | None = None, **kw) -> "LocalApi":
    """on_ready hook for host.run: serve the local API of a running host until its stop_event is set."""
    api = LocalApi.from_config(host.cfg, HostProvider(host, config_file=config_file), **kw).start()
    stop_event = getattr(host, "stop_event", None)
    if stop_event is not None:
        def watch():
            stop_event.wait()
            api.stop()
        threading.Thread(target=watch, name="local-api-stop", daemon=True).start()
    return api


class OfflineProvider:
    """No engine running: read the data directories of a config."""

    def __init__(self, cfg: dict, config_file: str | None = None):
        self.cfg, self.config_file = cfg, config_file
        self.started_at = None

    def devices(self) -> list[dict]:
        return config_devices(self.cfg)

    def import_bundle(self, device_id: str, data: bytes) -> dict:
        """Accepted into state.json; the engine installs it at its next start."""
        from flexengine2.core.reconciler import Engine
        from flexengine2.host import device_cfg

        dev = next((d for d in self.cfg.get("devices", []) if d["deviceId"] == device_id), None)
        if dev is None:
            raise ApiError(404, "unknown device", device_id)
        if not self.cfg.get("cloud", {}).get("cloudPublicKey"):
            raise ApiError(422, "bundle refused", "no cloud.cloudPublicKey to verify the desired state")
        dcfg = device_cfg(self.cfg, dev)
        dcfg.setdefault("releasePublicKeys", [])
        role = next(d["role"] for d in config_devices(self.cfg) if d["deviceId"] == device_id)
        eng = Engine(dcfg, {}, role=role, parent=dev.get("parent"))
        eng.log = _stderr_log(device_id)   # the CLI's stdout is its answer (--json), the engine's log goes to stderr
        return accept_bundle(eng, data)


def _default_log(msg: str):
    try:
        from flexengine2.obs import logging as flog

        flog.emit("info", f"local API: {msg}", event="local-api")
    except ImportError:  # pragma: no cover
        print(f"flexengine local API: {msg}", flush=True)


def _stderr_log(device: str):
    import sys

    def log(msg, level="info", event=None, **kv):
        print(f"{device}: {msg}", file=sys.stderr, flush=True)
    return log


# ---- readers -----------------------------------------------------------------------------------------------------
def _snapshot(engine) -> dict:
    """Consistent copy of a live engine's state (the engine thread may mutate it while we copy)."""
    lock = getattr(engine, "lock", None)
    for _ in range(20):
        try:
            with lock if lock is not None else contextlib.nullcontext():
                return copy.deepcopy(engine.state)
        except RuntimeError:  # dictionary changed size during iteration
            time.sleep(0.001)
    return _read_state(Path(engine.dir))


def _read_state(data_dir: Path) -> dict:
    try:
        st = json.loads((Path(data_dir) / "state.json").read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def _history_reader(provider):
    fn = getattr(provider, "history", None)
    if callable(fn):
        return fn
    try:  # EV-09 ring buffer (obs/history.py), when present
        from flexengine2.obs import history as h  # type: ignore

        if callable(getattr(h, "read", None)):
            return h.read
    except ImportError:
        pass

    def read(data_dir, since):
        return _jsonl(Path(data_dir) / "history.jsonl.1") + _jsonl(Path(data_dir) / "history.jsonl")
    return read


REPORTED = ("Pending", "Installing", "Installed", "Failed", "Removing", "Removed")
_HIST_FIELDS = ("version", "digest", "reason", "health", "correlationId", "traceparent", "rolledBackTo", "failFast",
                "restarts")


def _entries(raw) -> list[dict]:
    """CP-2 records from history items: obs/history.py transitions ({t, device, dep, state, kind, seq, ...}; only
    kind `reported` ones are status reports), {record: ...} wrappers, or plain records."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("record"), dict):
            out.append(item["record"])
        elif "deviceTime" in item and "seq" in item:
            out.append(item)
        elif item.get("kind", "reported") == "reported" and item.get("state") in REPORTED and item.get("dep") \
                and isinstance(item.get("seq"), int):
            rec = {"dep": item["dep"], "seq": item["seq"], "state": item["state"],
                   "deviceTime": float(item.get("t") or 0), "manifestVersion": item.get("manifestVersion")}
            rec.update({k: item[k] for k in _HIST_FIELDS if item.get(k) is not None})
            out.append(rec)
    return out


STATUS_FIELDS = None


def _clean_record(rec: dict) -> dict:
    """Only CP-2 DeploymentStatus fields (a ring buffer may add bookkeeping keys)."""
    global STATUS_FIELDS
    if STATUS_FIELDS is None:
        from flexengine2 import contracts

        STATUS_FIELDS = set(contracts.load("status")["properties"])
    return {k: v for k, v in rec.items() if k in STATUS_FIELDS}


class LocalApi:
    def __init__(self, provider, *, socket_path: str | None = DEFAULT_SOCKET, group: str | None = None,
                 tcp_bind: str | None = None, token_file: str | None = None, log=None, clock=time.time):
        self.provider = provider
        self.socket_path, self.group = socket_path, group
        self.tcp_bind, self.token_file = tcp_bind, token_file
        self.log = log or _default_log
        self.clock = clock
        self._token: str | None = None
        self._servers: list = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._ticks: dict[str, tuple[int, float]] = {}
        self.created = clock()

    @classmethod
    def from_config(cls, cfg: dict, provider, **kw) -> "LocalApi":
        return cls(provider, **{**local_settings(cfg), **kw})

    # ---- server ---------------------------------------------------------------------------------------------
    def start(self) -> "LocalApi":
        if self.socket_path:
            self._servers.append(self._bind_unix(Path(self.socket_path)))
        if self.tcp_bind:
            from flexengine2.identity.secrets import read_secret_file

            if not self.token_file:
                raise ValueError("local.tcp needs tokenFile")
            self._token = read_secret_file(self.token_file)
            host, _, port = self.tcp_bind.rpartition(":")
            host = host.strip("[]")
            srv = _TcpServer((host, int(port)), _Handler, ipv6=":" in host)
            srv.api, srv.via = self, "tcp"
            self._servers.append(srv)
            self.log(f"read-only TCP API on {self.tcp_bind}")
        for srv in self._servers:
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                                 name=f"local-api-{srv.via}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    @property
    def tcp_address(self):
        for s in self._servers:
            if s.via == "tcp":
                return s.server_address[:2]
        return None

    def _bind_unix(self, path: Path):
        if not socket_path_fits(path):                  # instead of a bare "OSError: AF_UNIX path too long"
            raise ValueError(f"local API socket path is {len(os.fsencode(path))} bytes, longer than the "
                             f"{SUN_PATH_MAX} this platform allows: {path} (set local.socket to a shorter path)")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise FileExistsError(f"{path} exists and is not a socket")
            if ApiClient(str(path), timeout=1).reachable():
                raise RuntimeError(f"{path}: another engine already serves the local API")
            path.unlink()
        old = os.umask(0o117)
        try:
            srv = _UnixServer(str(path), _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o660)
        if self.group:
            try:
                os.chown(path, -1, grp.getgrnam(self.group).gr_gid)
            except (KeyError, PermissionError) as e:
                self.log(f"cannot give {path} to group {self.group!r}: {e}")
        srv.api, srv.via = self, "socket"
        self.log(f"local API on unix:{path}")
        return srv

    def stop(self):
        self._stop.set()
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
            if srv.via == "socket":
                with contextlib.suppress(OSError):
                    os.unlink(srv.server_address)
        self._servers.clear()

    # ---- dispatch -------------------------------------------------------------------------------------------
    def handle(self, method: str, target: str, body: bytes = b"", *, via: str = "socket",
               headers: dict | None = None, closed=None) -> Response:
        """One request (also used in-process by the CLI in offline mode)."""
        try:
            return self._route(method.upper(), target, body, via, {k.lower(): v for k, v in (headers or {}).items()},
                               closed or (lambda: False))
        except ApiError as e:
            hdr = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {}
            return Response(e.status, json.dumps(e.problem()).encode(), "application/problem+json", hdr)

    def _route(self, method, target, body, via, headers, closed) -> Response:
        u = urlsplit(target)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        if via == "tcp":
            if method != "GET":
                raise ApiError(403, "forbidden", "the TCP bind is read-only; use the Unix socket")
            auth = headers.get("authorization", "")
            if not (self._token and auth.startswith("Bearer ")
                    and hmac.compare_digest(auth[7:].strip().encode(), self._token.encode())):
                raise ApiError(401, "unauthorized", "bearer token required")
        if parts[:1] != ["v1"]:
            raise ApiError(404, "not found", u.path)
        rest = parts[1:]
        gets = {("health",): lambda: self._health(), ("status",): lambda: _json(200, self.status()),
                ("history",): lambda: _json(200, self.history(_since(q.get("since")), _dev_q(q))),
                ("logs",): lambda: self._logs(q, closed), ("metrics",): lambda: _json(200, self.metrics())}
        posts = {("bundle",): lambda: _json(202, self.import_bundle(body, _dev_q(q))),
                 ("reconcile",): lambda: _json(202, self.reconcile())}
        key = tuple(rest)
        if len(rest) == 4 and rest[0] == "devices" and rest[2] == "deployments":
            gets[key] = lambda: _json(200, self.deployment(rest[1], rest[3]))
        if len(rest) == 2 and rest[0] == "capabilities":
            gets[key] = lambda: _json(200, self.capabilities(rest[1]))
        table = gets if method in ("GET", "HEAD") else posts if method == "POST" else {}
        if key in table:
            return table[key]()
        if key in gets or key in posts:
            raise ApiError(405, "method not allowed", f"{method} {u.path}")
        raise ApiError(404, "not found", u.path)

    # ---- operations -----------------------------------------------------------------------------------------
    def _devices(self) -> list[dict]:
        return list(self.provider.devices())

    def _device(self, device_id: str) -> dict:
        for d in self._devices():
            if d["deviceId"] == device_id:
                return d
        raise ApiError(404, "unknown device", device_id)

    def _tick_age(self, d: dict, now: float) -> tuple[int, float | None]:
        e = d.get("engine")
        if e is None:
            return 0, None
        ticks = int(getattr(e, "ticks", 0) or 0)
        m = getattr(e, "_m", None)
        cands = [m.get("lastTickAt")] if isinstance(m, dict) else []
        cands += [getattr(e, attr, None) for attr in ("last_tick", "lastTickAt", "last_tick_at")]
        for at in cands:
            if isinstance(at, (int, float)) and at > 0:
                return ticks, max(0.0, now - at)
        seen = self._ticks.get(d["deviceId"])
        if seen is None or seen[0] != ticks:
            self._ticks[d["deviceId"]] = seen = (ticks, now)
        return ticks, max(0.0, now - seen[1])

    def health(self) -> dict:
        fn = getattr(self.provider, "health", None)
        if callable(fn):
            return fn()
        now = self.clock()
        devs = [d for d in self._devices() if d.get("engine") is not None]
        ticks, ages, ok = 0, [], bool(devs)
        for d in devs:
            n, age = self._tick_age(d, now)
            e = d["engine"]
            limit = max(STALE_MIN, 2 * (float(getattr(e, "poll", 1.0)) + float(getattr(getattr(e, "backoff", None),
                                                                                         "cap", 4.0))))
            ticks += n
            if age is not None:
                ages.append(age)
                ok = ok and age <= limit
        doc = {"ok": ok, "ticks": ticks}
        if ages:
            doc["lastTickAgeSeconds"] = round(max(ages), 3)
        return doc

    def _health(self) -> Response:
        h = self.health()
        return _json(200 if h.get("ok") else 503, h)

    def _records(self, d: dict, since: float = 0.0) -> list[tuple[dict, bool | None]]:
        """Status records of a device: ring buffer / history.jsonl (delivered) + the outbox (not yet delivered)."""
        st = _snapshot(d["engine"]) if d.get("engine") is not None else _read_state(Path(d["dataDir"]))
        try:
            hist = _history_reader(self.provider)(d["dataDir"], since)
        except Exception as e:  # noqa: BLE001  a broken history never breaks status
            self.log(f"history of {d['deviceId']}: {e!r}")
            hist = []
        outbox = [r for r in st.get("outbox") or [] if isinstance(r, dict)]
        pending = {(r.get("dep"), r.get("seq")) for r in outbox}
        seen, uniq = set(), []
        for rec in outbox + _entries(hist):  # the outbox copy is the full record
            k = (rec.get("dep"), rec.get("seq"))
            if k in seen:
                continue
            seen.add(k)
            if float(rec.get("deviceTime") or 0) >= since:
                uniq.append((rec, k not in pending))
        uniq.sort(key=lambda t: (float(t[0].get("deviceTime") or 0), t[0].get("seq") or 0))
        return uniq

    def status(self) -> dict:
        now = self.clock()
        started = getattr(self.provider, "started_at", None)
        engine = {"version": __version__, "spi": _spi(), "uptimeSeconds": round(max(0.0, now - started), 3) if started else 0.0,
                  "otel": _otel(), "mode": "running" if any(d.get("engine") is not None for d in self._devices())
                  else "offline"}
        if getattr(self.provider, "config_file", None):
            engine["configFile"] = str(self.provider.config_file)
        return {"apiVersion": API_VERSION, "kind": "EngineStatus", "engine": engine,
                "devices": [self.device_status(d, now) for d in self._devices()]}

    def device_status(self, d: dict, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        e = d.get("engine")
        st = _snapshot(e) if e is not None else _read_state(Path(d["dataDir"]))
        profiles = _profiles()
        drivers = [p for p in (d.get("drivers") or (list(e.drivers) if e is not None else [])) if p in profiles]
        online = d.get("online")
        if online is None and e is not None:
            online = getattr(e, "online", None)
            if online is None:  # backoff.reset() after every successful cloud round trip
                online = bool(getattr(e, "ticks", 0)) and getattr(getattr(e, "backoff", None), "n", 1) == 0
        etag_at = d.get("etagAt") if d.get("etagAt") is not None else (
            getattr(e, "etag_at", None) or getattr(e, "last_pull_at", None) if e is not None else None)
        out = {"deviceId": d["deviceId"], "role": d.get("role") or getattr(e, "role", "standalone-device"),
               "onboarded": bool(st.get("onboarded")), "manifestVersion": int(st.get("manifestVersion") or 0),
               "etag": st.get("etag"),
               "etagAgeSeconds": round(max(0.0, now - etag_at), 3) if isinstance(etag_at, (int, float)) else None,
               "online": bool(online), "outboxLength": len(st.get("outbox") or []),
               "downgradesRefused": int(st.get("downgradesRefused") or 0), "rejected": st.get("rejected"),
               "lastRefusal": st.get("lastRefusal"),
               "drivers": drivers, "journal": st.get("journal"),
               "deployments": self._deployments(st, self._records(d) if st else [])}
        if d.get("parent"):
            out["parent"] = d["parent"]
        return out

    def _deployments(self, st: dict, records) -> list[dict]:
        from flexengine2 import contracts

        profiles = _profiles()
        desired_doc = st.get("desired") or {}
        desired = {x["metadata"]["id"]: x for x in desired_doc.get("deployments") or []
                   if isinstance(x, dict) and "metadata" in x}
        applied = st.get("applied") or {}
        last: dict[tuple, dict] = {}   # (dep, state) -> newest report of that state
        for rec, _ in records:
            last[(rec.get("dep"), rec.get("state"))] = rec
        mv = desired_doc.get("manifestVersion")
        journal = st.get("journal") or {}
        rejected = st.get("rejected") or {}
        out = []
        for dep_id in list(desired) + [k for k in applied if k not in desired]:
            d, a = desired.get(dep_id), applied.get(dep_id)
            spec = d or (a or {}).get("dep") or {}
            driver = spec.get("x-flex-driver")
            if driver not in profiles:
                continue
            try:
                rv = spec["spec"]["deploymentProfile"]["components"][0]["properties"]
            except (KeyError, IndexError, TypeError):
                rv = {}
            wl = (a or {}).get("wl") or {}
            want = rv.get("digest")
            if journal.get("dep") == dep_id:
                state, ver, dig = "Installing", rv.get("revision"), journal.get("digest") or want
            elif d is None:
                state, ver, dig = "Removing", wl.get("version"), wl.get("digest")
            elif a and wl.get("digest") == want and (st.get("failed") or {}).get(dep_id) != want:
                state, ver, dig = "Installed", wl.get("version"), wl.get("digest")
            elif (st.get("failed") or {}).get(dep_id) == want:
                state, ver, dig = "Failed", rv.get("revision"), want
            elif rejected.get("deployment") == dep_id and rejected.get("manifestVersion") == desired_doc.get("manifestVersion"):
                state, ver, dig = "Failed", rv.get("revision"), want
            else:
                state, ver, dig = "Pending", rv.get("revision"), want
            item = {"id": dep_id, "driver": driver, "state": state}
            if ver:
                item["version"] = str(ver)
            if isinstance(dig, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", dig):
                item["digest"] = dig
            if state == "Installed":
                item["health"] = "healthy" if (st.get("health") or {}).get(dep_id) else "unhealthy"
            prev = (a or {}).get("previous")
            if a is not None:
                item["previousVersion"] = (prev or {}).get("version")
            rec = last.get((dep_id, state))
            if rec and state in ("Failed", "Pending", "Installing") and rec.get("manifestVersion") not in (mv, None):
                rec = None             # a report of an earlier manifest says nothing about this one
            reason = None
            if rec:
                if isinstance(rec.get("deviceTime"), (int, float)):
                    item["since"] = float(rec["deviceTime"])
                reason = rec.get("reason")
                for k in ("rolledBackTo", "failFast", "restarts"):
                    if k in rec and (rec[k] is None or isinstance(rec[k], (str, bool, int))):
                        item[k] = rec[k]
            if state == "Failed" and not reason and rejected.get("deployment") == dep_id:
                reason = rejected.get("reason") and f"artefact rejected: {rejected['reason']}"
            if reason:
                item["reason"] = str(reason)
                code = contracts.reason_code(str(reason), "reason")
                if code:
                    item["reasonCode"] = code["code"]
            out.append(item)
        return out

    def deployment(self, device_id: str, dep_id: str) -> dict:
        for dep in self.device_status(self._device(device_id))["deployments"]:
            if dep["id"] == dep_id:
                return dep
        raise ApiError(404, "unknown deployment", f"{device_id}/{dep_id}")

    def history(self, since: float | None = None, device: str | None = None) -> list[dict]:
        since = self.clock() - 3600 if since is None else since
        devs = [self._device(device)] if device else self._devices()
        out = []
        for d in devs:
            for rec, delivered in self._records(d, since):
                e = {"deviceId": d["deviceId"], "record": _clean_record(rec)}
                if delivered is not None:
                    e["delivered"] = bool(delivered)
                out.append(e)
        out.sort(key=lambda e: (float(e["record"].get("deviceTime") or 0), e["deviceId"]))
        return out

    def capabilities(self, device_id: str) -> dict:
        d = self._device(device_id)
        if d.get("engine") is None:
            raise ApiError(503, "engine not running", "capabilities come from the running engine's drivers")
        return d["engine"].capabilities()

    def metrics(self) -> dict:
        fn = getattr(self.provider, "metrics", None)
        out = {}
        if callable(fn):
            out = fn() or {}
        else:
            for d in self._devices():
                m = getattr(d.get("engine"), "metrics", None)
                if callable(m):
                    out[d["deviceId"]] = m()
        if not out and not any(d.get("engine") is not None for d in self._devices()):
            raise ApiError(503, "engine not running", "metrics come from the running engine")
        return out

    def import_bundle(self, data: bytes, device: str | None = None) -> dict:
        if not data:
            raise ApiError(400, "bad request", "empty body (an offline bundle tar is expected)")
        target = bundle_device(data)
        if target is None:
            raise ApiError(422, "bundle refused", "FE-BUN-001: bundle without desired state")
        if device and device != target:
            raise ApiError(422, "bundle refused", f"bundle is for device {target!r}, not {device!r}")
        self._device(target)
        fn = getattr(self.provider, "import_bundle", None)
        if not callable(fn):
            raise ApiError(503, "not supported", "this host cannot import bundles")
        return fn(target, data)

    def reconcile(self) -> dict:
        fn = getattr(self.provider, "reconcile", None)
        if not callable(fn):
            raise ApiError(503, "not supported", "no running engine to nudge")
        return {"queued": list(fn() or [])}

    # ---- logs -----------------------------------------------------------------------------------------------
    def _log_sources(self, device: str | None) -> list[tuple[str, str, Path]]:
        devs = [self._device(device)] if device else self._devices()
        out = []
        extra = getattr(self.provider, "log_files", None)
        for d in devs:
            dd = Path(d["dataDir"])
            out.append((d["deviceId"], "actions", dd / "actions.jsonl"))
            files = list(extra(d["deviceId"], dd)) if callable(extra) else []
            files += [p for p in (dd / "engine.log", dd / "engine.jsonl", dd / "log.jsonl") if p not in files]
            out += [(d["deviceId"], "log", Path(p)) for p in files]
        return out

    @staticmethod
    def _log_line(device: str, source: str, line: str) -> dict | None:
        line = line.rstrip("\n")
        if not line.strip():
            return None
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError
        except ValueError:
            rec = {"msg": line}
        rec.setdefault("device", device)
        rec.setdefault("source", source)
        if "t" not in rec:
            rec["t"] = _epoch(rec.get("ts"))
        if source == "actions" and "action" in rec:  # same field names as the structured engine log (obs/logging)
            rec.setdefault("level", "info")
            rec.setdefault("event", f"action.{rec['action']}")
            if rec.get("dep"):
                rec.setdefault("deployment", rec["dep"])
            rec.setdefault("msg", f"{rec['action']} {rec.get('dep', '')} {rec.get('version', '')}".strip())
        if isinstance(rec.get("t"), (int, float)):
            from flexengine2.util import iso

            rec.setdefault("ts", iso(float(rec["t"])))
        return rec

    def _logs(self, q: dict, closed) -> Response:
        device = _dev_q(q)
        follow = q.get("follow", "0")
        if follow not in ("0", "1"):
            raise ApiError(400, "bad request", "follow must be 0 or 1")
        try:
            tail = int(q.get("tail", LOG_TAIL))
        except ValueError:
            raise ApiError(400, "bad request", "tail must be an integer") from None
        sources = self._log_sources(device)
        lines, offsets = [], {}
        for dev, src, p in sources:
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                offsets[p] = 0
                continue
            offsets[p] = len(data)
            for raw in data.decode("utf-8", "replace").splitlines():
                rec = self._log_line(dev, src, raw)
                if rec:
                    lines.append(rec)
        lines.sort(key=lambda r: float(r["t"]) if isinstance(r.get("t"), (int, float)) else 0.0)
        lines = lines[-tail:] if tail > 0 else []
        head = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in lines)
        if follow == "0":
            return Response(200, head, "application/x-ndjson")

        def stream():
            if head:
                yield head
            pending = {p: b"" for p in offsets}
            while not self._stop.is_set() and not closed():
                out = []
                for dev, src, p in sources:
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size < offsets.get(p, 0):  # rotated / truncated
                        offsets[p] = 0
                    if size > offsets.get(p, 0):
                        with open(p, "rb") as f:
                            f.seek(offsets.get(p, 0))
                            chunk = f.read(size - offsets.get(p, 0))
                        offsets[p] = size
                        buf = pending.get(p, b"") + chunk
                        *done, pending[p] = buf.split(b"\n")
                        for raw in done:
                            rec = self._log_line(dev, src, raw.decode("utf-8", "replace"))
                            if rec:
                                out.append(json.dumps(rec, sort_keys=True).encode() + b"\n")
                if out:
                    yield b"".join(out)
                else:
                    self._stop.wait(FOLLOW_POLL)
        return Response(200, stream(), "application/x-ndjson")


def _epoch(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        import calendar

        m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", ts)
        if m:
            return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) + float(m.group(2) or 0)
    return None


def _spi() -> str:
    from flexengine2.drivers.base import SPI_VERSION

    return SPI_VERSION


def _otel() -> bool:
    if os.environ.get("FLEX_OTEL") != "1":
        return False
    import importlib.util

    return importlib.util.find_spec("opentelemetry") is not None


def _since(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        raise ApiError(400, "bad request", "since must be unix seconds") from None


def _dev_q(q: dict) -> str | None:
    d = q.get("device")
    if d is not None and not ID_RE.match(d):
        raise ApiError(400, "bad request", f"invalid device id {d!r}")
    return d or None


# ---- HTTP plumbing ---------------------------------------------------------------------------------------------
class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    via = "socket"


class _TcpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    via = "tcp"

    def __init__(self, addr, handler, ipv6=False):
        if ipv6:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"flexengine/{__version__}"

    def address_string(self):
        return "unix" if self.server.via == "socket" else super().address_string()

    def log_message(self, fmt, *args):  # quiet: the engine log is for the engine
        pass

    def _closed(self) -> bool:
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _serve(self):
        api: LocalApi = self.server.api
        body = b""
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self._send(Response(411, json.dumps({"title": "length required", "status": 411}).encode(),
                                    "application/problem+json"))
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._send(Response(413, json.dumps({"title": "too large", "status": 413}).encode(),
                                    "application/problem+json"))
                return
            body = self.rfile.read(n) if n else b""
        resp = api.handle(self.command, self.path, body, via=self.server.via, headers=dict(self.headers.items()),
                          closed=self._closed)
        self._send(resp)

    def _send(self, resp: Response):
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        try:
            if not resp.streaming:
                self.send_header("Content-Length", str(len(resp.body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(resp.body)
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()
            for chunk in resp.body:
                if chunk:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            if resp.streaming:
                close = getattr(resp.body, "close", None)
                if close:
                    close()

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = _serve


# ---- client ------------------------------------------------------------------------------------------------------
class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


class ApiClient:
    """Client of the local API over the Unix socket (the CLI's online mode)."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, timeout: float | None = 10):
        self.socket_path, self.timeout = socket_path, timeout

    def reachable(self) -> bool:
        try:
            self.request("GET", "/v1/health")
            return True
        except OSError:
            return False

    def request(self, method: str, target: str, body: bytes = b"", ctype: str | None = None,
                timeout: float | None = -1) -> Response:
        c = _UnixConnection(self.socket_path, self.timeout if timeout == -1 else timeout)
        hdr = {"Content-Type": ctype} if ctype else {}
        c.request(method, target, body=body or None, headers=hdr)
        r = c.getresponse()
        ct = r.getheader("Content-Type", "")
        if r.getheader("Transfer-Encoding", "").lower() == "chunked" and "ndjson" in ct:
            def lines():
                try:
                    while True:
                        line = r.readline()
                        if not line:
                            return
                        yield line
                finally:
                    c.close()
            return Response(r.status, lines(), ct)
        data = r.read()
        c.close()
        return Response(r.status, data, ct)


def quote_id(s: str) -> str:
    return quote(s, safe="")

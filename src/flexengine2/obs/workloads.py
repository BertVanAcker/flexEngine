"""Workload observation (UM-4): per-deployment resources and the workloads' own stdout/stderr.

    obs = WorkloadObserver(engine, cfg.get("observe"))     # Engine does this; the host starts its thread
    obs.start(stop_event)                                  # one daemon thread per device engine
    obs.step()                                             # or by hand (tests): one round, returns the next delay
    obs.resources()                                        # {dep: DeploymentResources + sampledAt, driver}

The observer calls the drivers' optional SPI members ``resources(dep_ids)`` and ``logs(dep_id, cursor, limit)``
(drivers/base.py) from its own thread, never from the reconcile loop: a slow executor CLI (``docker stats`` takes a
second) delays the next sample, not a tick. Drivers without them are skipped. Every round is bounded: one
``resources`` call per driver, one ``logs`` call per deployment of at most ``LINES_PER_CALL`` lines, each line cut
at ``MSG_MAX`` characters.

Workload lines go to ``<dataDir>/workload.jsonl`` (one JSON object per line: ``t, dep, deployment, stream?,
container?, level, event: "workload", msg, truncated?``), rotated to ``workload.jsonl.1`` when it would grow past
``observe.workloadLogMaxBytes`` (default 1 MiB), so the disk use is bounded by twice that. GET /v1/logs serves them
as ``source: workload`` with the same tail / follow semantics as the engine log (local/api.py). The drivers'
cursors are kept in ``<dataDir>/workload-logs.json``, so a restarted engine continues where it was instead of
collecting the same lines again.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from flexengine2.drivers.base import supports
from flexengine2.util import atomic_write_json, read_json, repair_jsonl_tail

LOG_FILE = "workload.jsonl"
CURSOR_FILE = "workload-logs.json"
RESOURCES_EVERY = 10.0
LOGS_EVERY = 5.0
LOG_MAX_BYTES = 1 << 20
LINES_PER_CALL = 200
MSG_MAX = 4096
IDLE = 30.0                 # thread wake-up when both observations are off
RES_KEYS = {"cpuPercent": float, "memoryBytes": int, "memoryLimitBytes": int, "memoryPercent": float, "pids": int}

__all__ = ["WorkloadObserver", "LOG_FILE", "CURSOR_FILE", "settings"]


def settings(cfg: dict | None) -> dict:
    """The `observe` block of engine.yaml with its defaults (0 = off)."""
    cfg = cfg or {}
    return {"resourcesEverySeconds": float(cfg.get("resourcesEverySeconds", RESOURCES_EVERY)),
            "workloadLogsEverySeconds": float(cfg.get("workloadLogsEverySeconds", LOGS_EVERY)),
            "workloadLogMaxBytes": int(cfg.get("workloadLogMaxBytes", LOG_MAX_BYTES))}


def clean_resources(raw) -> dict:
    """Only the DeploymentResources keys, with their types, never negative (a driver's slip is not passed on)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, typ in RES_KEYS.items():
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
            continue
        out[k] = round(float(v), 2) if typ is float else int(v)
    return out


class WorkloadObserver:
    def __init__(self, engine, cfg: dict | None = None, clock=time.time):
        self.engine = engine
        self.cfg = settings(cfg)
        self.clock = clock
        self.dir = Path(engine.dir)
        self.log_path = self.dir / LOG_FILE
        self.cursor_path = self.dir / CURSOR_FILE
        repair_jsonl_tail(self.log_path)
        self._cursors: dict[str, str | None] = {k: v for k, v in (read_json(self.cursor_path, {}) or {}).items()
                                                if isinstance(k, str) and (v is None or isinstance(v, str))}
        self._res: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._next = {"resources": 0.0, "logs": 0.0}
        self._warned: set[tuple[str, str]] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- what is observed -------------------------------------------------------------------------------------
    def _deployments(self) -> list[tuple[str, object]]:
        """(dep_id, driver) of every applied deployment (a copy: the engine thread changes `applied`)."""
        applied = (self.engine.state.get("applied") or {}).copy()
        out = []
        for dep_id, rec in sorted(applied.items()):
            try:
                drv = self.engine.driver_for(rec["dep"])
            except (KeyError, TypeError, AttributeError):
                drv = None
            if drv is not None:
                out.append((dep_id, drv))
        return out

    def _warn(self, kind: str, dep: str | None, drv, e: BaseException) -> None:
        key = (kind, f"{getattr(drv, 'name', '?')}/{dep or ''}")
        if key in self._warned:
            return
        self._warned.add(key)
        try:
            self.engine.log(f"observe {kind}: {getattr(drv, 'name', '?')} raised {type(e).__name__}: {e}"[:400],
                            "warning", "observe.failed", dep=dep)
        except Exception:  # noqa: BLE001
            pass

    # ---- resources --------------------------------------------------------------------------------------------
    def sample_resources(self) -> dict[str, dict]:
        now = self.clock()
        by_drv: dict[int, tuple[object, list[str]]] = {}
        for dep_id, drv in self._deployments():
            if supports(drv, "resources"):
                by_drv.setdefault(id(drv), (drv, []))[1].append(dep_id)
        out: dict[str, dict] = {}
        for drv, deps in by_drv.values():
            try:
                got = drv.resources(list(deps)) or {}
            except Exception as e:  # noqa: BLE001 - SPI: never raises; a driver that does is skipped this round
                self._warn("resources", None, drv, e)
                continue
            for dep in deps:
                r = clean_resources(got.get(dep) if isinstance(got, dict) else None)
                if r:
                    out[dep] = {"sampledAt": round(now, 3), "driver": str(getattr(drv, "name", "?")), **r}
        with self._lock:
            self._res = out
        return out

    def resources(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._res.items()}

    # ---- workload logs ----------------------------------------------------------------------------------------
    def _append(self, recs: list[dict]) -> None:
        if not recs:
            return
        data = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in recs)
        cap = max(4096, self.cfg["workloadLogMaxBytes"])
        try:
            size = self.log_path.stat().st_size
        except OSError:
            size = 0
        if size and size + len(data) > cap:
            os.replace(self.log_path, self.log_path.with_name(LOG_FILE + ".1"))
        with open(self.log_path, "ab") as f:
            f.write(data[-cap:] if len(data) > cap else data)   # one oversized batch: its newest part
            f.flush()

    def collect_logs(self) -> int:
        """One round over every deployment whose driver has logs(); returns the lines written."""
        deps = [(d, drv) for d, drv in self._deployments() if supports(drv, "logs")]
        recs: list[dict] = []
        changed = False
        for dep_id, drv in deps:
            cur = self._cursors.get(dep_id)
            try:
                batch = drv.logs(dep_id, cur, LINES_PER_CALL) or {}
            except Exception as e:  # noqa: BLE001 - SPI: never raises
                self._warn("logs", dep_id, drv, e)
                continue
            now = self.clock()
            lines = batch.get("lines") if isinstance(batch, dict) else None
            for ln in (lines if isinstance(lines, list) else [])[-LINES_PER_CALL:]:
                if not isinstance(ln, dict) or not isinstance(ln.get("msg"), str):
                    continue
                msg = ln["msg"].rstrip("\r\n")
                t = ln.get("t")
                rec = {"t": round(float(t), 6) if isinstance(t, (int, float)) and not isinstance(t, bool) else
                       round(now, 6), "dep": dep_id, "deployment": dep_id, "level": "info", "event": "workload",
                       "msg": msg[:MSG_MAX]}
                if len(msg) > MSG_MAX:
                    rec["truncated"] = True
                if ln.get("stream") in ("stdout", "stderr"):
                    rec["stream"] = ln["stream"]
                if isinstance(ln.get("container"), str) and ln["container"]:
                    rec["container"] = ln["container"][:200]
                recs.append(rec)
            new = batch.get("cursor") if isinstance(batch, dict) else cur
            if (new is None or isinstance(new, str)) and new != cur:
                self._cursors[dep_id] = new
                changed = True
        gone = set(self._cursors) - {d for d, _ in self._deployments()}
        for d in gone:
            del self._cursors[d]
            changed = True
        recs.sort(key=lambda r: r["t"])
        try:
            self._append(recs)
            if changed:
                atomic_write_json(self.cursor_path, self._cursors)
        except OSError as e:
            self._warn("logs", None, self, e)
        return len(recs)

    # ---- driving ----------------------------------------------------------------------------------------------
    def step(self, now: float | None = None) -> float:
        """Run what is due; returns the seconds until the next round. Never raises."""
        now = time.monotonic() if now is None else now
        delays = []
        for kind, every, fn in (("resources", self.cfg["resourcesEverySeconds"], self.sample_resources),
                                ("logs", self.cfg["workloadLogsEverySeconds"], self.collect_logs)):
            if every <= 0:
                continue
            if now >= self._next[kind]:
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 - observation never takes the engine down
                    self._warn(kind, None, self, e)
                self._next[kind] = now + every
            delays.append(max(0.0, self._next[kind] - now))
        return min(delays) if delays else IDLE

    def start(self, stop_event: threading.Event | None = None) -> threading.Thread | None:
        """The observer thread (idempotent; none when both observations are off)."""
        if self.cfg["resourcesEverySeconds"] <= 0 and self.cfg["workloadLogsEverySeconds"] <= 0:
            return None
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        stop = stop_event or self._stop

        def loop():
            while not stop.is_set() and not self._stop.is_set() and not getattr(self.engine, "stopping", False):
                stop.wait(max(0.05, self.step()))

        self._thread = threading.Thread(target=loop, name=f"observe-{self.engine.device}", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

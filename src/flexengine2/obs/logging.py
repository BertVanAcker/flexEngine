"""Structured logging of the engine (EV-05): one record per line, journald-friendly (no colours, no multi-line).

    from flexengine2.obs import logging as flog
    flog.configure("json")                         # or "text"; cfg `logFormat` (default json)
    flog.emit("info", "accepted desired state v3", device="panda", event="manifest.accepted", version="1.1.0")
    log = flog.DeviceLog("panda"); log("installing", event="install", dep="d1")

json  {"ts": "2026-10-17T10:00:00.123Z", "level": "info", "device": "panda", "event": "action.apply",
       "dep": "d1", "version": "1.1.0", "msg": "apply d1 1.1.0", ...extra key/values}
text  2026-10-17T10:00:00.123Z [panda] apply d1 1.1.0          (the probe/P2 format; warnings/errors get a
                                                                 "warning: " / "error: " prefix)

Records go to stdout (systemd forwards it to the journal) unless `configure(stream=...)` says otherwise. The stream is
looked up at emit time, so pytest's capsys and a redirected sys.stdout both work. Writes are serialised by a lock:
the per-device worker threads (host.py) never interleave a line. `bridge_stdlib()` routes the stdlib `logging`
records of the other modules (e.g. flexengine2.cloud.client's plain-HTTP warning) through the same formatter.
"""

from __future__ import annotations

import json
import logging as _stdlog
import os
import sys
import threading

from flexengine2.util import iso

FORMATS = ("json", "text")
LEVELS = ("debug", "info", "warning", "error")
_lock = threading.Lock()
_cfg: dict = {"format": None, "stream": None}


def configure(fmt: str | None = None, stream=None) -> str:
    """Set the output format (json | text) and optionally the stream. Returns the active format."""
    if fmt is not None:
        if fmt not in FORMATS:
            raise ValueError(f"logFormat must be one of {', '.join(FORMATS)}; got {fmt!r}")
        _cfg["format"] = fmt
    if stream is not None:
        _cfg["stream"] = stream
    return get_format()


def get_format() -> str:
    """Configured format, else $FLEX_LOG_FORMAT, else json."""
    f = _cfg["format"] or os.environ.get("FLEX_LOG_FORMAT")
    return f if f in FORMATS else "json"


def reset() -> None:
    """Back to the defaults (tests)."""
    _cfg.update(format=None, stream=None)


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def format_record(level: str, msg: str, *, device: str | None = None, event: str | None = None,
                  dep: str | None = None, version: str | None = None, fmt: str | None = None, ts: str | None = None,
                  **kv) -> str:
    fmt = fmt or get_format()
    ts = ts or iso()
    msg = " ".join(str(msg).splitlines()) if "\n" in str(msg) or "\r" in str(msg) else str(msg)
    if fmt == "text":
        prefix = "" if level in ("info", "debug") else f"{level}: "
        return f"{ts} [{device or '-'}] {prefix}{msg}"
    rec = {"ts": ts, "level": level, "device": device, "event": event or "log"}
    if dep is not None:
        rec["dep"] = dep
    if version not in (None, ""):
        rec["version"] = version
    rec["msg"] = msg
    for k, v in kv.items():
        if k not in rec and v is not None:
            rec[k] = _jsonable(v)
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"))


def emit(level: str, msg: str, **fields) -> None:
    line = format_record(level, msg, **fields)
    stream = _cfg["stream"] or sys.stdout
    with _lock:
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):  # closed/broken stdout must never take an engine down
            pass


class DeviceLog:
    """Logger bound to one device: `log(msg, level=..., event=..., dep=..., **kv)`."""

    def __init__(self, device: str | None):
        self.device = device

    def __call__(self, msg: str, level: str = "info", event: str | None = None, **kv) -> None:
        emit(level, msg, device=self.device, event=event, **kv)

    def warning(self, msg: str, event: str | None = None, **kv) -> None:
        self(msg, "warning", event, **kv)

    def error(self, msg: str, event: str | None = None, **kv) -> None:
        self(msg, "error", event, **kv)


class _Bridge(_stdlog.Handler):
    def emit(self, record):  # noqa: D401 - logging.Handler API
        try:
            lvl = {_stdlog.DEBUG: "debug", _stdlog.INFO: "info", _stdlog.WARNING: "warning"}.get(record.levelno, "error")
            emit(lvl, record.getMessage(), device=getattr(record, "device", None), event=record.name)
        except Exception:  # noqa: BLE001
            self.handleError(record)


_bridge = _Bridge()


def bridge_stdlib(level: int = _stdlog.INFO) -> None:
    """Send stdlib logging of the flexengine2 package through this formatter (idempotent)."""
    lg = _stdlog.getLogger("flexengine2")
    if _bridge not in lg.handlers:
        lg.addHandler(_bridge)
    if lg.level == _stdlog.NOTSET or lg.level > level:
        lg.setLevel(level)
    # propagate stays on: without root handlers (production) nothing is printed twice, as the lastResort handler
    # is only used when no handler exists anywhere in the chain

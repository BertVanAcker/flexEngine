"""Local status history (EV-09): the last N state transitions of one device, in memory and on disk.

    h = History(data_dir, "panda", max_entries=500)
    h.record("Installing", dep="d1", version="1.1.0")            # a reported state (plan §3.3)
    h.record("TESTING", dep="d1", kind="internal")               # an internal one (PREPARING, GATE, TESTING, ...)
    read(data_dir, since=time.time() - 3600)                     # for the local API / CLI (another thread/process)

On disk: `<dataDir>/history.jsonl`, one JSON object per line ({"t", "device", "dep", "state", "kind", ...}). When the
file holds `max_entries` lines it is rotated to `history.jsonl.1` (the previous `.1` is dropped), so the disk use is
bounded by 2 x N lines and `read()` always sees at least the last N transitions. Appends are flushed but not fsynced:
the history is diagnostic, the outbox and state.json are the durable record. A torn last line (power cut) is skipped
by readers and cut off when the engine starts (util.repair_jsonl_tail), so the next record is not glued to it.
"""

from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

from flexengine2.util import repair_jsonl_tail

FILE = "history.jsonl"
DEFAULT_MAX = 500


def _read_file(p: Path) -> list[dict]:
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # torn line
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


def read(data_dir: str | Path, since: float | None = None, limit: int | None = None) -> list[dict]:
    """Transitions of the device whose data directory is `data_dir`, oldest first; only those with `t > since`
    when given, at most the newest `limit`. Safe to call while the engine appends (from another thread or process)."""
    d = Path(data_dir)
    recs = _read_file(d / f"{FILE}.1") + _read_file(d / FILE)
    if since is not None:
        recs = [r for r in recs if float(r.get("t", 0)) > since]
    if limit is not None:
        recs = recs[-limit:] if limit > 0 else []
    return recs


class History:
    def __init__(self, data_dir: str | Path, device: str, max_entries: int = DEFAULT_MAX):
        self.dir, self.device = Path(data_dir), device
        self.max = max(1, int(max_entries))
        self.path = self.dir / FILE
        self._lock = threading.Lock()
        try:
            repair_jsonl_tail(self.path, fsync=False)
        except OSError:
            pass
        cur = _read_file(self.path)
        self._lines = len(cur)
        self.ring: collections.deque = collections.deque(read(self.dir, limit=self.max), maxlen=self.max)

    def record(self, state: str, dep: str | None = None, kind: str = "reported", **kw) -> dict:
        rec = {"t": round(time.time(), 6), "device": self.device, "dep": dep, "state": state, "kind": kind,
               **{k: v for k, v in kw.items() if v is not None}}
        line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
        with self._lock:
            self.ring.append(rec)
            try:
                if self._lines >= self.max:
                    try:
                        os.replace(self.path, self.path.with_name(f"{FILE}.1"))
                    except FileNotFoundError:
                        pass
                    self._lines = 0
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                self._lines += 1
            except OSError:
                pass  # disk full etc.: the in-memory ring still has it; never fail a transition for the history
        return rec

    def recent(self, since: float | None = None) -> list[dict]:
        with self._lock:
            recs = list(self.ring)
        return [r for r in recs if since is None or r["t"] > since]

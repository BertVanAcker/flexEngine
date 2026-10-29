"""Example capture workload for flexEngine's native profile (docs/capture-release.md): a read-only data collector.

It samples one source (collector.json, read-only in the payload), writes the samples as JSON Lines into rotating
capture files under its data directory, applies a retention policy and serves its health on FLEX_HEALTH_PORT.
Standard library only, so the payload needs no wheels.

    engine environment     FLEX_HEALTH_PORT, FLEX_DATA_DIR (writable), FLEX_PARAM_<NAME> (deployment parameters)
    parameters             output_dir, prefix, sample_period_s, rotate_s, keep_files, max_mb, stale_s
    output                 <output dir>/captures/<prefix>-<UTC start>.jsonl; the open file ends in .part and is
                           renamed when it is complete (rotation or SIGTERM; after a crash at the next start), so a
                           finished file never changes again
    health                 GET /healthz: 200 while samples arrive and the output directory is writable, else 503

It never writes into its own tree (the payload is read-only on the device) and never writes to the source.
"""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def settings() -> dict:
    """collector.json (the release's defaults), overridden by the deployment's parameters."""
    cfg = json.loads((HERE / "collector.json").read_text())
    out = dict(cfg["defaults"])
    for key, default in cfg["defaults"].items():
        raw = os.environ.get(f"FLEX_PARAM_{key.upper()}")
        if raw is not None:
            out[key] = type(default)(float(raw)) if isinstance(default, (int, float)) else raw
    out["source"] = cfg["source"]
    out["output_dir"] = (os.environ.get("FLEX_PARAM_OUTPUT_DIR") or os.environ.get("FLEX_DATA_DIR")
                         or str(HERE.parent / "data"))
    return out


def read_source(src: dict, n: int) -> dict:
    """One sample. `file`: the first line of a file (e.g. /proc/loadavg), opened read-only; `synthetic`: a counter
    and a sine wave, for a bench without a real source."""
    if src["kind"] == "file":
        with open(src["path"], encoding="utf-8") as f:
            return {"value": f.readline().strip()}
    return {"n": n, "value": round(math.sin(n / 10.0), 6)}


class Capture:
    def __init__(self, s: dict):
        self.s = s
        self.dir = Path(s["output_dir"]) / "captures"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = None
        self.path = None
        self.opened = 0.0
        self.last_sample = 0.0
        self.samples = 0
        self.error = None
        self.recover()

    def recover(self):
        """A file left open by a crash or a SIGKILL holds every sample up to its last line (one flush per line):
        complete it at the next start, so nothing stays `.part` for ever."""
        for part in sorted(self.dir.glob("*.jsonl.part")):
            part.rename(part.with_suffix(""))

    def _open(self, now: float):
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = self.dir / f"{self.s['prefix']}-{stamp}.jsonl.part"
        self.file = open(self.path, "a", encoding="utf-8")
        self.opened = now

    def close(self):
        """Complete the open file: flush, fsync, drop `.part`."""
        if self.file is None:
            return
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        self.path.rename(self.path.with_suffix(""))
        self.file = None

    def retain(self):
        """Keep at most keep_files complete files and max_mb in total; the oldest go first."""
        done = sorted(self.dir.glob("*.jsonl"))
        total = sum(p.stat().st_size for p in done)
        while done and (len(done) > self.s["keep_files"] or total > self.s["max_mb"] * 1024 * 1024):
            old = done.pop(0)
            total -= old.stat().st_size
            old.unlink(missing_ok=True)

    def step(self, now: float):
        if self.file is not None and now - self.opened >= self.s["rotate_s"]:
            self.close()
            self.retain()
        if self.file is None:
            self._open(now)
        try:
            rec = {"t": round(now, 3), **read_source(self.s["source"], self.samples)}
        except OSError as e:                         # source gone: no sample; health turns 503 after stale_s
            self.error = str(e)
            return
        self.file.write(json.dumps(rec, sort_keys=True) + "\n")
        self.file.flush()
        self.samples += 1
        self.last_sample, self.error = now, None

    def healthy(self) -> tuple[bool, dict]:
        fresh = time.time() - self.last_sample < self.s["stale_s"]
        writable = os.access(self.dir, os.W_OK)
        return fresh and writable, {"samples": self.samples, "fresh": fresh, "writable": writable,
                                    "file": self.path.name if self.path else None, "error": self.error}


def serve_health(cap: Capture, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok, body = cap.healthy()
            data = json.dumps(body).encode()
            self.send_response(404 if self.path != "/healthz" else 200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):                # no access log on stderr
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    s = settings()
    cap = Capture(s)
    srv = serve_health(cap, int(os.environ.get("FLEX_HEALTH_PORT", "8080")))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())   # the engine stops a workload with SIGTERM
    print(f"capture: {s['source']['kind']} every {s['sample_period_s']} s into {cap.dir}", flush=True)
    while not stop.is_set():
        cap.step(time.time())
        stop.wait(s["sample_period_s"])
    cap.close()
    cap.retain()
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

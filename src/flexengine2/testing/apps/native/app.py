"""Native (container-free) flexNode stand-in, run by the process driver.

Serves GET /healthz on 127.0.0.1:$FLEX_HEALTH_PORT. Reads release.json next to it for version/fault flags.
"""
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REL = json.loads((Path(__file__).parent / "release.json").read_text())
PORT = int(os.environ.get("FLEX_HEALTH_PORT", "0"))
READY = threading.Event()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = READY.is_set() and self.path == "/healthz"
        body = (f"ok {REL['app']} {REL['version']}" if ok else "not ready").encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    signal.signal(signal.SIGTERM, lambda *a: (srv.shutdown(), sys.exit(0)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[{REL['app']} {REL['version']}] pid {os.getpid()} on :{PORT}", flush=True)
    time.sleep(float(os.environ.get("FLEX_PARAM_STARTUP_DELAY", "0.2")))
    if not REL.get("fail_health"):
        READY.set()
    crash = REL.get("crash_after_s") or os.environ.get("FLEX_PARAM_CRASH_AFTER")
    t0 = time.time()
    while True:
        time.sleep(0.5)
        if crash and time.time() - t0 > float(crash):
            print("crashing (simulated)", flush=True)
            os._exit(3)


if __name__ == "__main__":
    main()

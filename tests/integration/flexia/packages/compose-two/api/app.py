"""Compose service with its own health endpoint: /healthz, and /greeting returns $GREETING (set in compose.yaml)."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/healthz": b"ok\n", "/greeting": os.environ.get("GREETING", "").encode()}.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), H).serve_forever()

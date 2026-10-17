"""Test support for the ported one-host integration suites (P3 MCU, P4 App Lab, demo/edge; EV-02).

* `HostProc`  the engine under test as a real subprocess: `python -m flexengine2.testing.edge_lab run --config X`,
              i.e. the production CLI (`flexengine2.local.cli.main`, = `flexengine run`) behind a thin guard that
              records the subprocess' `sys.modules` and `sys.path` (env `FLEX_TEST_MODULES_FILE`), so a fixture can
              assert that no probe/demo engine module (`flexp2`, `flexp3`, `flexp4`, `flexedge`) was loaded.
* `admin()`   bearer-token client of the reference cloud fake (`flexengine2.testing.fakecloud`) northbound API.
* `wait()`    polling helper with a log tail in the failure message.
* `relay-inject` / `relay-status`: the demo/edge relay CLI (`python -m flexedge relay-inject ...`) on the V2 relay,
              which `flexengine` does not offer yet (V-62; proposed for local/cli.py).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

FORBIDDEN = ("flexp2", "flexp3", "flexp4", "flexedge", "flexobs")
_FORBIDDEN_PATH_PARTS = ("/probes/", "/demo/edge", "/demo/obs")


# ---- engine subprocess --------------------------------------------------------------------------------

def _start_guard(path: str, period: float = 0.5) -> None:
    def snap():
        doc = {"pid": os.getpid(), "argv": sys.argv, "modules": sorted(sys.modules), "path": list(sys.path)}
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, path)

    def loop():
        while True:
            try:
                snap()
            except Exception:  # noqa: BLE001  (best effort; the check reads the last snapshot)
                pass
            time.sleep(period)

    snap()
    threading.Thread(target=loop, name="modules-guard", daemon=True).start()


def offenders(doc: dict) -> list[str]:
    """Probe/demo engine modules or source paths visible in a guard snapshot."""
    bad = [m for m in doc.get("modules", []) if m.split(".")[0] in FORBIDDEN]
    bad += [f"sys.path:{p}" for p in doc.get("path", []) if any(x in p for x in _FORBIDDEN_PATH_PARTS)]
    return bad


def clean_env(extra: dict | None = None) -> dict:
    """Environment for the engine: no PYTHONPATH (nothing but the installed flexengine2 + its deps)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({"PYTHONUNBUFFERED": "1", **(extra or {})})
    return env


class HostProc:
    """`flexengine run --config <cfg>` as a subprocess in its own process group."""

    def __init__(self, cfg_path: Path, log_path: Path, *, env: dict | None = None, cwd: Path | None = None,
                 python: str | None = None):
        self.cfg_path, self.log_path = Path(cfg_path), Path(log_path)
        self.modules_file = self.log_path.with_suffix(".modules.json")
        self.env = env
        self.cwd = Path(cwd) if cwd else self.log_path.parent
        self.python = python or sys.executable
        self.proc: subprocess.Popen | None = None
        self.snapshots: list[dict] = []

    @property
    def argv(self) -> list[str]:
        return [self.python, "-m", "flexengine2.testing.edge_lab", "run", "--config", str(self.cfg_path)]

    def start(self) -> "HostProc":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.modules_file.unlink(missing_ok=True)
        log = open(self.log_path, "ab")
        env = clean_env({**(self.env or {}), "FLEX_TEST_MODULES_FILE": str(self.modules_file)})
        if self.env and "PYTHONPATH" in self.env:
            env["PYTHONPATH"] = self.env["PYTHONPATH"]
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                     start_new_session=True)
        log.close()
        return self

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _snapshot(self):
        try:
            self.snapshots.append(json.loads(self.modules_file.read_text()))
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float = 15):
        """SIGTERM (graceful: the host loop ends, relays close), SIGKILL after `timeout`."""
        if self.alive():
            self._snapshot()
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(10)
        self.proc = None

    def kill(self):
        """SIGKILL (crash)."""
        if self.alive():
            self._snapshot()
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(10)
        self.proc = None

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    def check_clean(self) -> list[dict]:
        """Assert that every run of this host used flexengine2 only. Returns the snapshots checked."""
        self._snapshot()
        assert self.snapshots, f"no module snapshot from the engine subprocess ({self.modules_file}); log:\n" \
                               + self.log()[-2000:]
        for doc in self.snapshots:
            assert "flexengine2.local.cli" in doc["modules"], doc["argv"]
            bad = offenders(doc)
            assert not bad, f"engine subprocess loaded probe/demo engine code: {bad}"
        return self.snapshots


# ---- cloud fake admin client ---------------------------------------------------------------------------

def admin(port: int, method: str, path: str, body=None, user: str = "carol", token: str = "admin-secret",
          timeout: float = 20):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = json.dumps(body).encode() if body is not None else b""
    c.request(method, path, body=data, headers={"Authorization": f"Bearer {token}", "X-Flex-User": user,
                                                 "Content-Type": "application/json", "Connection": "close"})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (json.loads(out) if out and r.getheader("Content-Type", "").endswith("json") else out)


def wait(pred, timeout: float = 60, period: float = 0.1, what: str = "", log=None):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(period)
    tail = ""
    if log is not None:
        tail = "\n--- engine log tail ---\n" + (log() if callable(log) else str(log))[-4000:]
    raise AssertionError(f"timeout waiting for {what or pred}{tail}")


# ---- runner / relay CLI ----------------------------------------------------------------------------------

def _relay(a):
    from flexengine2.drivers.mcu.driver import LinkPool
    from flexengine2.gateway.relay import McuRelay

    host, port = a.smp.rsplit(":", 1)
    return McuRelay(LinkPool(host, int(port)), Path(a.model), a.deployment, a.device, Path(a.work),
                    zenoh_enabled=False)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("relay-inject", "relay-status"):
        from flexengine2.local.cli import main as cli

        if os.environ.get("FLEX_TEST_MODULES_FILE"):  # after the CLI import: every snapshot shows the entry point
            _start_guard(os.environ["FLEX_TEST_MODULES_FILE"])
        return cli(argv)
    ap = argparse.ArgumentParser(prog="flexengine2.testing.edge_lab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("relay-inject", "relay-status"):
        p = sub.add_parser(name)
        p.add_argument("--smp", required=True)
        p.add_argument("--model", required=True)
        p.add_argument("--deployment", default="d_pick_lab")
        p.add_argument("--device", default="portenta")
        p.add_argument("--work", required=True)
        if name == "relay-inject":
            p.add_argument("--instance", required=True)
            p.add_argument("--port", required=True)
            p.add_argument("--json", required=True)
    a = ap.parse_args(argv)
    r = _relay(a)
    if a.cmd == "relay-inject":
        from flexengine2.gateway.relay import find_port

        p = find_port(r.ports, a.instance, a.port)
        n = r.inject_raw(p.topic, r.codec.from_json(p.qname, json.loads(a.json)), f"{a.instance}.{a.port}")
        print(json.dumps({"instance": a.instance, "port": a.port, "type": p.qname, "topic": f"0x{p.topic:08x}",
                          "delivered": n}))
        return 0 if n else 1
    msgs = r.poll()
    for m in msgs:
        m["xcdr1"] = m["xcdr1"].hex()
    print(json.dumps(msgs, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

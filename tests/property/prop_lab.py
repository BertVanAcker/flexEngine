"""Subprocess lab of the EV-13 properties: real signed releases of the P2 native test app, the reference fake cloud
(flexengine2.testing.fakecloud, one per session, a fresh device per schedule), engines as real subprocesses with the
native process driver (prop_killrun.py = testing.single + deterministic SIGKILL points), NetShim link cuts.

Releases (module detection_logger, safety class none in the UC1 model; health gate 2 x 0.1 s, timeout 1.5 s so a
failing release costs ~2 s instead of the P2 lab's 8 s):
    1.0.0 1.1.0 1.4.0   good
    1.2.0               fails its health check (process alive, /healthz 503) -> rollback
    1.3.0               unsigned -> the whole manifest is rejected, the previous state keeps running
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from hypothesis import Phase, settings

from flexengine2.artefacts.oci import MT_PAYLOAD_NATIVE, Layout
from flexengine2.drivers.base import http_probe
from flexengine2.identity import crypto
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.testing.release import (REPO, build_native_payload, descriptor_from_module, model_safety,
                                         module_hashes, publish_release, sbom_stub, sign_release)
from flexengine2.util import read_json

import prop_oracle as O

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parents[1]
WORK = FLEXENGINE / "tests" / "work" / "property"
MODEL = REPO / "examples" / "models" / "uc1"
RUNNER = HERE / "prop_killrun.py"
MODULE = "detection_logger"
KIND = {"1.0.0": "good", "1.1.0": "good", "1.4.0": "good", "1.2.0": "bad-health", "1.3.0": "rejected"}
GOOD = ("1.0.0", "1.1.0", "1.4.0")
DEPS = ("dl-a", "dl-b")
PROFILE = os.environ.get("FLEX_PROPERTY_PROFILE", "push")


def budget(push: int, nightly: int, shrink: bool = True) -> settings:
    """max_examples of one property in the active tier (profile from FLEX_PROPERTY_PROFILE). Subprocess properties
    pass shrink=False: one schedule takes seconds, a failing one up to its 25 s convergence bound, so shrinking
    would take hours; the failing schedule is printed (and kept in the nightly example database) as it is."""
    kw = {} if shrink else {"phases": [Phase.explicit, Phase.reuse, Phase.generate]}
    return settings(max_examples=max(1, nightly if PROFILE == "nightly" else push), **kw)


def wait(pred, timeout: float, period: float = 0.05):
    end = time.time() + timeout
    while True:
        v = pred()
        if v or time.time() > end:
            return v
        time.sleep(period)


class Lab:
    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        kd = WORK / "keys"
        self.keys = {n: crypto.load_or_create(kd / f"{n}.key") for n in ("release", "cloud")}
        oci = WORK / "oci"
        shutil.rmtree(oci, ignore_errors=True)
        layout = Layout(oci)
        hashes, classes = module_hashes(MODEL), model_safety(MODEL)
        health = {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.1,
                  "successThreshold": 2, "timeoutSeconds": 1.5}
        restart = {"policy": "on-failure", "maxRetries": 5, "restartSec": 0.3}
        self.digest = {}
        for v, kind in KIND.items():
            repo = f"flexnodes/{MODULE}"
            desc = descriptor_from_module(MODEL, MODULE, v, profile="native", health=health, restart=restart,
                                          hashes=hashes, classes=classes)
            payload = build_native_payload(MODULE, v, fail_health=kind == "bad-health")
            m = publish_release(layout, repo, desc, [(MT_PAYLOAD_NATIVE, payload, {
                "dev.flex.profile": "native", "dev.flex.platform": "linux/any+python3"})],
                sbom_stub(MODULE, v, "native"))
            if kind != "rejected":
                sign_release(layout, self.keys["release"], repo, m, v, None)
            self.digest[v] = m["digest"]
        self.version_of = {d: v for v, d in self.digest.items()}
        cdir = self.tmp / "cloud"
        cdir.mkdir(parents=True)
        os.symlink(oci, cdir / "oci")
        self.srv = CloudServer(cdir, 0, "admin-secret", self.keys["cloud"], model_dirs=[MODEL]).start()
        self.url, self.port = self.srv.url, self.srv.port

    def close(self):
        self.srv.stop()

    # ---- northbound (admin) -------------------------------------------------------------------------
    def admin(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else b""
        c.request(method, path, body=data, headers={"Authorization": "Bearer admin-secret", "X-Flex-User": "carol",
                                                     "Content-Type": "application/json", "Connection": "close"})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out and r.getheader("Content-Type", "").endswith("json") else out)

    def token(self, dev: str) -> str:
        s, b = self.admin("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
        assert s == 201, b
        return b["token"]

    def publish(self, dev: str, spec: dict[str, str]) -> int:
        """Plan + apply {dep: version}; returns the new manifestVersion."""
        items = [{"id": d, "module": MODULE, "version": v, "driver": "native"} for d, v in sorted(spec.items())]
        s, p = self.admin("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items,
                                                          "allowUnknownDevice": True})
        assert s == 201, p
        s, b = self.admin("POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
        assert s == 200, b
        return b["manifestVersion"]

    def bundle(self, dev: str) -> bytes:
        s, b = self.admin("GET", f"/api/v1/admin/devices/{dev}/bundle")
        assert s == 200
        return b

    def device(self, dev: str) -> dict:
        return self.admin("GET", f"/api/v1/admin/devices/{dev}")[1]

    def storm(self, seconds: float):
        """503 + Retry-After on every device request for `seconds` (fake cloud fault injection)."""
        self.admin("POST", "/api/v1/admin/faults", {"unavailableSeconds": seconds})

    def last_seq(self, dev: str) -> int:
        return self.srv.state.s["lastSeq"].get(dev, 0)


def unit_procs(dev: str) -> dict[str, list[int]]:
    """Live processes carrying this device's FLEX_UNIT marker, per deployment (ground truth of the executor)."""
    out: dict[str, list[int]] = {}
    pre = f"FLEX_UNIT={dev}/".encode()
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/stat") as f:
                if f.read().split(") ")[-1].startswith("Z"):
                    continue
            env = Path(f"/proc/{p}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for e in env:
            if e.startswith(pre):
                out.setdefault(e[len(pre):].decode(), []).append(int(p))
    return out


class SubDev:
    """One device run as a real engine subprocess; restarts keep its data directory."""

    def __init__(self, lab: Lab, api_url: str | None = None, prefix: str = "p", **extra):
        self.lab = lab
        self.dev = f"{prefix}-{uuid.uuid4().hex[:8]}"
        self.dir = lab.tmp / self.dev
        self.dir.mkdir(parents=True)
        self.data = self.dir / "data"
        self.cfg = {"deviceId": self.dev, "apiUrl": api_url or lab.url, "dataDir": str(self.data),
                    "onboardingToken": lab.token(self.dev), "cloudPublicKey": crypto.public_b64(lab.keys["cloud"]),
                    "releasePublicKeys": [crypto.public_b64(lab.keys["release"])], "pollInterval": 0.1,
                    "backoff": [0.05, 0.5], "httpTimeout": 2, "healthEveryTicks": 1,
                    "drivers": {"native": True}, **extra}
        self.proc: subprocess.Popen | None = None
        self.crashes: list[dict] = []
        self.mv_seen: list[int] = []
        self.errs: list[str] = []
        self.incarnations = 0

    # ---- process control -----------------------------------------------------------------------------
    def start(self, kill: str | None = None, bundle: Path | None = None, **cfg):
        self.cfg.update(cfg)
        (self.dir / "engine.json").write_text(json.dumps(self.cfg))
        args = [sys.executable, str(RUNNER), "--config", str(self.dir / "engine.json")]
        if kill:
            args += ["--kill", kill]
        if bundle:
            args += ["--import-bundle", str(bundle)]
        self.incarnations += 1
        log = open(self.dir / "engine.log", "ab")
        self.proc = subprocess.Popen(args, cwd=self.dir, stdout=log, stderr=subprocess.STDOUT,
                                     env={**os.environ, "PYTHONUNBUFFERED": "1"})
        log.close()
        return self

    def onboard(self, timeout: float = 15):
        """Start unarmed until the onboarded state is saved, then stop gracefully (keeps the onboarding window out of
        the kill points: bug onboarding-not-idempotent, test_bug_kill_during_onboarding_bricks_device)."""
        self.start()
        ok = wait(lambda: self.state().get("onboarded"), timeout)
        self.stop(signal.SIGTERM)
        assert ok, f"device did not onboard\n{self.context()}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def started(self) -> bool:
        """This incarnation reached its run loop (`engine.start` is logged after a bundle import)."""
        try:
            return (self.dir / "engine.log").read_text().count('"event":"engine.start"') >= self.incarnations
        except OSError:
            return False

    def run_until(self, pred, timeout: float) -> str:
        """'killed' (the kill point fired), 'pred' (only once this incarnation is running) or 'timeout'."""
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return "killed"
            if self.started() and pred():
                return "pred"
            time.sleep(0.05)
        return "timeout"

    def stop(self, sig=signal.SIGKILL):
        if self.alive():
            self.proc.send_signal(sig)
            self.proc.wait(timeout=15)

    def after_kill(self, power_cut: bool = False, label: str = ""):
        """Take the crash record; with a power cut also stop every workload and drop unsynced history lines."""
        self.stop()
        if power_cut:
            for pids in unit_procs(self.dev).values():
                for pid in pids:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            wait(lambda: not unit_procs(self.dev), 5)
            h = self.data / "history.jsonl"
            if h.exists():
                lines = h.read_text().splitlines(keepends=True)
                h.write_text("".join(lines[: len(lines) // 2]))
        st = self.state()
        j = st.get("journal")
        acts, _ = O.read_actions(self.data / "actions.jsonl")
        self.crashes.append({"line": len(acts), "journal": (j["dep"], j["digest"]) if j else None,
                             "running": self.running(), "kill": label, "powerCut": power_cut})
        self.observe()

    def cleanup(self, **stats):
        self.stop()
        try:
            O.record_stats("subprocess", self.crashes, self.actions(), incarnations=self.incarnations, **stats)
        except OSError:
            pass
        for pids in unit_procs(self.dev).values():
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    # ---- observations ----------------------------------------------------------------------------------
    def state(self) -> dict:
        try:
            return read_json(self.data / "state.json", {}) or {}
        except ValueError:
            return {"_unreadable": True}

    def units(self) -> dict:
        try:
            return read_json(self.data / "native" / "units.json", {}) or {}
        except ValueError:
            return {}

    def running(self) -> dict[str, str]:
        procs = unit_procs(self.dev)
        return {d: u["digest"] for d, u in self.units().items() if u.get("pid") in procs.get(d, [])}

    def actions(self) -> list[dict]:
        return O.read_actions(self.data / "actions.jsonl")[0]

    def history(self) -> list[dict]:
        out = []
        for name in ("history.jsonl.1", "history.jsonl"):
            p = self.data / name
            if p.exists():
                for line in p.read_text().splitlines():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out

    def observe(self):
        st = self.state()
        if "_unreadable" in st:
            self.errs.append("state.json unreadable after a kill (atomic write contract broken)")
            return
        mv = st.get("manifestVersion", 0)
        if self.mv_seen and mv < self.mv_seen[-1]:
            self.errs.append(f"manifestVersion went down on disk: {self.mv_seen[-1]} -> {mv}")
        self.mv_seen.append(mv)
        self.errs += O.seq_strictly_increasing(st.get("outbox") or [])

    # ---- convergence -------------------------------------------------------------------------------------
    def expected(self, st: dict) -> dict[str, str] | None:
        """{dep: digest} the desired state asks for, or None when the accepted manifest was rejected (then the
        previous state must keep running) -- bad releases are checked separately (never left committed)."""
        doc = st.get("desired")
        if not doc or (st.get("rejected") or {}).get("manifestVersion") == doc["manifestVersion"]:
            return None
        return {d["metadata"]["id"]: d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]
                for d in doc["deployments"]}

    def converged(self, online: bool = True, cloud_mv: int | None = None,
                  known: frozenset = O.KNOWN_BUGS) -> list[str]:
        st = self.state()
        errs: list[str] = []
        if not st or "_unreadable" in st:
            return ["no readable state yet"]
        if cloud_mv is not None:
            O.check(st.get("manifestVersion") == cloud_mv, f"accepted v{st.get('manifestVersion')} != cloud v{cloud_mv}",
                    errs)
        applied = {d: r["wl"]["digest"] for d, r in st.get("applied", {}).items()}
        units = self.units()
        digests = {d: u["digest"] for d, u in units.items()}
        j = st.get("journal")
        if "stale-journal" in known:
            j = O.tolerate_stale(st, self.crashes, applied, digests, j)
        skip = set(units) - set(digests)            # deployments the stale-journal tolerance took out
        O.check(j is None, f"journal open: {j}", errs)
        v = self.lab.version_of.get
        O.check(digests == applied, f"executor {[(d, v(g)) for d, g in digests.items()]} != applied "
                f"{[(d, v(g)) for d, g in applied.items()]}", errs)
        procs = unit_procs(self.dev)
        for d, u in units.items():
            if d in skip:
                continue
            O.check(procs.get(d) == [u.get("pid")], f"{d}: live unit processes {procs.get(d)} != unit pid {u.get('pid')}",
                    errs)
            O.check(bool(u.get("port")) and http_probe("127.0.0.1", u["port"], "/healthz"), f"{d}: not healthy", errs)
        for d in procs:
            O.check(d in units, f"orphan workload process for {d}: {procs[d]}", errs)
        want = self.expected(st)
        if want is not None:
            for d, g in want.items():
                if d in skip or (d not in applied and d in O.stale_journals(st, self.crashes) and "stale-journal" in known):
                    continue
                if KIND[v(g)] == "good":
                    O.check(applied.get(d) == g, f"{d}: desired {v(g)} not applied", errs)
                else:
                    O.check(applied.get(d) != g, f"{d}: bad release {v(g)} committed", errs)
            for d in applied:
                O.check(d in want, f"{d}: applied but absent from the desired state", errs)
        if online:
            O.check(not st.get("outbox"), f"outbox not drained ({len(st.get('outbox') or [])})", errs)
            if not errs:
                errs += self.delivery_errors(st, applied, skip)
        else:   # never had a link: the outbox holds every report since the device started
            rep = {d: g for d, g in O.committed_from_reports(st.get("outbox") or []).items() if d not in skip}
            O.check(rep == applied, f"offline outbox reports {rep} != applied {applied}", errs)
        return errs

    def delivery_errors(self, st: dict, applied: dict, skip=()) -> list[str]:
        """Cloud side: every report the device persisted reached flexCloud once (seq dedup), in seq order, and the
        cloud's view of what runs equals the device's."""
        errs: list[str] = []
        log = self.lab.device(self.dev)["statusLog"]
        seqs = [r["seq"] for r in log]
        O.check(all(b > a for a, b in zip(seqs, seqs[1:])), f"cloud statusLog seq not increasing: {seqs}", errs)
        O.check(self.lab.last_seq(self.dev) == st.get("seq"),
                f"cloud lastSeq {self.lab.last_seq(self.dev)} != device seq {st.get('seq')}", errs)
        by_seq = {r["seq"]: r for r in log}
        events = {e["seq"] for e in self.lab.srv.state.s.get("events", []) if e.get("deviceId") == self.dev}
        for h in self.history():
            if h.get("kind") != "reported" or "seq" not in h:
                continue
            if h.get("dep") is None:
                O.check(h["seq"] in events, f"device event seq {h['seq']} never delivered", errs)
                continue
            r = by_seq.get(h["seq"])
            O.check(r is not None and r.get("state") == h["state"] and r.get("deploymentId") == h["dep"],
                    f"report seq {h['seq']} ({h['dep']} {h['state']}) not delivered as such: {r and r.get('state')}",
                    errs)
        committed = {d: g for d, g in O.committed_from_reports(log).items() if d not in skip}
        O.check(committed == applied, f"cloud view {committed} != device {applied}", errs)
        return errs

    def settle(self, timeout: float = 25, online: bool = True, cloud_mv: int | None = None,
               known: frozenset = O.KNOWN_BUGS) -> list[str]:
        """Run undisturbed until converged (twice in a row, 0.3 s apart) or `timeout`."""
        errs: list[str] = ["not checked"]
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return [f"engine exited with {self.proc.returncode}"]
            errs = self.converged(online, cloud_mv, known)
            if not errs:
                time.sleep(0.3)
                errs = self.converged(online, cloud_mv, known)
                if not errs:
                    return []
            time.sleep(0.1)
        return [f"not converged within {timeout:g} s: {e}" for e in errs]

    def final_errors(self, known: frozenset = O.KNOWN_BUGS) -> list[str]:
        self.observe()
        errs = list(self.errs)
        acts, bad = O.read_actions(self.data / "actions.jsonl")
        errs += [f"unparseable action record: {b[:80]!r}" for b in bad]
        errs += O.duplicate_applies(acts, self.crashes, strict="reapply-after-rollback-crash" not in known)
        return errs

    def context(self) -> str:
        acts = [(a["action"], a["dep"], a.get("version")) for a in self.actions()]
        tail = (self.dir / "engine.log").read_text()[-3000:] if (self.dir / "engine.log").exists() else ""
        return f"device={self.dev}\ncrashes={self.crashes}\nactions={acts}\nengine.log tail:\n{tail}"

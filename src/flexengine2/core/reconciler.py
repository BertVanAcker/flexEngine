"""flexEngineV2 reconciler: one device engine (from probe P2, with the P3 gateway roles and the D1 PLC hold merged).

    loop:  flush status outbox -> pull desired state (ETag) -> reconcile -> supervise -> sleep/backoff

Reconcile is level-triggered: it compares the last *accepted* desired state (persisted, so it also
works offline and after a restart) with what the drivers observe, and converges:

  1. prepare  download + verify every changed release (all-or-nothing per manifest: one bad digest or
              signature keeps the whole previous state, as Margo requires), pick the payload layer for the
              driver's platform, run the driver's preflight (refusal = rejected like a bad signature)
  2. remove   deployments absent from the manifest (absence = removal); safety-gated like installs
  3. install  per changed deployment: activation gate (safe state / window) -> driver.apply ->
              health gate (fail-fast via driver.failed) -> driver.commit -> commit (keep previous as rollback
              target) or automatic rollback
  4. drift    a committed workload that disappeared is re-created; health changes are reported

Every state transition is reported to flexCloud through a persisted outbox with a per-device
sequence number, so reports survive network cuts and engine restarts and are deduplicated upstream.
"""

from __future__ import annotations

import json
import os
import platform
import random
import signal
import threading
import time
from pathlib import Path

from flexengine2.identity import crypto
from flexengine2.util import atomic_write_json, iso, read_json
from flexengine2.artefacts.store import ArtefactStore, VerificationError
from flexengine2 import __version__
from flexengine2.drivers.base import SPI_VERSION, Driver, DriverError, Workload, wait_until
from flexengine2.cloud.client import CloudClient, Offline, Rejected


class Backoff:
    def __init__(self, base: float, cap: float):
        self.base, self.cap, self.n = base, cap, 0

    def fail(self, retry_after: float | None = None) -> float:
        self.n += 1
        d = random.uniform(self.base, min(self.cap, self.base * 2 ** self.n))  # "full jitter"
        return max(d, retry_after or 0.0)

    def reset(self):
        self.n = 0


class SafeState:
    """UC1 activation predicates. On the machine these come from ISOBUS/CAN or the PLC (via ADS);
    here they are read from a JSON file that the test flips: {"machine_parked": bool, "operator_confirmed": bool}."""

    def __init__(self, path: Path | None):
        self.path = path

    def signals(self) -> dict:
        return read_json(self.path, {}) if self.path else {}

    def check(self, requires: list[str], window: dict | None = None) -> tuple[bool, str]:
        sig = self.signals()
        missing = [r for r in requires if not sig.get(r)]
        if missing:
            return False, "awaiting safe state: " + ", ".join(missing)
        if window:
            hm = time.strftime("%H:%M", time.gmtime())
            s, e = window["start"], window["end"]
            inside = s <= hm < e if s <= e else (hm >= s or hm < e)
            if not inside:
                return False, f"outside activation window {s}-{e} UTC"
        return True, ""


class Engine:
    def __init__(self, cfg: dict, drivers: dict[str, Driver] | None = None, role: str = "standalone-device",
                 parent: str | None = None, children: list[dict] | None = None, mcu: dict | None = None):
        self.cfg = cfg
        self.role, self.parent, self.children, self.mcu = role, parent, children or [], mcu or {}
        self.device = cfg["deviceId"]
        self.dir = Path(cfg["dataDir"])
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = crypto.load_or_create(self.dir / "device.key")
        self.client = CloudClient(cfg["apiUrl"], self.device, self.key, timeout=cfg.get("httpTimeout", 5))
        self.store = ArtefactStore(self.dir, self.client, cfg["releasePublicKeys"])
        self.cloud_pub = cfg["cloudPublicKey"]
        self.safe = SafeState(Path(cfg["safeStateFile"]) if cfg.get("safeStateFile") else None)
        self.poll = float(cfg.get("pollInterval", 1.0))
        self.backoff = Backoff(*cfg.get("backoff", [0.25, 4.0]))
        self.state_path = self.dir / "state.json"
        self.state = read_json(self.state_path) or {
            "deviceId": self.device, "onboarded": False, "etag": None, "manifestVersion": 0,
            "desired": None, "applied": {}, "failed": {}, "rejected": None, "outbox": [], "seq": 0,
            "notices": {}, "journal": None, "health": {}}
        self.drivers = drivers if drivers is not None else build_drivers(cfg, self.dir)
        self.actions_path = self.dir / "actions.jsonl"
        self._stop = threading.Event()
        self.ticks = 0

    # ---- persistence / logging -------------------------------------------------------------
    def save(self):
        atomic_write_json(self.state_path, self.state)

    def action(self, kind: str, dep: str, **kw):
        rec = {"t": time.time(), "action": kind, "dep": dep, **kw}
        with open(self.actions_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.log(f"{kind} {dep} {kw.get('version', '')}")

    def log(self, msg: str):
        print(f"{iso()} [{self.device}] {msg}", flush=True)

    def report(self, dep: str, state: str, **kw):
        if state == "Failed" and str(kw.get("reason", "")).startswith("artefact rejected"):
            # (from P3) after a rejected manifest, a desired state equal to what runs must be re-reported as
            # Installed; leave a notice so reconcile's "desired went back to what is running" path does it
            self.state["notices"][dep] = "rejected"
        self.state["seq"] += 1
        self.state["outbox"].append({"dep": dep, "seq": self.state["seq"], "state": state, "deviceTime": time.time(),
                                     "manifestVersion": (self.state["desired"] or {}).get("manifestVersion"), **kw})
        self.save()

    def notice(self, dep: str, key: str, state: str, **kw):
        """Report once per (dep, key): avoids re-sending 'Pending' every tick."""
        if self.state["notices"].get(dep) != key:
            self.state["notices"][dep] = key
            self.report(dep, state, **kw)

    # ---- cloud I/O -----------------------------------------------------------------------------
    def onboard(self):
        if self.state["onboarded"]:
            return
        _, _, data = self.client.post_json("/api/v1/onboarding", {
            "deviceId": self.device, "publicKey": crypto.public_b64(self.key),
            "token": self.cfg["onboardingToken"]}, signed=False)
        resp = json.loads(data)
        if resp["cloudPublicKey"] != self.cloud_pub:
            raise RuntimeError("cloud key at onboarding does not match the provisioned trust anchor")
        self.state["onboarded"] = True
        self.save()
        self.log("onboarded")

    def capabilities(self) -> dict:
        mem = 0
        try:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            pass
        drivers = {p: d.capabilities() for p, d in self.drivers.items()}
        doc = {
            "apiVersion": "device.margo.org/v1alpha1", "kind": "DeviceCapabilities",
            "properties": {
                "id": self.device, "vendor": "flex", "modelNumber": platform.node() or "host", "serialNumber": self.device,
                "roles": ["standalone-device"],
                "resources": {"cpu": {"cores": os.cpu_count(), "architecture": platform.machine()},
                              "memory": f"{mem // (1 << 20)}Mi"},
                "peripherals": [], "interfaces": [{"type": "ethernet"}],
                "otelCollector": False,
                "supportedRuntimes": ["oci"] if "container" in drivers else [],
                "supportedDeploymentTypes": ["custom"],
            },
            "x-flex": {"drivers": drivers, "safeStateSignals": sorted(self.safe.signals().keys()),
                       "engine": {"impl": "flexengine2", "version": __version__, "spi": SPI_VERSION}},
        }
        p = doc["properties"]
        p["roles"] = [self.role]
        if self.role == "child-device":
            p["modelNumber"] = self.mcu.get("board", "mcu")
            p["resources"] = {"cpu": {"architecture": self.mcu.get("arch", "?"), "cores": 1}}
            p["interfaces"] = [{"type": "uart", "protocol": "smp"}]
            doc["x-flex"]["parent"] = {"deviceId": self.parent, "mode": "see-thru"}
            doc["x-flex"]["engine"]["onDevice"] = "flexEngine-MCU base image"
        if self.children:
            doc["x-flex"]["children"] = self.children
        return doc

    def flush_outbox(self):
        while self.state["outbox"]:
            r = self.state["outbox"][0]
            self.client.post_json(f"/api/v1/devices/{self.device}/deployments/{r['dep']}/status", r)
            self.state["outbox"].pop(0)
            self.save()

    def pull(self) -> bool:
        h = {"If-None-Match": self.state["etag"]} if self.state["etag"] else {}
        status, hdrs, body = self.client.request("GET", f"/api/v1/devices/{self.device}/deployments", headers=h)
        if status == 304:
            return False
        return self.accept_manifest(body, hdrs.get("x-flex-manifest-signature", ""), hdrs.get("etag"))

    def accept_manifest(self, body: bytes, sig: str, etag: str | None) -> bool:
        if not crypto.verify(self.cloud_pub, body, sig):
            self.log("desired state with bad/missing cloud signature ignored")
            return False
        doc = json.loads(body)
        if doc.get("deviceId") != self.device:
            self.log("desired state for another device ignored")
            return False
        mv = int(doc["manifestVersion"])
        if mv < self.state["manifestVersion"] or (mv == self.state["manifestVersion"] and self.state["desired"]
                                                  and self.state["desired"] != doc):
            self.log(f"manifestVersion {mv} <= accepted {self.state['manifestVersion']}: rollback attack? ignored")
            self.state.setdefault("downgradesRefused", 0)
            self.state["downgradesRefused"] += 1
            self.save()
            return False
        changed = self.state["desired"] != doc
        self.state.update(desired=doc, manifestVersion=mv, etag=etag)
        self.save()
        if changed:
            self.log(f"accepted desired state v{mv} ({len(doc['deployments'])} deployments)")
        return changed

    # ---- reconcile -------------------------------------------------------------------------------
    @staticmethod
    def _rev(d: dict) -> dict:
        return d["spec"]["deploymentProfile"]["components"][0]["properties"]

    def _gate(self, dep: dict) -> tuple[bool, str]:
        """Activation gate: `plc:<dep>[@<hash>]` holds (D1a: Pending until the plc-verify driver has verified that PLC
        deployment), then the safe-state signals (FE-S6, implicit for the safety class) and the time window."""
        act = dep.get("x-flex-activation") or {}
        req = list(act.get("requires", []))
        plc = [r for r in req if r.startswith("plc:")]
        if plc:
            drv = self.drivers.get("plc-verify")
            missing = []
            for r in plc:
                name, _, want = r[4:].partition("@")
                if drv is None or not drv.verified(name, want or None):
                    missing.append(r)
            if missing:
                return False, "held: PLC not verified: " + ", ".join(missing)
            req = [r for r in req if not r.startswith("plc:")]
        if dep.get("x-flex-safetyClass", "none") != "none":  # FE-S6 interlock is implicit for safety class
            req = sorted(set(req) | {"machine_parked", "operator_confirmed"})
        return self.safe.check(req, act.get("window"))

    def _workload(self, dep: dict, rel) -> Workload:
        rv = self._rev(dep)
        return Workload(dep_id=dep["metadata"]["id"], module=dep["spec"]["applicationId"], version=rv["revision"],
                        digest=rv["digest"], payload_path=rel.payload_path,
                        payload_annotations=rel.payload_annotations, parameters=dep["spec"].get("parameters", {}),
                        health=rel.descriptor.get("health", {}), restart=rel.descriptor.get("restartPolicy", {}),
                        run_options=rel.descriptor.get("runOptions") or {})

    def health_gate(self, drv: Driver, dep_id: str, probe: dict, rollback: bool = False) -> tuple[bool, float]:
        """N consecutive successful probes within the timeout. Fails fast (P4 amendment) when the driver
        reports a terminal failure (`Driver.failed`); the reason is left in `self.gate_failure`.
        `minSeconds` (D1 gap 2, MCU confirm window): a forward install is committed only after it has been observed
        healthy for that long (faults still end the gate at once); not for a rollback to the previous version."""
        t0 = time.time()
        self.gate_failure = None
        need, ok = int(probe.get("successThreshold", 1)), 0
        period, timeout = float(probe.get("periodSeconds", 0.25)), float(probe.get("timeoutSeconds", 10))
        soak = 0.0 if rollback else float(probe.get("minSeconds", 0) or 0)
        while time.time() - t0 < timeout:
            if drv.health(dep_id, probe):
                ok += 1
            else:
                ok = 0
                why = drv.failed(dep_id)
                if why:
                    self.gate_failure = why
                    return False, time.time() - t0
            if ok >= need and time.time() - t0 >= soak:
                commit = getattr(drv, "commit", None)
                if commit is not None and getattr(type(drv), "commit", None) is not Driver.commit:
                    try:
                        commit(dep_id)  # two-phase executors confirm only after a passed gate (P3 amendment)
                    except DriverError as e:
                        self.gate_failure = f"commit failed: {e}"
                        self.log(self.gate_failure)
                        return False, time.time() - t0
                    self.action("commit", dep_id)
                return True, time.time() - t0
            time.sleep(period)
        self.gate_failure = f"timeout after {timeout:g} s"
        return False, time.time() - t0

    def reconcile(self):
        desired_doc = self.state["desired"]
        if not desired_doc:
            return
        mv = desired_doc["manifestVersion"]
        if (self.state.get("rejected") or {}).get("manifestVersion") == mv:
            return  # keep previous state until a new manifest arrives
        desired = {d["metadata"]["id"]: d for d in desired_doc["deployments"]}
        applied = self.state["applied"]

        # 1. prepare (all-or-nothing)
        targets = {}
        for dep_id, d in desired.items():
            rv = self._rev(d)
            cur = applied.get(dep_id)
            if cur and cur["wl"]["digest"] == rv["digest"] and cur["wl"]["parameters"] == d["spec"].get("parameters", {}):
                if self.state["failed"].pop(dep_id, None) or self.state["notices"].pop(dep_id, None):
                    # desired went back to what is running (e.g. operator reverted a failed release)
                    self.report(dep_id, "Installed", version=cur["wl"]["version"], digest=cur["wl"]["digest"],
                                health="healthy" if self.state["health"].get(dep_id) else "unhealthy")
                continue
            if self.state["failed"].get(dep_id) == rv["digest"]:
                continue
            if d["x-flex-driver"] not in self.drivers:
                self.state["rejected"] = {"manifestVersion": mv, "reason": f"no driver for {d['x-flex-driver']}"}
                self.report(dep_id, "Failed", reason=self.state["rejected"]["reason"], version=rv["revision"])
                return
            repo = rv["repository"].removeprefix("oci://").split("/", 1)[1]
            drv = self.drivers[d["x-flex-driver"]]
            try:
                plat = drv.platform() if hasattr(drv, "platform") else None
            except DriverError as e:
                raise Offline(f"{d['x-flex-driver']} executor unreachable: {e}") from e  # defer, do not reject
            try:
                rel = self.store.verify_release(repo, rv["digest"], d["x-flex-driver"], d.get("x-flex-safetyClass", "none"),
                                                platform=plat)
            except Offline as e:
                self.log(f"prepare deferred (offline): {e}")
                raise
            except (VerificationError, Rejected, ValueError, KeyError) as e:
                self.state["rejected"] = {"manifestVersion": mv, "reason": str(e), "deployment": dep_id}
                self.action("reject", dep_id, version=rv["revision"], digest=rv["digest"], reason=str(e))
                self.report(dep_id, "Failed", reason=f"artefact rejected: {e}", version=rv["revision"],
                            digest=rv["digest"], keptPrevious=bool(applied))
                return
            # driver preflight (P3 amendment): checks before anything reaches the executor
            try:
                reason = drv.preflight(rel, d) if hasattr(drv, "preflight") else None
            except DriverError as e:
                raise Offline(f"{d['x-flex-driver']} preflight deferred: {e}") from e
            if reason:
                self.state["rejected"] = {"manifestVersion": mv, "reason": reason, "deployment": dep_id}
                self.action("reject", dep_id, version=rv["revision"], digest=rv["digest"], reason=reason)
                self.report(dep_id, "Failed", reason=f"artefact rejected: {reason}", version=rv["revision"],
                            digest=rv["digest"], keptPrevious=bool(applied), stage="preflight")
                return
            targets[dep_id] = (d, rel)

        # 2. removals (absence = removal)
        for dep_id in [k for k in applied if k not in desired]:
            cur = applied[dep_id]
            ok, why = self._gate(cur["dep"])
            if not ok:
                self.notice(dep_id, f"pending-remove:{why}", "Pending", reason=why, operation="remove")
                continue
            self.report(dep_id, "Removing", version=cur["wl"]["version"])
            self.action("remove", dep_id, version=cur["wl"]["version"], digest=cur["wl"]["digest"])
            self.drivers[cur["dep"]["x-flex-driver"]].remove(dep_id)
            del applied[dep_id]
            self.state["notices"].pop(dep_id, None)
            self.report(dep_id, "Removed", version=cur["wl"]["version"])

        # 3. installs / updates
        for dep_id, (d, rel) in targets.items():
            ok, why = self._gate(d)
            rv = self._rev(d)
            if not ok:
                self.notice(dep_id, f"pending:{rv['digest']}:{why}", "Pending", reason=why, version=rv["revision"])
                continue
            self.install(dep_id, d, rel)

    def install(self, dep_id: str, d: dict, rel):
        applied = self.state["applied"]
        drv = self.drivers[d["x-flex-driver"]]
        wl = self._workload(d, rel)
        cur = applied.get(dep_id)
        cur_wl = Workload.from_dict(cur["wl"]) if cur else None
        self.state["journal"] = {"dep": dep_id, "digest": wl.digest, "phase": "applying", "at": time.time()}
        self.save()
        obs = drv.status(dep_id)
        t0 = time.time()
        apply_error = None
        if obs.present and obs.digest == wl.digest and obs.running:
            self.action("adopt", dep_id, version=wl.version, digest=wl.digest)  # crash-resume: no second apply
        else:
            self.report(dep_id, "Installing", version=wl.version, digest=wl.digest,
                        fromVersion=cur_wl.version if cur_wl else None, plan=drv.plan(cur_wl, wl))
            self.action("apply", dep_id, version=wl.version, digest=wl.digest)
            try:
                drv.apply(cur_wl, wl)
            except DriverError as e:
                apply_error = str(e)
                self.log(f"apply failed: {e}")
        if apply_error is not None:
            # fail fast (P4 amendment): a refused/failed apply never enters the health gate
            healthy, dt, why = False, 0.0, f"apply failed: {apply_error}"
        else:
            healthy, dt = self.health_gate(drv, dep_id, wl.health)
            why = getattr(self, "gate_failure", None)
        if healthy:
            applied[dep_id] = {"dep": d, "wl": wl.to_dict(), "committedAt": time.time(),
                               "previous": cur["wl"] if cur else None}
            self.state["failed"].pop(dep_id, None)
            self.state["notices"].pop(dep_id, None)
            self.state["journal"] = None
            self.state["health"][dep_id] = True
            self.report(dep_id, "Installed", version=wl.version, digest=wl.digest, health="healthy",
                        interfaceHash=rel.descriptor.get("interfaceHash"), applySeconds=round(time.time() - t0, 3), healthGateSeconds=round(dt, 3))
            return
        # apply or health gate failed -> automatic rollback
        self.state["failed"][dep_id] = wl.digest
        reason = "apply failed" if apply_error is not None else "health check failed"
        detail = {"failure": why, "failFast": bool(why) and not str(why).startswith("timeout")}
        if cur_wl:
            self.action("rollback", dep_id, version=cur_wl.version, digest=cur_wl.digest, failed=wl.version)
            try:
                drv.apply(wl, cur_wl)
                back, _ = self.health_gate(drv, dep_id, cur_wl.health, rollback=True)
            except DriverError as e:
                back = False
                self.log(f"rollback failed: {e}")
            self.state["journal"] = None
            fd = getattr(drv, "failure_detail", None)   # e.g. when the fault became visible on the device
            extra = (fd(dep_id, time.time()) or {}) if fd and back else {}
            self.report(dep_id, "Failed", version=wl.version, digest=wl.digest, reason=reason,
                        rolledBackTo=cur_wl.version, rollbackHealthy=back, **detail,
                        detectSeconds=round(dt, 3), totalSeconds=round(time.time() - t0, 3), **extra)
        else:
            self.action("remove", dep_id, version=wl.version, digest=wl.digest, why="failed first install")
            drv.remove(dep_id)
            self.state["journal"] = None
            self.report(dep_id, "Failed", version=wl.version, digest=wl.digest, reason=reason,
                        rolledBackTo=None, **detail, detectSeconds=round(dt, 3))

    # ---- supervision / drift ------------------------------------------------------------------
    def supervise(self):
        for drv in self.drivers.values():
            for ev in drv.supervise():
                self.log(f"supervision: {ev}")
                if ev["event"] in ("restarted", "start-limit-hit"):
                    self.report(ev["dep"], "Installed" if ev["event"] == "restarted" else "Failed",
                                reason=f"supervisor: {ev['event']}", restarts=ev.get("restarts"))
        check_health = self.ticks % int(self.cfg.get("healthEveryTicks", 3)) == 0
        for dep_id, rec in list(self.state["applied"].items()):
            drv = self.drivers[rec["dep"]["x-flex-driver"]]
            obs = drv.status(dep_id)
            wl = Workload.from_dict(rec["wl"])
            if not obs.present:
                self.action("repair", dep_id, version=wl.version, digest=wl.digest)
                drv.apply(None, wl)
                self.report(dep_id, "Installed", version=wl.version, reason="drift repaired (workload was missing)")
                continue
            if check_health:
                h = drv.health(dep_id, wl.health)
                if h != self.state["health"].get(dep_id):
                    self.state["health"][dep_id] = h
                    self.report(dep_id, "Installed", version=wl.version, health="healthy" if h else "unhealthy",
                                restarts=obs.restarts)

    # ---- main loop ---------------------------------------------------------------------------
    def tick(self) -> float:
        self.ticks += 1
        delay = self.poll
        try:
            self.onboard()
            if not self.state.get("capsSent"):
                self.client.post_json(f"/api/v1/devices/{self.device}/capabilities", self.capabilities(), method="PUT")
                self.state["capsSent"] = True
                self.save()
            self.flush_outbox()
            self.pull()
            self.backoff.reset()
        except Offline as e:
            delay = self.backoff.fail(e.retry_after)
            self.log(f"offline ({e}); retry in {delay:.2f}s")
        try:
            self.reconcile()
            self.flush_outbox()
        except Offline as e:
            delay = self.backoff.fail(e.retry_after)
        self.supervise()
        return delay

    def run(self):
        signal.signal(signal.SIGTERM, lambda *a: self._stop.set())
        self.log(f"engine start: manifestVersion={self.state['manifestVersion']} applied={sorted(self.state['applied'])} "
                 f"journal={self.state.get('journal')}")
        while not self._stop.is_set():
            delay = self.tick()
            self._stop.wait(delay)
        self.log("engine stop")

    def import_bundle(self, path: str):
        desired, sig = self.store.import_bundle(Path(path).read_bytes())
        if self.accept_manifest(desired, sig, None):
            self.log("desired state imported from offline bundle")


def build_drivers(cfg: dict, data_dir: Path) -> dict[str, Driver]:
    from flexengine2.drivers.container import AnkaiosDriver, CliContainerDriver
    from flexengine2.drivers.process import ProcessDriver

    out: dict[str, Driver] = {}
    dc = cfg.get("drivers", {}).get("container")
    if dc:
        if dc["runtime"] == "ankaios":
            out["container"] = AnkaiosDriver(cfg["deviceId"], dc["ankBin"], dc["serverUrl"], dc["agent"])
        else:
            out["container"] = CliContainerDriver(cfg["deviceId"], dc["runtime"], dc.get("allowedRunOptions"))
    if cfg.get("drivers", {}).get("native", True):
        out["native"] = ProcessDriver(cfg["deviceId"], data_dir)
    return out

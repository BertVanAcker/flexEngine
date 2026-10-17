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

Hardening (EV-05): `tick()` never raises. Every phase (sync = onboard/capabilities/outbox/pull, reconcile, supervise)
catches what a driver or the reconciler throws, logs it with type + phase + deployment, counts it (`metrics()`) and
backs the device off (1 s .. 10 s); `Offline` keeps its cloud back-off. state.json carries `schemaVersion` (v0 files
are migrated, corrupt ones refused: core/state.py); the outbox is bounded (`outboxMax`, core/outbox.py). Logs are
structured (obs/logging.py), every transition also goes to the local history (obs/history.py), and with OTel on the
engine emits its own spans (obs/tracing.py). The engine is driven by one worker thread (host.py); other threads talk
to it only through `submit()` (runs a callable on the worker between ticks), `metrics()` and `request_stop()`.
"""

from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import json
import os
import platform
import queue
import random
import signal
import threading
import time
import traceback
from pathlib import Path

from flexengine2.identity import crypto, keys, secrets
from flexengine2.util import read_json, repair_jsonl_tail
from flexengine2.artefacts.store import ArtefactStore, VerificationError
from flexengine2 import __version__
from flexengine2.drivers.base import SPI_VERSION, Driver, DriverError, Workload, wait_until
from flexengine2.cloud.client import CloudClient, Offline, Rejected, TlsError, from_cfg_kwargs
from flexengine2 import contracts
from flexengine2.core import outbox as outbox_policy
from flexengine2.core import state as state_file
from flexengine2.core.state import StateCorrupt, StateError, StateTooNew  # noqa: F401  (re-exported)
from flexengine2.obs import history as history_mod
from flexengine2.obs import logging as flog
from flexengine2.obs import tracing

OUTBOX_MAX = 1000
ERROR_BACKOFF = (1.0, 10.0)


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
        self._log = flog.DeviceLog(self.device)
        self.state_path = self.dir / "state.json"
        self.state = state_file.load(self.state_path, self.device)  # StateCorrupt / StateTooNew: device not run
        self.key = keys.load_or_create_device_key(self.dir / "device.key",
                                                  fix_permissions=bool(cfg.get("fixPermissions")))
        self.client = CloudClient(cfg["apiUrl"], self.device, self.key, timeout=cfg.get("httpTimeout", 5),
                                  **from_cfg_kwargs(cfg))
        self.store = ArtefactStore(self.dir, self.client, cfg["releasePublicKeys"])
        self.cloud_pub = cfg["cloudPublicKey"]
        self.safe = SafeState(Path(cfg["safeStateFile"]) if cfg.get("safeStateFile") else None)
        self.poll = float(cfg.get("pollInterval", 1.0))
        self.backoff = Backoff(*cfg.get("backoff", [0.25, 4.0]))
        self.err_backoff = Backoff(*ERROR_BACKOFF)
        self.outbox_max = max(1, int(cfg.get("outboxMax", OUTBOX_MAX)))
        self._backpressure = False
        self.drivers = drivers if drivers is not None else build_drivers(cfg, self.dir)
        self.actions_path = self.dir / "actions.jsonl"
        repair_jsonl_tail(self.actions_path)   # a power cut inside the last append leaves a torn line (EV-13)
        self.history = history_mod.History(self.dir, self.device, int(cfg.get("historyMax", history_mod.DEFAULT_MAX)))
        self._hist_seen: dict[str, set] = {}
        if not tracing.enabled() and cfg.get("otel"):
            tracing.configure(cfg.get("otel"))
        self.tracer = tracing.EngineTracer(self.device)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._calls: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None   # set by the host while a worker thread drives this engine
        self._dep: str | None = None                  # deployment being worked on (for error records)
        self.ticks = 0
        self._m = {"reconcileSeconds": _stat(), "tickSeconds": _stat(), "gateSeconds": _stat(), "outboxDropped": 0,
                   "errors": {}, "errorsByType": {}, "lastError": None, "lastTickAt": None, "startedAt": time.time()}

    # ---- persistence / logging -------------------------------------------------------------
    def save(self):
        state_file.save(self.state_path, self.state)

    def action(self, kind: str, dep: str, **kw):
        rec = {"t": time.time(), "action": kind, "dep": dep, **kw}
        with open(self.actions_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        extra = {k: v for k, v in kw.items() if k != "version"}
        self.log(f"{kind} {dep} {kw.get('version', '')}", event=f"action.{kind}", dep=dep, version=kw.get("version"),
                 **extra)

    def log(self, msg: str, level: str = "info", event: str | None = None, **kv):
        """One structured record (obs/logging.py; `logFormat` json | text)."""
        self._log(msg, level, event, **kv)

    def _trace_fields(self) -> dict:
        tp = self.tracer.current_traceparent() or self._mv_traceparent()
        return {"traceparent": tp, "correlationId": tp.split("-")[1]} if tp else {}

    def _mv_traceparent(self, mv=None) -> str | None:
        mv = (self.state.get("desired") or {}).get("manifestVersion") if mv is None else mv
        return (self.state.get("traceparents") or {}).get(str(mv))

    def _hist(self, dep: str | None, state: str, kind: str = "internal", once=None, **kw):
        """Local history (plan §3.3: the internal states PREPARING, GATE, TESTING, COMMITTING, ROLLING_BACK, REPAIR
        too). An internal state with a `once` key is recorded once per (state, key) until the deployment's next
        terminal report (a deployment waiting at the gate does not fill the ring every tick)."""
        seen = self._hist_seen.setdefault(dep or "", set())
        if once is not None:
            if (state, once) in seen:
                return
            seen.add((state, once))
        if kind == "reported" and state in ("Installed", "Failed", "Removed"):
            seen.clear()
        self.history.record(state, dep=dep, kind=kind, **kw)

    def report(self, dep: str, state: str, **kw):
        if state == "Failed" and str(kw.get("reason", "")).startswith("artefact rejected"):
            # (from P3) after a rejected manifest, a desired state equal to what runs must be re-reported as
            # Installed; leave a notice so reconcile's "desired went back to what is running" path does it
            self.state["notices"][dep] = "rejected"
        for k, v in self._trace_fields().items():
            kw.setdefault(k, v)
        if kw.get("reason") and "reasonCode" not in kw:
            code = reason_code_of(kw["reason"])
            if code:
                kw["reasonCode"] = code
        self.state["seq"] += 1
        mv = (self.state["desired"] or {}).get("manifestVersion")
        self.state["outbox"].append({"dep": dep, "seq": self.state["seq"], "state": state, "deviceTime": time.time(),
                                     "manifestVersion": mv, **kw})
        self._bound_outbox()
        self.save()
        self._hist(dep, state, "reported", seq=self.state["seq"], manifestVersion=mv, version=kw.get("version"),
                   reason=kw.get("reason"), health=kw.get("health"), correlationId=kw.get("correlationId"))

    def _bound_outbox(self):
        ob = self.state["outbox"]
        if len(ob) > self.outbox_max:
            kept, dropped = outbox_policy.compact(ob, self.outbox_max)
            self.state["outbox"] = kept
            self._m["outboxDropped"] += dropped
            if not self._backpressure:
                self._backpressure = True
                self.log(f"status outbox over {self.outbox_max} reports (cloud unreachable?): dropping superseded "
                         f"reports ({dropped} now, newest per deployment kept; length {len(kept)})", "warning",
                         "outbox.backpressure", outboxLength=len(kept), dropped=dropped)
        elif self._backpressure and len(ob) <= self.outbox_max // 2:
            self._backpressure = False
            self.log(f"status outbox back to {len(ob)} reports", event="outbox.backpressure-end", outboxLength=len(ob))

    def notice(self, dep: str, key: str, state: str, **kw):
        """Report once per (dep, key): avoids re-sending 'Pending' every tick."""
        if self.state["notices"].get(dep) != key:
            self.state["notices"][dep] = key
            self.report(dep, state, **kw)

    # ---- cloud I/O -----------------------------------------------------------------------------
    def onboard(self):
        """One-time onboarding. The token comes from `onboardingTokenFile` (EV-06: 0600, consumed = overwritten and
        deleted once the cloud confirmed and the onboarded state is saved) or, failing that, `onboardingToken`."""
        if self.state["onboarded"]:
            return
        tok = None
        path = self.cfg.get("onboardingTokenFile")
        if path and (Path(path).exists() or not self.cfg.get("onboardingToken")):
            tok = secrets.consume_onboarding_token(path)  # TokenConsumedError / SecretPermissionError: logged, retried
        value = tok.value if tok is not None else self.cfg.get("onboardingToken")
        if not value:
            raise secrets.TokenConsumedError(f"device {self.device} is not onboarded and has no onboarding token "
                                             "(set onboardingTokenFile or onboardingToken)")
        _, _, data = self.client.post_json("/api/v1/onboarding", {
            "deviceId": self.device, "publicKey": crypto.public_b64(self.key), "token": value}, signed=False)
        resp = json.loads(data)
        if resp["cloudPublicKey"] != self.cloud_pub:
            raise RuntimeError("cloud key at onboarding does not match the provisioned trust anchor")
        self.state["onboarded"] = True
        self.save()
        if tok is not None:
            tok.mark_consumed()
        self.log("onboarded", event="onboarded")

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

    def sync_capabilities(self):
        """PUT the capabilities when they changed: checked at start and every capsEveryTicks ticks (default 30), so a
        fragment built while an executor was unreachable (reachable/available false) is replaced once it answers."""
        n = max(1, int(self.cfg.get("capsEveryTicks", 30)))
        due = (not self.state.get("capsHash") or not self.state.get("capsSent", True) or self.ticks % n == 0)
        if not due:
            return
        doc = self.capabilities()
        h = "sha256:" + hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if h == self.state.get("capsHash") and self.state.get("capsSent", True):
            return
        self.client.post_json(f"/api/v1/devices/{self.device}/capabilities", doc, method="PUT")
        self.state["capsHash"] = h
        self.state["capsSent"] = True
        self.save()
        self.log("capabilities sent", event="capabilities.sent", capsHash=h[:19])

    def flush_outbox(self):
        while self.state["outbox"]:
            r = self.state["outbox"][0]
            if r.get("dep") is None:   # device-level event (desired state refused), CP-2 device-event
                self.client.post_json(f"/api/v1/devices/{self.device}/events", {k: v for k, v in r.items() if k != "dep"})
            else:
                self.client.post_json(f"/api/v1/devices/{self.device}/deployments/{r['dep']}/status", r)
            self.state["outbox"].pop(0)
            self.save()
        if self._backpressure and len(self.state["outbox"]) <= self.outbox_max // 2:
            self._bound_outbox()

    def pull(self) -> bool:
        h = {"If-None-Match": self.state["etag"]} if self.state["etag"] else {}
        status, hdrs, body = self.client.request("GET", f"/api/v1/devices/{self.device}/deployments", headers=h)
        if status == 304:
            return False
        return self.accept_manifest(body, hdrs.get("x-flex-manifest-signature", ""), hdrs.get("etag"),
                                    traceparent=hdrs.get("traceparent"))

    def accept_manifest(self, body: bytes, sig: str, etag: str | None, traceparent: str | None = None) -> bool:
        """Accept a desired state only when it is signed by the cloud, is a valid CP-1 document (lead decision
        2026-10-17: checked against desired-state.v1alpha1 before anything else reads it), is for this device and
        does not go back in manifestVersion. A refused document changes nothing (the previous state keeps running)
        and is reported once as a device event (status outbox -> POST /devices/{id}/events) and in the local API."""
        if not crypto.verify(self.cloud_pub, body, sig):
            return self.refuse(body, reason="desired state with bad/missing cloud signature ignored")
        try:
            doc = json.loads(body)
        except ValueError as e:
            return self.refuse(body, reason=f"desired state is not valid JSON: {e}")
        errs = contracts.errors("desired-state", doc)
        if errs:
            detail = "; ".join(errs[:3]) + (f" (+{len(errs) - 3} more)" if len(errs) > 3 else "")
            return self.refuse(body, reason=f"desired state does not match the CP-1 schema: {detail}",
                               offered=doc.get("manifestVersion") if isinstance(doc, dict) else None)
        if doc.get("deviceId") != self.device:
            return self.refuse(body, reason="desired state for another device ignored", offered=doc["manifestVersion"])
        mv = int(doc["manifestVersion"])
        if mv < self.state["manifestVersion"] or (mv == self.state["manifestVersion"] and self.state["desired"]
                                                  and self.state["desired"] != doc):
            self.state.setdefault("downgradesRefused", 0)
            self.state["downgradesRefused"] += 1
            self.save()
            return self.refuse(body, offered=mv,
                               reason=f"manifestVersion {mv} <= accepted {self.state['manifestVersion']}: rollback attack? ignored")
        changed = self.state["desired"] != doc
        self.state.update(desired=doc, manifestVersion=mv, etag=etag)
        if traceparent:  # EV-09: the plan's trace context, per manifest version (survives a restart)
            tps = self.state.setdefault("traceparents", {})
            tps[str(mv)] = traceparent
            for old in sorted(tps, key=lambda k: int(k) if k.isdigit() else 0)[:-8]:
                del tps[old]
        self.save()
        if changed:
            self.log(f"accepted desired state v{mv} ({len(doc['deployments'])} deployments)", event="manifest.accepted",
                     manifestVersion=mv)
        return changed

    def refuse(self, body: bytes, reason: str, offered=None) -> bool:
        """A refused desired state: log it, keep it as `lastRefusal` (local API), and report it once per distinct
        (document, reason) as a device event, so a cloud that keeps serving the same bad document does not flood the
        outbox. Returns False (nothing accepted)."""
        import hashlib

        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        code = reason_code_of(reason)
        self.log(reason, "warning", "manifest.refused", reasonCode=code, offeredManifestVersion=offered)
        last = self.state.get("lastRefusal") or {}
        if last.get("digest") == digest and last.get("reason") == reason:
            last["count"] = int(last.get("count", 1)) + 1
            last["lastAt"] = time.time()
            self.save()
            return False
        self.state["lastRefusal"] = {"at": time.time(), "lastAt": time.time(), "count": 1, "digest": digest,
                                     "reason": reason, "reasonCode": code, "offeredManifestVersion": offered,
                                     "acceptedManifestVersion": self.state["manifestVersion"]}
        self.state["seq"] += 1
        ev = {"dep": None, "kind": "DeviceEvent", "event": "DesiredStateRefused", "seq": self.state["seq"],
              "deviceTime": time.time(), "reason": reason, "documentDigest": digest,
              "offeredManifestVersion": offered if isinstance(offered, int) else None,
              "acceptedManifestVersion": self.state["manifestVersion"]}
        if code:
            ev["reasonCode"] = code
        self.state["outbox"].append(ev)
        self._bound_outbox()
        self.save()
        self._hist(None, "DesiredStateRefused", "reported", seq=ev["seq"], reason=reason, reasonCode=code,
                   manifestVersion=offered)
        return False

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
        self._hist(dep_id, "TESTING", once="rollback" if rollback else "forward")
        with self.tracer.span("engine.health-gate", self._mv_traceparent(), **{"flex.deployment": dep_id,
                                                                              "flex.rollback": rollback}) as sp:
            ok, dt = self._health_gate(drv, dep_id, probe, rollback)
            tracing.set_attrs(sp, **{"flex.healthy": ok, "flex.gate_failure": str(self.gate_failure or ""),
                                     "flex.seconds": round(dt, 3)})
        _observe(self._m["gateSeconds"], dt)
        return ok, dt

    def _health_gate(self, drv: Driver, dep_id: str, probe: dict, rollback: bool) -> tuple[bool, float]:
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
                    self._hist(dep_id, "COMMITTING", once="rollback" if rollback else "forward")
                    try:
                        commit(dep_id)  # two-phase executors confirm only after a passed gate (P3 amendment)
                    except DriverError as e:
                        self.gate_failure = f"commit failed: {e}"
                        self.log(self.gate_failure, "warning", "commit.failed", dep=dep_id)
                        return False, time.time() - t0
                    self.action("commit", dep_id)
                return True, time.time() - t0
            time.sleep(period)
        self.gate_failure = f"timeout after {timeout:g} s"
        return False, time.time() - t0

    def _driver_call(self, op: str, drv: Driver, profile: str, fn, *args):
        """driver.apply / driver.remove span around one executor call (CLIENT kind, child of the install span)."""
        with self.tracer.span(f"driver.{op}", self._mv_traceparent(), client=True,
                              **{"flex.driver": profile, "flex.profile": getattr(drv, "profile", profile),
                                 "flex.deployment": self._dep}) as sp:
            t0 = time.time()
            try:
                return fn(*args)
            finally:
                tracing.set_attrs(sp, **{"flex.seconds": round(time.time() - t0, 3)})

    def reconcile(self):
        self.resolve_journal()   # crash-resume: an install journal left by a crash (EV-13)
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
            self._dep = dep_id
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
            self._hist(dep_id, "PREPARING", once=rv["digest"], version=rv["revision"])
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
                self.log(f"prepare deferred (offline): {e}", event="prepare.deferred", dep=dep_id)
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
            self._dep = dep_id
            cur = applied[dep_id]
            ok, why = self._gate(cur["dep"])
            if not ok:
                self.notice(dep_id, f"pending-remove:{why}", "Pending", reason=why, operation="remove")
                continue
            self.report(dep_id, "Removing", version=cur["wl"]["version"])
            self.action("remove", dep_id, version=cur["wl"]["version"], digest=cur["wl"]["digest"])
            drv = self.drivers[cur["dep"]["x-flex-driver"]]
            self._driver_call("remove", drv, cur["dep"]["x-flex-driver"], drv.remove, dep_id)
            del applied[dep_id]
            self.state["notices"].pop(dep_id, None)
            self.report(dep_id, "Removed", version=cur["wl"]["version"])

        # 3. installs / updates
        for dep_id, (d, rel) in targets.items():
            self._dep = dep_id
            ok, why = self._gate(d)
            rv = self._rev(d)
            if not ok:
                self._hist(dep_id, "GATE", once=(rv["digest"], why), version=rv["revision"], reason=why)
                self.notice(dep_id, f"pending:{rv['digest']}:{why}", "Pending", reason=why, version=rv["revision"])
                continue
            self.install(dep_id, d, rel)
        self._dep = None

    def install(self, dep_id: str, d: dict, rel):
        rv = self._rev(d)
        mv = (self.state.get("desired") or {}).get("manifestVersion")
        with self.tracer.span(f"engine.install {dep_id}", self._mv_traceparent(),
                              **{"flex.deployment": dep_id, "flex.version": rv["revision"],
                                 "flex.driver": d["x-flex-driver"], "flex.manifest_version": mv}):
            return self._install(dep_id, d, rel)

    def _install(self, dep_id: str, d: dict, rel):
        applied = self.state["applied"]
        drv = self.drivers[d["x-flex-driver"]]
        prof = d["x-flex-driver"]
        wl = self._workload(d, rel)
        cur = applied.get(dep_id)
        cur_wl = Workload.from_dict(cur["wl"]) if cur else None
        self.state["journal"] = {"dep": dep_id, "digest": wl.digest, "version": wl.version, "driver": prof,
                                 "phase": "applying", "at": time.time()}
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
                self._driver_call("apply", drv, prof, drv.apply, cur_wl, wl)
            except DriverError as e:
                apply_error = str(e)
                self.log(f"apply failed: {e}", "warning", "apply.failed", dep=dep_id, version=wl.version)
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
        # apply or health gate failed -> automatic rollback. The verdict (digest failed) and the rollback phase are
        # made durable *before* the rollback starts, for every driver: a crash, a power cut or a rebooting driver's
        # reboot (os-rauc, `reboots_on_apply`) inside the rollback ends this process before the Failed report; the
        # next reconcile finishes the rollback from the journal and reports (resolve_journal), and the known-bad
        # release is never started again (EV-13, EV-17).
        self.state["failed"][dep_id] = wl.digest
        reason = "apply failed" if apply_error is not None else "health check failed"
        self.state["journal"].update(
            phase="rolling-back", failedDigest=wl.digest, failedVersion=wl.version,
            rolledBackTo=cur_wl.version if cur_wl else None, reason=reason, failure=why,
            failFast=bool(why) and not str(why).startswith("timeout"), detectSeconds=round(dt, 3), startedAt=t0)
        self.save()
        self._rollback(dep_id, drv, prof, wl.version, wl.digest, wl, cur_wl, self.state["journal"])

    def _rollback(self, dep_id: str, drv: Driver, prof: str, version: str, digest: str, failed_wl, cur_wl,
                  j: dict, resumed: bool = False):
        """ROLLING_BACK: re-apply the committed previous version (or remove a failed first install), close the
        journal and report Failed. Also run from the persisted journal after a crash (`resumed`): then a previous
        version the executor already runs again is not applied a second time."""
        reason, why = j.get("reason") or "health check failed", j.get("failure")
        dt, t0 = float(j.get("detectSeconds") or 0), float(j.get("startedAt") or j.get("at") or time.time())
        detail = {"failure": why, "failFast": bool(why) and not str(why).startswith("timeout")}
        again = {"resumed": True} if resumed else {}
        if cur_wl:
            self._hist(dep_id, "ROLLING_BACK", version=cur_wl.version, failed=version)
            self.action("rollback-resume" if resumed else "rollback", dep_id, version=cur_wl.version,
                        digest=cur_wl.digest, failed=version)
            with self.tracer.span("engine.rollback", self._mv_traceparent(),
                                  **{"flex.deployment": dep_id, "flex.version": cur_wl.version,
                                     "flex.failed_version": version}) as sp:
                try:
                    obs = drv.status(dep_id) if resumed else None
                    if not (obs is not None and obs.present and obs.running and obs.digest == cur_wl.digest):
                        self._driver_call("apply", drv, prof, drv.apply, failed_wl, cur_wl)
                    back, _ = self.health_gate(drv, dep_id, cur_wl.health, rollback=True)
                except DriverError as e:
                    back = False
                    self.log(f"rollback failed: {e}", "error", "rollback.failed", dep=dep_id, version=cur_wl.version)
                tracing.set_attrs(sp, **{"flex.healthy": back})
            self.state["journal"] = None
            fd = getattr(drv, "failure_detail", None)   # e.g. when the fault became visible on the device
            extra = (fd(dep_id, time.time()) or {}) if fd and back else {}
            self.report(dep_id, "Failed", version=version, digest=digest, reason=reason,
                        rolledBackTo=cur_wl.version, rollbackHealthy=back, **detail,
                        detectSeconds=round(dt, 3), totalSeconds=round(time.time() - t0, 3), **extra)
        else:
            self._hist(dep_id, "ROLLING_BACK", version=None, failed=version)
            self.action("remove", dep_id, version=version, digest=digest, why="failed first install", **again)
            self._driver_call("remove", drv, prof, drv.remove, dep_id)
            self.state["journal"] = None
            self.report(dep_id, "Failed", version=version, digest=digest, reason=reason,
                        rolledBackTo=None, **detail, detectSeconds=round(dt, 3))

    def resolve_journal(self):
        """Crash-resume of an install journal found at the start of a reconcile (EV-13):

        * phase `rolling-back`: the failed verdict is already durable; finish the rollback (re-apply the committed
          version, or remove a failed first install) and report Failed -- the known-bad release is not started again
        * phase `applying` whose (deployment, digest) the accepted desired state still asks for: left to the install
          path (it adopts what the executor already runs, or applies again)
        * phase `applying` no longer desired (desired state changed, deployment dropped, manifest rejected): the
          uncommitted release is taken off the executor -- the committed version is restored (action `repair`,
          report Installed) or, without one, the deployment is removed (report Removed)."""
        j = self.state.get("journal")
        if not j:
            return
        dep_id, digest = j.get("dep"), j.get("digest")
        version = j.get("version") or (digest or "?")[:19]
        cur = self.state["applied"].get(dep_id)
        cur_wl = Workload.from_dict(cur["wl"]) if cur else None
        prof = j.get("driver") or (cur["dep"]["x-flex-driver"] if cur else None)
        doc = self.state.get("desired") or {}
        want = None
        if doc and (self.state.get("rejected") or {}).get("manifestVersion") != doc.get("manifestVersion"):
            want = next((d for d in doc.get("deployments", []) if d["metadata"]["id"] == dep_id), None)
        if prof is None and want is not None:
            prof = want.get("x-flex-driver")
        drv = self.drivers.get(prof) if prof else None
        if drv is None:
            self.log(f"install journal of {dep_id} names no available driver ({prof}); dropped", "warning",
                     "journal.dropped", dep=dep_id)
            self.state["journal"] = None
            self.save()
            return
        self._dep = dep_id
        if j.get("phase") == "rolling-back":
            # failedDigest/failedVersion name the failed release (an os-rauc journal of 2026-10-17 kept the rollback
            # target in `digest`; current journals carry both)
            digest, version = j.get("failedDigest") or digest, j.get("failedVersion") or version
            self.log(f"resuming the rollback of {dep_id} {version} after a restart", "warning", "rollback.resumed",
                     dep=dep_id, version=version)
            self.state["failed"][dep_id] = digest
            self._rollback(dep_id, drv, prof, version, digest, None, cur_wl, j, resumed=True)
            return
        if want is not None and self._rev(want)["digest"] == digest and self.state["failed"].get(dep_id) != digest:
            return   # still desired: the install path resumes it (adopt or apply)
        obs = drv.status(dep_id)
        if obs.present and not obs.running and str(obs.detail).startswith("executor unreachable"):
            raise Offline(f"{prof} executor unreachable: {obs.detail}")   # resolved once it answers again
        why = f"interrupted install of {version} abandoned (no longer desired)"
        self.log(f"{dep_id}: {why}", "warning", "journal.abandoned", dep=dep_id, version=version)
        if cur_wl is not None:
            if not (obs.present and obs.running and obs.digest == cur_wl.digest):
                self._hist(dep_id, "REPAIR", version=cur_wl.version, abandoned=version)
                self.action("repair", dep_id, version=cur_wl.version, digest=cur_wl.digest, abandoned=version)
                self._driver_call("apply", drv, prof, drv.apply, None, cur_wl)
            self.state["journal"] = None
            self.report(dep_id, "Installed", version=cur_wl.version, digest=cur_wl.digest, reason=why)
        else:
            if obs.present:
                self.action("remove", dep_id, version=version, digest=digest, why="abandoned install")
                self._driver_call("remove", drv, prof, drv.remove, dep_id)
            self.state["journal"] = None
            self.report(dep_id, "Removed", version=version, reason=why)
        self._dep = None

    # ---- supervision / drift ------------------------------------------------------------------
    def supervise(self):
        for drv in self.drivers.values():
            for ev in drv.supervise():
                self.log(f"supervision: {ev}", event="supervision", dep=ev.get("dep"), supervision=ev.get("event"))
                if ev["event"] in ("restarted", "start-limit-hit"):
                    self.report(ev["dep"], "Installed" if ev["event"] == "restarted" else "Failed",
                                reason=f"supervisor: {ev['event']}", restarts=ev.get("restarts"))
        check_health = self.ticks % int(self.cfg.get("healthEveryTicks", 3)) == 0
        for dep_id, rec in list(self.state["applied"].items()):
            self._dep = dep_id
            drv = self.drivers[rec["dep"]["x-flex-driver"]]
            try:
                obs = drv.status(dep_id)
            except DriverError as e:   # executor unreachable: defer this deployment, never repair blindly
                self.log(f"supervise deferred: {e}", "warning", "supervise.deferred", dep=dep_id)
                continue
            wl = Workload.from_dict(rec["wl"])
            if obs.present and not obs.running and str(obs.detail).startswith("executor unreachable"):
                continue               # SPI: status() of an unreachable executor = present, not running, detail
            if not obs.present:
                self._hist(dep_id, "REPAIR", version=wl.version)
                self.action("repair", dep_id, version=wl.version, digest=wl.digest)
                self._driver_call("apply", drv, rec["dep"]["x-flex-driver"], drv.apply, None, wl)
                self.report(dep_id, "Installed", version=wl.version, reason="drift repaired (workload was missing)")
                continue
            j = self.state.get("journal") or {}
            if (obs.running and obs.digest and obs.digest != wl.digest and getattr(drv, "repairs_digest_drift", True)
                    and not (j.get("dep") == dep_id and j.get("digest") == obs.digest)):
                # drift by digest (EV-13): the executor runs another release than the committed one (e.g. what an
                # interrupted install left behind) -> restore the committed version
                self._hist(dep_id, "REPAIR", version=wl.version, observed=obs.version)
                self.action("repair", dep_id, version=wl.version, digest=wl.digest, observed=obs.digest)
                try:
                    self._driver_call("apply", drv, rec["dep"]["x-flex-driver"], drv.apply, None, wl)
                except DriverError as e:
                    self.log(f"drift repair failed: {e}", "warning", "repair.failed", dep=dep_id, version=wl.version)
                    continue
                self.report(dep_id, "Installed", version=wl.version, digest=wl.digest,
                            reason="drift repaired (executor ran another release)")
                continue
            if check_health:
                try:
                    h = drv.health(dep_id, wl.health)
                except DriverError as e:
                    self.log(f"supervise deferred: {e}", "warning", "supervise.deferred", dep=dep_id)
                    continue
                if h != self.state["health"].get(dep_id):
                    self.state["health"][dep_id] = h
                    self.report(dep_id, "Installed", version=wl.version, health="healthy" if h else "unhealthy",
                                restarts=obs.restarts)
        self._dep = None

    # ---- main loop ---------------------------------------------------------------------------
    def _error(self, phase: str, e: BaseException):
        """Typed error record of one failed phase: logged (type, phase, deployment, where), counted, kept."""
        typ = type(e).__name__
        dep = self._dep
        tb = traceback.extract_tb(e.__traceback__)
        where = f"{Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else None
        m = self._m
        m["errors"][phase] = m["errors"].get(phase, 0) + 1
        m["errorsByType"][typ] = m["errorsByType"].get(typ, 0) + 1
        m["lastError"] = {"t": time.time(), "phase": phase, "dep": dep, "type": typ, "message": str(e)[:500],
                          "where": where}
        self.log(f"engine error in {phase}{f' ({dep})' if dep else ''}: {typ}: {e}", "error", "engine.error",
                 dep=dep, phase=phase, errorType=typ, where=where)

    def tick(self) -> float:
        """One engine round; never raises (a failing phase is logged, counted and backs the device off)."""
        t_start = time.monotonic()
        self.ticks += 1
        delay = self.poll
        failed = False
        self._dep = None
        try:
            self.onboard()
            self.sync_capabilities()
            self.flush_outbox()
            self.pull()
            self.backoff.reset()
        except Offline as e:
            delay = self.backoff.fail(e.retry_after)
            self.log(f"offline ({e}); retry in {delay:.2f}s", "warning", "offline", retryIn=round(delay, 2),
                     tls=True if isinstance(e, TlsError) else None)
        except Exception as e:  # noqa: BLE001 - typed record, never stops the host
            self._error("sync", e)
            failed = True
        self._dep = None
        t0 = time.monotonic()
        try:
            self.reconcile()
            self.flush_outbox()
        except Offline as e:
            delay = self.backoff.fail(e.retry_after)
        except Exception as e:  # noqa: BLE001
            self._error("reconcile", e)
            failed = True
        _observe(self._m["reconcileSeconds"], time.monotonic() - t0)
        self._dep = None
        try:
            self.supervise()
        except Exception as e:  # noqa: BLE001
            self._error("supervise", e)
            failed = True
        self._dep = None
        if failed:
            delay = max(delay, self.err_backoff.fail())
        else:
            self.err_backoff.reset()
        self._m["lastTickAt"] = time.time()
        _observe(self._m["tickSeconds"], time.monotonic() - t_start)
        return delay

    # ---- cross-thread interface (host worker thread / local API) -------------------------------
    def submit(self, fn, *args, **kw) -> concurrent.futures.Future:
        """Run `fn(*args, **kw)` on the thread that drives this engine, between two ticks (e.g. the local API's
        bundle import). Without a running worker thread, or when called from it, it runs at once."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self.worker is None or not self.worker.is_alive() or threading.current_thread() is self.worker:
            _run_into(fut, fn, args, kw)
        else:
            self._calls.put((fut, fn, args, kw))
            self._wake.set()
        return fut

    def run_pending(self) -> int:
        n = 0
        while True:
            try:
                fut, fn, args, kw = self._calls.get_nowait()
            except queue.Empty:
                return n
            n += 1
            if fut.set_running_or_notify_cancel():
                _run_into(fut, fn, args, kw)

    def sleep(self, delay: float) -> None:
        """Wait up to `delay` s; returns early on request_stop() or submit()."""
        self._wake.wait(max(0.0, delay))
        self._wake.clear()

    def wake(self) -> None:
        """End the current poll sleep now (e.g. the local API after an operator action); any thread."""
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def metrics(self) -> dict:
        """Basic in-memory metrics (EV-09): reconcile/tick/health-gate durations, outbox length, error counts.
        Safe to call from any thread; JSON-serialisable."""
        m = self._m
        return {"device": self.device, "role": self.role, "ticks": self.ticks, "startedAt": m["startedAt"],
                "lastTickAt": m["lastTickAt"],
                "reconcileSeconds": _summary(m["reconcileSeconds"]), "tickSeconds": _summary(m["tickSeconds"]),
                "gateSeconds": _summary(m["gateSeconds"]),
                "outboxLength": len(self.state.get("outbox") or []), "outboxMax": self.outbox_max,
                "outboxDropped": m["outboxDropped"], "backpressure": self._backpressure,
                "errors": dict(m["errors"]), "errorsByType": dict(m["errorsByType"]),
                "lastError": dict(m["lastError"]) if m["lastError"] else None,
                "manifestVersion": self.state.get("manifestVersion"),
                # dict.copy() is atomic under the GIL: safe while the worker thread changes `applied`
                "applied": sorted((self.state.get("applied") or {}).copy())}

    def run(self):
        """Single-device loop (testing.single): SIGTERM/SIGINT stop it, the state is saved on the way out."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *a: self.request_stop())
        self.log(f"engine start: manifestVersion={self.state['manifestVersion']} applied={sorted(self.state['applied'])} "
                 f"journal={self.state.get('journal')}", event="engine.start")
        while not self._stop.is_set():
            self.run_pending()
            delay = self.tick()
            self.sleep(delay)
        self.shutdown()

    def shutdown(self):
        """Final state save (the worker's last act); calls still queued by submit() fail with RuntimeError."""
        while True:
            try:
                fut, *_ = self._calls.get_nowait()
            except queue.Empty:
                break
            if fut.set_running_or_notify_cancel():
                fut.set_exception(RuntimeError(f"engine {self.device} stopped"))
        try:
            self.save()
        except OSError as e:
            self.log(f"final state save failed: {e}", "error", "engine.save-failed")
        self.log("engine stop", event="engine.stop")

    def import_bundle(self, path: str):
        """Offline bundle: the desired state must be signed by the cloud before anything from the bundle is stored;
        an unsigned one is refused like an online answer (FE-DS-001 device event) and the store stays untouched."""
        refused = []

        def accept(d: bytes, s: str) -> bool:
            if crypto.verify(self.cloud_pub, d, s):
                return True
            refused.append(self.refuse(d, reason="desired state with bad/missing cloud signature ignored"))
            return False
        try:
            desired, sig = self.store.import_bundle(Path(path).read_bytes(), accept=accept)
        except VerificationError:
            if refused:
                return
            raise
        if self.accept_manifest(desired, sig, None):
            self.log("desired state imported from offline bundle", event="bundle.imported")


def _run_into(fut, fn, args, kw):
    try:
        fut.set_result(fn(*args, **kw))
    except BaseException as e:  # noqa: BLE001 - handed to the caller
        fut.set_exception(e)


@functools.lru_cache(maxsize=2048)
def reason_code_of(text: str) -> str | None:
    """Stable code of a reported reason text (CP-2 registry), None when the text is not registered."""
    try:
        e = contracts.reason_code(str(text))
    except Exception:  # noqa: BLE001  (the registry must never break a report)
        return None
    return e["code"] if e else None


def _stat() -> dict:
    return {"last": None, "max": 0.0, "sum": 0.0, "count": 0}


def _observe(st: dict, v: float) -> None:
    st["last"] = round(v, 6)
    st["max"] = max(st["max"], round(v, 6))
    st["sum"] += v
    st["count"] += 1


def _summary(st: dict) -> dict:
    n = st["count"]
    return {"last": st["last"], "max": st["max"], "avg": round(st["sum"] / n, 6) if n else None, "count": n}


def build_drivers(cfg: dict, data_dir: Path) -> dict[str, Driver]:
    from flexengine2.drivers.container import AnkaiosDriver, CliContainerDriver
    from flexengine2.drivers.process import ProcessDriver

    out: dict[str, Driver] = {}
    dc = cfg.get("drivers", {}).get("container")
    if dc:
        if dc["runtime"] == "ankaios":
            out["container"] = AnkaiosDriver(cfg["deviceId"], dc["ankBin"], dc["serverUrl"], dc["agent"])
        else:
            out["container"] = CliContainerDriver(cfg["deviceId"], dc["runtime"], dc.get("allowedRunOptions"),
                                                  secrets_dir=dc.get("secretsDir"))   # EV-41 envFrom files
    nat = cfg.get("drivers", {}).get("native", True)
    if isinstance(nat, dict) and nat.get("runtime", "systemd") == "systemd":   # EV-19: real systemd units
        from flexengine2.drivers.native.systemd import SystemdDriver

        out["native"] = SystemdDriver(cfg["deviceId"], data_dir, scope=nat.get("scope", "system"),
                                      unit_dir=nat.get("unitDir"), user=nat.get("user"), python=nat.get("python"))
    elif nat:
        out["native"] = ProcessDriver(cfg["deviceId"], data_dir,
                                      python=nat.get("python") if isinstance(nat, dict) else None)  # EV-36
    dr = cfg.get("drivers", {}).get("os-rauc")
    if dr:
        from flexengine2.drivers.os_rauc import OsRaucDriver

        out["os-rauc"] = OsRaucDriver(cfg["deviceId"], data_dir, dr)
    da = cfg.get("drivers", {}).get("arduino-ota")
    if da:  # EV-38: flexNode-arduino boards over the Portenta OTA path
        from flexengine2.drivers.arduino_ota import ArduinoOtaDriver

        out["arduino-ota"] = ArduinoOtaDriver(cfg["deviceId"], data_dir, da)
    return out

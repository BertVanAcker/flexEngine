"""flexEngineV2 reconciler: one device engine (from probe P2, with the P3 gateway roles and the D1 PLC hold merged).

    loop:  flush status outbox -> pull desired state (ETag) -> reconcile -> supervise -> sleep/backoff

Reconcile is level-triggered: it compares the last *accepted* desired state (persisted, so it also
works offline and after a restart) with what the drivers observe, and converges:

  1. prepare  download + verify every changed release (all-or-nothing per manifest: one bad digest or
              signature keeps the whole previous state, as Margo requires), pick the payload layer for the
              driver's platform, run the driver's preflight (refusal = rejected like a bad signature)
  2. remove   deployments absent from the manifest (absence = removal); safety-gated like installs; dependents
              before their dependencies (reverse x-flex-dependsOn order, EV-42)
  3. install  per changed deployment, dependencies first (x-flex-dependsOn: held Pending until each dependency
              runs its desired release, healthy by default): activation gate (safe state / window) -> driver.apply ->
              health gate (fail-fast via driver.failed) -> driver.commit -> commit (keep previous as rollback
              target) or automatic rollback
  4. drift    a committed workload that disappeared is re-created; health changes are reported

FE-4 additions (optional keys, see api/README.md "Additive changes"): `x-flex-runState` (staged | running | stopped):
a staged or stopped deployment is installed through `driver.stage` (no health gate, status Staged / Stopped), a run
state change of an installed release is a start (dependencies first, activation gate, health gate; a failed start
rolls back to the previous release, or stops the release again when there is none) or a stop (dependents first,
activation gate); `x-flex-runtime` picks the driver instance of a runtime (engine.yaml lists several per profile);
`x-flex-build` builds the deployment on the device from its release's source payload once per build id (build
events in the outbox) before it is staged or started; a newer manifestVersion whose deployments equal those of the
manifest a release failed under retries that release (capabilities `x-flex.features` retry).

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
import re
import shutil
import signal
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from flexengine2.identity import crypto, keys, secrets
from flexengine2.util import read_json, repair_jsonl_tail
from flexengine2.artefacts.store import ArtefactStore, VerificationError
from flexengine2 import __version__
from flexengine2.drivers.base import (SPI_VERSION, BuildRequest, BuildTimeout, Driver, DriverError, Workload, supports,
                                      wait_until)
from flexengine2.cloud.client import CloudClient, Offline, Rejected, TlsError, from_cfg_kwargs
from flexengine2 import contracts
from flexengine2.core import outbox as outbox_policy
from flexengine2.core import state as state_file
from flexengine2.core.state import StateCorrupt, StateError, StateTooNew  # noqa: F401  (re-exported)
from flexengine2.obs import history as history_mod
from flexengine2.obs import logging as flog
from flexengine2.obs import tracing
from flexengine2.obs.resources import HostSampler
from flexengine2.obs.workloads import WorkloadObserver

OUTBOX_MAX = 1000
ERROR_BACKOFF = (1.0, 10.0)
BUILD_LOG_MAX = 1000            # BuildLog events per build (the rest goes to the engine log only)
BUILD_FLUSH_EVERY = 1.0         # s between outbox flushes while a build runs (live progress)
RUN_STATE_REPORT = {"staged": "Staged", "stopped": "Stopped", "running": "Installed"}


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
                 parent: str | None = None, children: list[dict] | None = None, mcu: dict | None = None,
                 runtimes: dict[str, Driver] | None = None):
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
        if runtimes is None and drivers is None:
            runtimes = build_runtime_drivers(cfg, self.dir)
        # FE-4: x-flex-runtime -> driver instance (each profile's default driver, plus the further runtimes of
        # engine.yaml); a deployment without x-flex-runtime runs on its profile's default driver
        self.runtime_drivers: dict[str, Driver] = {}
        for d in [*self.drivers.values(), *(runtimes or {}).values()]:
            rt = getattr(d, "runtime", None)
            if isinstance(rt, str) and rt not in self.runtime_drivers:
                self.runtime_drivers[rt] = d
        self._build_flush = 0.0
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
        # UM-4: device resources (cheap /proc + statvfs reads, on demand from metrics()) and the workload observer
        # (per-deployment resources and workload stdout/stderr through the drivers' optional SPI members, in its own
        # thread: obs/workloads.py). A child device's resources are its MCU's, not this host's: none are reported.
        self.host_resources = None if role == "child-device" else HostSampler(self.dir)
        self.observer = WorkloadObserver(self, cfg.get("observe"))

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
        if kind == "reported" and state in ("Installed", "Failed", "Removed", "Staged", "Stopped"):
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
        # the local API's status keeps rolledBackTo/failFast/restarts of the last report from the history once the
        # outbox copy is delivered (desk runbooks read them from GET /v1/status)
        self._hist(dep, state, "reported", seq=self.state["seq"], manifestVersion=mv, version=kw.get("version"),
                   reason=kw.get("reason"), health=kw.get("health"), correlationId=kw.get("correlationId"),
                   **{k: kw[k] for k in ("rolledBackTo", "failFast", "restarts") if k in kw})

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
                       "engine": {"impl": "flexengine2", "version": __version__, "spi": SPI_VERSION},
                       "features": self.features(), "runtimes": sorted(self.runtime_drivers)},
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

    # ---- FE-4: drivers per runtime, features -----------------------------------------------------------------
    def all_drivers(self) -> list[Driver]:
        """Every driver instance once (profile defaults and further runtimes)."""
        out: list[Driver] = []
        for d in [*self.drivers.values(), *self.runtime_drivers.values()]:
            if not any(d is o for o in out):
                out.append(d)
        return out

    def driver_for(self, dep: dict) -> Driver | None:
        """The driver instance of a deployment: its x-flex-runtime's, else its profile's default."""
        rt = dep.get("x-flex-runtime")
        if rt:
            drv = self.runtime_drivers.get(rt)
            return drv if drv is not None and drv.profile == dep.get("x-flex-driver") else None
        return self.drivers.get(dep.get("x-flex-driver"))

    def features(self) -> list[str]:
        """capabilities x-flex.features: what this engine honours of the optional FE-4 keys."""
        out = {"runtime", "retry"}
        drivers = self.all_drivers()
        if any(supports(d, "runState") for d in drivers):
            out.add("runState")
        if any(supports(d, "build") for d in drivers):
            out.add("build")
        return [f for f in contracts.FEATURES if f in out]

    def unsupported(self, doc: dict) -> str | None:
        """FE-4 refusal of a schema-valid desired state this device cannot carry out (FE-DS-007..009), else None."""
        feats = set(self.features())
        for d in doc["deployments"]:
            dep_id = d["metadata"]["id"]
            for key, feat in contracts.FEATURE_KEYS.items():
                if key in d and feat not in feats:
                    why = f"deployment {dep_id} uses {key}, which this device does not support (x-flex.features {feat})"
                    return why
            rt = d.get("x-flex-runtime")
            if rt and rt not in self.runtime_drivers:
                why = (f"deployment {dep_id} asks for runtime {rt}, which this device does not allow "
                       f"(allowed: {', '.join(sorted(self.runtime_drivers)) or 'none'})")
                return why
            drv = self.driver_for(d)
            if drv is not None and contracts.run_state(d) != "running" and not supports(drv, "runState"):
                why = f"deployment {dep_id}: run state {contracts.run_state(d)} is not supported by the {drv.name} driver"
                return why
        return None

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
            offered = doc.get("manifestVersion") if isinstance(doc, dict) else None
            if _only_dependency_errors(doc, errs):   # EV-42: unknown id, duplicate or cycle in x-flex-dependsOn
                return self.refuse(body, reason=f"desired state has invalid deployment dependencies: {detail}",
                                   offered=offered)
            return self.refuse(body, reason=f"desired state does not match the CP-1 schema: {detail}",
                               offered=offered)
        if doc.get("deviceId") != self.device:
            return self.refuse(body, reason="desired state for another device ignored", offered=doc["manifestVersion"])
        why = self.unsupported(doc)
        if why:   # FE-4: a key whose feature, a runtime or a run state this device does not have (FE-DS-007..009)
            return self.refuse(body, reason=why, offered=doc["manifestVersion"])
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

    def _waiting_for(self, dep: dict, desired: dict[str, dict]) -> list[str]:
        """x-flex-dependsOn (EV-42): the dependencies of `dep` that are not ready yet. A dependency is ready when it is
        committed at its desired release (digest and parameters; condition `started`) and, for condition `healthy`
        (the default), its last observed health is healthy. A dependency that is Pending, Installing, Failed (rolled
        back to its previous version) or not installed is not ready, so its dependents keep their previous version."""
        out = []
        for other, cond in contracts.depends_on(dep):
            want = desired.get(other)
            if want is None:
                continue   # refused by the semantic rule; a hand-made state does not block on it
            if contracts.run_state(want) != "running":
                continue   # FE-4: the desired state asks for it not to run (staged/stopped): nothing to wait for
            cur = self.state["applied"].get(other)
            rv = self._rev(want)
            ready = (cur is not None and cur["wl"]["digest"] == rv["digest"]
                     and cur["wl"]["parameters"] == want["spec"].get("parameters", {})
                     and cur.get("run", "running") == "running"          # FE-4: a staged/stopped one does not run
                     and (cond == "started" or bool(self.state["health"].get(other))))
            if not ready:
                out.append(other)
        return out

    def _workload(self, dep: dict, rel) -> Workload:
        rv = self._rev(dep)
        return Workload(dep_id=dep["metadata"]["id"], module=dep["spec"]["applicationId"], version=rv["revision"],
                        digest=rv["digest"], payload_path=rel.payload_path,
                        payload_annotations=rel.payload_annotations, parameters=dep["spec"].get("parameters", {}),
                        health=rel.descriptor.get("health", {}), restart=rel.descriptor.get("restartPolicy", {}),
                        run_options=rel.descriptor.get("runOptions") or {}, build=dict(getattr(rel, "build", None) or {}))

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

    # ---- FE-4 helpers: failed verdicts and their retry, run-state reports, build events -------------------------
    @staticmethod
    def _deployments_hash(doc: dict) -> str:
        """Identity of a desired state's deployments (without manifestVersion): a retry is a newer manifestVersion
        with the same deployments."""
        body = json.dumps(doc.get("deployments") or [], sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def _mark_failed(self, dep_id: str, digest: str, build: str | None = None):
        """The verdict `digest failed` of a deployment, with the manifest it failed under (the retry rule)."""
        self.state["failed"][dep_id] = digest
        doc = self.state.get("desired") or {}
        self.state.setdefault("failedAt", {})[dep_id] = {
            "manifestVersion": doc.get("manifestVersion"), "deployments": self._deployments_hash(doc),
            **({"build": build} if build else {})}

    def _retry(self, doc: dict, desired: dict[str, dict]):
        """FE-4 feature retry: a release that failed (its health gate, its start, its build) is tried again when a
        newer manifestVersion carries the same deployments as the manifest it failed under: the control plane
        published the same desired state once more to ask for exactly that. Any other change keeps the verdict
        (a failed release is never re-installed by an unrelated update)."""
        mv, h = doc["manifestVersion"], None
        fa = self.state.setdefault("failedAt", {})
        for dep_id, digest in list(self.state["failed"].items()):
            at, want = fa.get(dep_id), desired.get(dep_id)
            if not at or want is None or self._rev(want)["digest"] != digest or not isinstance(at.get("manifestVersion"), int):
                continue
            h = h or self._deployments_hash(doc)
            if mv > at["manifestVersion"] and at.get("deployments") == h:
                del self.state["failed"][dep_id]
                fa.pop(dep_id, None)
                self.state["notices"].pop(dep_id, None)
                self.action("retry", dep_id, digest=digest, manifestVersion=mv)
        for dep_id, b in list((self.state.get("builds") or {}).items()):
            want = desired.get(dep_id)
            if (b.get("state") == "failed" and want is not None and contracts.build_id(want) == b.get("id")
                    and isinstance(b.get("manifestVersion"), int) and mv > b["manifestVersion"]):
                h = h or self._deployments_hash(doc)
                if b.get("deployments") == h:
                    del self.state["builds"][dep_id]
                    self.action("retry", dep_id, build=b.get("id"), manifestVersion=mv)

    def _report_run(self, dep_id: str, rec: dict, **kw):
        """Status of an installed release by its run state: Installed (with health), Staged or Stopped."""
        run = rec.get("run", "running")
        wl = rec["wl"]
        if run == "running":
            kw.setdefault("health", "healthy" if self.state["health"].get(dep_id) else "unhealthy")
        self.report(dep_id, RUN_STATE_REPORT[run], version=wl["version"], digest=wl["digest"], **kw)

    def _event(self, event: str, **kw):
        """A device-level record (CP-2 DeviceEvent) into the status outbox."""
        for k, v in self._trace_fields().items():
            kw.setdefault(k, v)
        if kw.get("reason") and "reasonCode" not in kw:
            code = reason_code_of(kw["reason"], "event")    # an event's own code, not one of a detail it embeds
            if code:
                kw["reasonCode"] = code
        self.state["seq"] += 1
        ev = {"dep": None, "kind": "DeviceEvent", "event": event, "seq": self.state["seq"], "deviceTime": time.time(),
              **kw}
        self.state["outbox"].append(ev)
        self._bound_outbox()
        self.save()
        return ev

    def _build_event(self, event: str, dep_id: str, build_id: str, **kw):
        ev = self._event(event, deployment=dep_id, buildId=build_id, **kw)
        if event in ("BuildStarted", "BuildSucceeded", "BuildFailed"):
            self._hist(dep_id, event, "reported", seq=ev["seq"], buildId=build_id, reason=kw.get("reason"),
                       reasonCode=ev.get("reasonCode"), resultDigest=kw.get("resultDigest"))
            self.log(f"{event} {dep_id} {build_id}" + (f": {kw['reason']}" if kw.get("reason") else ""),
                     "warning" if event == "BuildFailed" else "info", f"build.{event[5:].lower()}", dep=dep_id,
                     buildId=build_id)

    def _flush_soon(self):
        """While a build runs: deliver its events about once a second (the studio shows live progress)."""
        now = time.monotonic()
        if now - self._build_flush < BUILD_FLUSH_EVERY:
            return
        self._build_flush = now
        try:
            self.flush_outbox()
        except Exception:  # noqa: BLE001 -- offline or a refused report: delivered with the next tick
            pass

    def _build(self, dep_id: str, d: dict, drv: Driver | None, rel):
        """x-flex-build: build the deployment once per build id. Returns the release to install (its payload =
        the build's result), or None when the build failed (BuildFailed sent; the deployment keeps its previous
        state). A succeeded build is reused (no second build of the same id)."""
        bid, prof = contracts.build_id(d), d["x-flex-driver"]
        builds = self.state.setdefault("builds", {})
        rec = builds.get(dep_id) or {}
        if rec.get("id") == bid and rec.get("state") == "succeeded" and (
                not rec.get("payloadPath") or Path(rec["payloadPath"]).exists()):
            return self._built(rel, rec)
        if rec.get("id") == bid and rec.get("state") == "failed":
            return None
        rv = self._rev(d)
        builds[dep_id] = rec = {"id": bid, "state": "running", "digest": rv["digest"], "startedAt": time.time()}
        self.save()
        self._build_event("BuildStarted", dep_id, bid, profile=prof)
        if drv is None or not supports(drv, "build"):
            why = f"build step not supported for profile {prof}"
            return self._build_failed(dep_id, bid, why, prof)
        if not getattr(rel, "source_path", None):
            why = "release has no source payload for a build"
            return self._build_failed(dep_id, bid, why, prof)
        work = self.dir / "builds" / _safe(dep_id) / _safe(bid)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        keep = {work.name, _safe(((self.state["applied"].get(dep_id) or {}).get("wl") or {}).get("build", {})
                                 .get("id") or "")}
        for old in work.parent.iterdir():
            if old.is_dir() and old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)
        lines = {"n": 0, "dropped": 0}
        last = {"p": -1.0, "t": 0.0}

        def emit(kind, value):
            if kind == "progress":
                now = time.monotonic()
                if value >= 1.0 or value - last["p"] >= 0.05 or now - last["t"] >= 1.0:
                    last.update(p=value, t=now)
                    self._build_event("BuildProgress", dep_id, bid, progress=round(float(value), 4))
            else:
                self.log(f"build {dep_id} {bid}: {value}", "debug", "build.log", dep=dep_id, buildId=bid)
                if lines["n"] < BUILD_LOG_MAX:
                    lines["n"] += 1
                    self._build_event("BuildLog", dep_id, bid, line=str(value)[:2000])
                else:
                    lines["dropped"] += 1
            self._flush_soon()

        spec = dict(rel.descriptor.get("build") or {})
        req = BuildRequest(id=bid, source_path=rel.source_path, spec=spec, work_dir=str(work), emit=emit,
                           timeout=float(spec.get("timeoutSeconds") or 1800))
        emit("progress", 0.0)
        self.action("build", dep_id, version=rv["revision"], digest=rv["digest"], build=bid)
        with self.tracer.span("engine.build", self._mv_traceparent(), **{"flex.deployment": dep_id,
                                                                        "flex.build": bid}):
            try:
                res = drv.build(self._workload(d, rel), req)
            except BuildTimeout as e:
                return self._build_failed(dep_id, bid, str(e), prof)
            except DriverError as e:
                why = f"build failed: {e}"
                return self._build_failed(dep_id, bid, why, prof)
        if lines["dropped"]:
            self._build_event("BuildLog", dep_id, bid, line=f"... {lines['dropped']} more lines in the engine log")
        rec.update(state="succeeded", resultDigest=res.digest, payloadPath=res.payload_path,
                   annotations=res.payload_annotations, finishedAt=time.time())
        self.save()
        self._build_event("BuildProgress", dep_id, bid, progress=1.0)
        self._build_event("BuildSucceeded", dep_id, bid, resultDigest=res.digest, profile=prof)
        return self._built(rel, rec)

    def _build_failed(self, dep_id: str, bid: str, why: str, prof: str):
        doc = self.state.get("desired") or {}
        self.state["builds"][dep_id].update(state="failed", reason=why, finishedAt=time.time(),
                                            manifestVersion=doc.get("manifestVersion"),
                                            deployments=self._deployments_hash(doc))
        self.save()
        self._build_event("BuildFailed", dep_id, bid, reason=why, profile=prof)
        return None

    @staticmethod
    def _built(rel, rec: dict):
        """The verified release with the build's result as its payload."""
        return SimpleNamespace(digest=getattr(rel, "digest", None), descriptor=rel.descriptor,
                               payload_path=rec.get("payloadPath") or "",
                               payload_annotations=dict(rec.get("annotations") or {}),
                               signature=getattr(rel, "signature", None), source_path=getattr(rel, "source_path", None),
                               build={"id": rec["id"], "resultDigest": rec.get("resultDigest")})

    def _verify(self, d: dict, drv: Driver, repo: str, rv: dict, plat, build: bool):
        kw = {}
        mts = getattr(drv, "media_types", None)
        if mts:
            kw["media_types"] = tuple(mts)
        if build:
            kw["source"] = True
        return self.store.verify_release(repo, rv["digest"], d["x-flex-driver"], d.get("x-flex-safetyClass", "none"),
                                         platform=plat, **kw)

    # ---- reconcile -------------------------------------------------------------------------------------------
    def reconcile(self):
        self.resolve_journal()   # crash-resume: an install journal left by a crash (EV-13)
        desired_doc = self.state["desired"]
        if not desired_doc:
            return
        mv = desired_doc["manifestVersion"]
        if (self.state.get("rejected") or {}).get("manifestVersion") == mv:
            return  # keep previous state until a new manifest arrives
        why = self.unsupported(desired_doc)
        if why:   # FE-4 (a state not checked at acceptance, e.g. from an older engine's state.json)
            self.state["rejected"] = {"manifestVersion": mv, "reason": why}
            self.log(why, "warning", "manifest.unsupported", manifestVersion=mv)
            self.save()
            return
        desired = {d["metadata"]["id"]: d for d in desired_doc["deployments"]}
        applied = self.state["applied"]
        self._retry(desired_doc, desired)

        # 1. prepare (all-or-nothing)
        targets, transitions = {}, {}
        for dep_id, d in desired.items():
            self._dep = dep_id
            rv = self._rev(d)
            cur = applied.get(dep_id)
            drv = self.driver_for(d)
            bid = contracts.build_id(d)
            want = contracts.run_state(d)
            if (cur and cur["wl"]["digest"] == rv["digest"] and cur["wl"]["parameters"] == d["spec"].get("parameters", {})
                    and self.driver_for(cur["dep"]) is drv
                    and (bid is None or (cur["wl"].get("build") or {}).get("id") == bid)):
                have = cur.get("run", "running")
                if self.state["failed"].get(dep_id) == rv["digest"]:
                    # FE-4: a release whose start failed (stopped again): not started again until a retry; a stop
                    # or stage request clears the verdict (the next start is a fresh attempt)
                    if want == "running":
                        continue
                    del self.state["failed"][dep_id]
                    self.state.get("failedAt", {}).pop(dep_id, None)
                    if want != have:
                        transitions[dep_id] = want
                    else:
                        self._report_run(dep_id, cur)
                    continue
                reverted = self.state["failed"].pop(dep_id, None) is not None
                if reverted:
                    self.state.get("failedAt", {}).pop(dep_id, None)
                if want != have:
                    transitions[dep_id] = want
                elif reverted or self.state["notices"].pop(dep_id, None):
                    # desired went back to what is running (e.g. operator reverted a failed release)
                    self._report_run(dep_id, cur)
                continue
            fa = self.state.get("failedAt", {}).get(dep_id) or {}
            if (self.state["failed"].get(dep_id) == rv["digest"] and (bid is None or fa.get("build") == bid)
                    and want == "running"):
                continue   # a failed release is not installed again (a staged / stopped one runs nothing: allowed)
            if drv is None:
                self.state["rejected"] = {"manifestVersion": mv, "reason": f"no driver for {d['x-flex-driver']}"}
                self.report(dep_id, "Failed", reason=self.state["rejected"]["reason"], version=rv["revision"])
                return
            self._hist(dep_id, "PREPARING", once=rv["digest"], version=rv["revision"])
            repo = rv["repository"].removeprefix("oci://").split("/", 1)[1]
            try:
                plat = drv.platform() if hasattr(drv, "platform") else None
            except DriverError as e:
                raise Offline(f"{d['x-flex-driver']} executor unreachable: {e}") from e  # defer, do not reject
            built = (self.state.get("builds") or {}).get(dep_id) or {}
            if bid and built.get("id") == bid and built.get("state") == "failed":
                continue   # FE-4: a failed build keeps the deployment's previous state (until a new id or a retry)
            need_source = bool(bid) and not (built.get("id") == bid and built.get("state") == "succeeded")
            try:
                rel = self._verify(d, drv, repo, rv, plat, build=need_source)
            except Offline as e:
                self.log(f"prepare deferred (offline): {e}", event="prepare.deferred", dep=dep_id)
                raise
            except (VerificationError, Rejected, ValueError, KeyError) as e:
                self.state["rejected"] = {"manifestVersion": mv, "reason": str(e), "deployment": dep_id}
                self.action("reject", dep_id, version=rv["revision"], digest=rv["digest"], reason=str(e))
                self.report(dep_id, "Failed", reason=f"artefact rejected: {e}", version=rv["revision"],
                            digest=rv["digest"], keptPrevious=bool(applied))
                return
            if bid:   # FE-4 on-device build, before anything is staged or started
                rel = self._build(dep_id, d, drv, rel)
                if rel is None:
                    continue
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

        # 2. removals (absence = removal), dependents before their dependencies (reverse x-flex-dependsOn order, EV-42)
        removing = [k for k in applied if k not in desired]
        for dep_id in reversed([d["metadata"]["id"] for d in contracts.dependency_order([applied[k]["dep"]
                                                                                         for k in removing])]):
            self._dep = dep_id
            cur = applied[dep_id]
            first = [k for k in removing if k != dep_id and k in applied
                     and dep_id in {o for o, _ in contracts.depends_on(applied[k]["dep"])}]
            if first:   # a dependent's removal is still held (e.g. at its safe-state gate)
                why = "held: waiting for removal of " + ", ".join(first)
                self.notice(dep_id, f"pending-remove:{why}", "Pending", reason=why, operation="remove",
                            waitingFor=first)
                continue
            ok, why = self._gate(cur["dep"])
            if not ok:
                self.notice(dep_id, f"pending-remove:{why}", "Pending", reason=why, operation="remove")
                continue
            self.report(dep_id, "Removing", version=cur["wl"]["version"])
            self.action("remove", dep_id, version=cur["wl"]["version"], digest=cur["wl"]["digest"])
            drv = self.driver_for(cur["dep"])
            self._driver_call("remove", drv, cur["dep"]["x-flex-driver"], drv.remove, dep_id)
            del applied[dep_id]
            self.state["notices"].pop(dep_id, None)
            self.state["health"].pop(dep_id, None)
            self.report(dep_id, "Removed", version=cur["wl"]["version"])

        # 2b. FE-4 stops (run state staged / stopped of an installed release), dependents first
        stops = [desired[k] for k, w in transitions.items() if w != "running"]
        for dep_id in reversed([d["metadata"]["id"] for d in contracts.dependency_order(stops)]):
            self._dep = dep_id
            self._stop_run(dep_id, desired[dep_id], transitions[dep_id])

        # 3. installs / updates, dependencies first (x-flex-dependsOn, EV-42): a deployment whose dependencies are not
        # ready yet is held at the GATE (Pending, reported once, re-checked every tick) on its previous version
        for dep_id in [d["metadata"]["id"] for d in contracts.dependency_order(list(desired.values()))]:
            start = transitions.get(dep_id) == "running"
            if dep_id not in targets and not start:
                continue
            d, rel = targets.get(dep_id) or (desired[dep_id], None)
            self._dep = dep_id
            rv = self._rev(d)
            # staging runs nothing, so it does not wait for dependencies (it is still an activation: gated)
            wait = self._waiting_for(d, desired) if contracts.run_state(d) == "running" else []
            if wait:
                why = "held: waiting for " + ", ".join(wait)
                self._hist(dep_id, "GATE", once=(rv["digest"], why), version=rv["revision"], reason=why)
                self.notice(dep_id, f"pending:{rv['digest']}:{why}", "Pending", reason=why, version=rv["revision"],
                            waitingFor=wait)
                continue
            ok, why = self._gate(d)
            if not ok:
                self._hist(dep_id, "GATE", once=(rv["digest"], why), version=rv["revision"], reason=why)
                self.notice(dep_id, f"pending:{rv['digest']}:{why}", "Pending", reason=why, version=rv["revision"])
                continue
            if start:
                self._start_run(dep_id, d)
            else:
                self.install(dep_id, d, rel)
        self._dep = None

    def _stop_run(self, dep_id: str, d: dict, want: str):
        """FE-4: an installed release to staged / stopped (the activation gate holds it like a removal)."""
        rec = self.state["applied"][dep_id]
        if rec.get("run", "running") == "running":
            ok, why = self._gate(rec["dep"])
            if not ok:
                self.notice(dep_id, f"pending-stop:{why}", "Pending", reason=why, version=rec["wl"]["version"])
                return
            drv = self.driver_for(rec["dep"])
            self.action("stop", dep_id, version=rec["wl"]["version"], digest=rec["wl"]["digest"])
            try:
                self._driver_call("stop", drv, rec["dep"]["x-flex-driver"], drv.stop, dep_id)
            except DriverError as e:
                self.log(f"stop failed: {e}", "warning", "stop.failed", dep=dep_id)
                raise Offline(f"{rec['dep']['x-flex-driver']} stop deferred: {e}") from e
            self.state["health"].pop(dep_id, None)
        rec["run"] = want
        rec["dep"] = d
        self.state["notices"].pop(dep_id, None)
        self._report_run(dep_id, rec)

    def _start_run(self, dep_id: str, d: dict):
        """FE-4: start an installed (staged / stopped) release; the health gate decides. A failed start rolls back to
        the previous release when there is one, else the release is stopped again; the verdict is kept until a
        retry (or another release)."""
        applied = self.state["applied"]
        rec = applied[dep_id]
        drv, prof = self.driver_for(rec["dep"]), rec["dep"]["x-flex-driver"]
        wl = Workload.from_dict(rec["wl"])
        with self.tracer.span(f"engine.start {dep_id}", self._mv_traceparent(),
                              **{"flex.deployment": dep_id, "flex.version": wl.version, "flex.driver": prof}):
            self.state["journal"] = {"dep": dep_id, "digest": wl.digest, "version": wl.version, "driver": prof,
                                     "runtime": rec["dep"].get("x-flex-runtime"), "phase": "starting",
                                     "at": time.time()}
            self.save()
            t0 = time.time()
            self.report(dep_id, "Installing", version=wl.version, digest=wl.digest, fromVersion=wl.version,
                        plan=[f"start {wl.version} ({rec.get('run', 'running')} -> running)"])
            self.action("start", dep_id, version=wl.version, digest=wl.digest)
            err = None
            try:
                self._driver_call("start", drv, prof, drv.start, dep_id)
            except DriverError as e:
                err = str(e)
                self.log(f"start failed: {e}", "warning", "start.failed", dep=dep_id, version=wl.version)
            if err is not None:
                healthy, dt, why = False, 0.0, f"start failed: {err}"
            else:
                healthy, dt = self.health_gate(drv, dep_id, wl.health)
                why = getattr(self, "gate_failure", None)
            if healthy:
                rec.update(run="running", dep=d)
                self.state["failed"].pop(dep_id, None)
                self.state.get("failedAt", {}).pop(dep_id, None)
                self.state["notices"].pop(dep_id, None)
                self.state["journal"] = None
                self.state["health"][dep_id] = True
                self.report(dep_id, "Installed", version=wl.version, digest=wl.digest, health="healthy",
                            applySeconds=round(time.time() - t0, 3), healthGateSeconds=round(dt, 3))
                return
            self._mark_failed(dep_id, wl.digest, (wl.build or {}).get("id"))
            reason = "start failed" if err is not None else "health check failed"
            prev = rec.get("previous")
            fail_fast = bool(why) and not str(why).startswith("timeout")
            if prev and prev.get("digest") != wl.digest:
                prev_wl = Workload.from_dict(prev)
                applied[dep_id] = {"dep": rec["dep"], "wl": prev, "committedAt": time.time(), "previous": None,
                                   "run": "running"}
                self.state["journal"].update(
                    phase="rolling-back", failedDigest=wl.digest, failedVersion=wl.version, rolledBackTo=prev_wl.version,
                    reason=reason, failure=why, failFast=fail_fast, detectSeconds=round(dt, 3), startedAt=t0,
                    prevRun="running")
                self.save()
                self._rollback(dep_id, drv, prof, wl.version, wl.digest, wl, prev_wl, self.state["journal"])
                return
            try:
                self._driver_call("stop", drv, prof, drv.stop, dep_id)
            except DriverError as e:
                self.log(f"stop after a failed start failed: {e}", "warning", "stop.failed", dep=dep_id)
            self.state["journal"] = None
            self.state["health"].pop(dep_id, None)
            self.report(dep_id, "Failed", version=wl.version, digest=wl.digest, reason=reason, failure=why,
                        failFast=fail_fast, detectSeconds=round(dt, 3))

    def install(self, dep_id: str, d: dict, rel):
        rv = self._rev(d)
        mv = (self.state.get("desired") or {}).get("manifestVersion")
        with self.tracer.span(f"engine.install {dep_id}", self._mv_traceparent(),
                              **{"flex.deployment": dep_id, "flex.version": rv["revision"],
                                 "flex.driver": d["x-flex-driver"], "flex.manifest_version": mv}):
            return self._install(dep_id, d, rel)

    def _install(self, dep_id: str, d: dict, rel):
        applied = self.state["applied"]
        drv = self.driver_for(d)
        prof = d["x-flex-driver"]
        want = contracts.run_state(d)
        cur = applied.get(dep_id)
        if cur is not None and self.driver_for(cur["dep"]) is not drv:
            # FE-4: another runtime for this deployment: a fresh install there, the old executor's workload goes first
            old = self.driver_for(cur["dep"])
            self.action("remove", dep_id, version=cur["wl"]["version"], digest=cur["wl"]["digest"],
                        why="runtime changed")
            if old is not None:
                self._driver_call("remove", old, cur["dep"]["x-flex-driver"], old.remove, dep_id)
            del applied[dep_id]
            self.state["health"].pop(dep_id, None)
            self.save()
            cur = None
        wl = self._workload(d, rel)
        cur_wl = Workload.from_dict(cur["wl"]) if cur else None
        self.state["journal"] = {"dep": dep_id, "digest": wl.digest, "version": wl.version, "driver": prof,
                                 "runtime": d.get("x-flex-runtime"), "phase": "applying", "at": time.time()}
        if want != "running":
            self.state["journal"]["run"] = want
        self.save()
        obs = drv.status(dep_id)
        t0 = time.time()
        apply_error = None
        adoptable = obs.present and obs.digest == wl.digest and not wl.build   # a rebuild is never adopted
        if want != "running":
            return self._stage(dep_id, d, drv, prof, wl, cur, cur_wl, want, adoptable and not obs.running, t0)
        if adoptable and obs.running:
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
                               "previous": cur["wl"] if cur else None, "run": "running"}
            self.state["failed"].pop(dep_id, None)
            self.state.get("failedAt", {}).pop(dep_id, None)
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
        self._mark_failed(dep_id, wl.digest, (wl.build or {}).get("id"))
        reason = "apply failed" if apply_error is not None else "health check failed"
        self.state["journal"].update(
            phase="rolling-back", failedDigest=wl.digest, failedVersion=wl.version,
            rolledBackTo=cur_wl.version if cur_wl else None, reason=reason, failure=why,
            failFast=bool(why) and not str(why).startswith("timeout"), detectSeconds=round(dt, 3), startedAt=t0,
            prevRun=(cur or {}).get("run", "running"))
        self.save()
        self._rollback(dep_id, drv, prof, wl.version, wl.digest, wl, cur_wl, self.state["journal"])

    def _stage(self, dep_id, d, drv, prof, wl, cur, cur_wl, want, adopt, t0):
        """FE-4: install a release without starting it (run state staged / stopped): driver.stage, no health gate.
        A failed stage restores the previous release in its run state (like a failed apply)."""
        applied = self.state["applied"]
        err = None
        if adopt:
            self.action("adopt", dep_id, version=wl.version, digest=wl.digest)
        else:
            self.report(dep_id, "Installing", version=wl.version, digest=wl.digest,
                        fromVersion=cur_wl.version if cur_wl else None,
                        plan=[*drv.plan(cur_wl, wl), f"do not start ({want})"])
            self.action("stage", dep_id, version=wl.version, digest=wl.digest, run=want)
            try:
                self._driver_call("stage", drv, prof, drv.stage, cur_wl, wl)
            except DriverError as e:
                err = str(e)
                self.log(f"stage failed: {e}", "warning", "stage.failed", dep=dep_id, version=wl.version)
        if err is None:
            applied[dep_id] = {"dep": d, "wl": wl.to_dict(), "committedAt": time.time(),
                               "previous": cur["wl"] if cur else None, "run": want}
            self.state["failed"].pop(dep_id, None)
            self.state.get("failedAt", {}).pop(dep_id, None)
            self.state["notices"].pop(dep_id, None)
            self.state["journal"] = None
            self.state["health"].pop(dep_id, None)
            self._report_run(dep_id, applied[dep_id], applySeconds=round(time.time() - t0, 3))
            return
        self._mark_failed(dep_id, wl.digest, (wl.build or {}).get("id"))
        why = f"apply failed: {err}"
        self.state["journal"].update(
            phase="rolling-back", failedDigest=wl.digest, failedVersion=wl.version,
            rolledBackTo=cur_wl.version if cur_wl else None, reason="apply failed", failure=why, failFast=True,
            detectSeconds=0.0, startedAt=t0, prevRun=(cur or {}).get("run", "running"))
        self.save()
        self._rollback(dep_id, drv, prof, wl.version, wl.digest, wl, cur_wl, self.state["journal"])

    def _rollback(self, dep_id: str, drv: Driver, prof: str, version: str, digest: str, failed_wl, cur_wl,
                  j: dict, resumed: bool = False):
        """ROLLING_BACK: re-apply the committed previous version (or remove a failed first install), close the
        journal and report Failed. Also run from the persisted journal after a crash (`resumed`): then a previous
        version the executor already runs again is not applied a second time. FE-4: a previous version that was
        staged or stopped (journal prevRun) is restored staged / stopped (driver.stage, no gate)."""
        reason, why = j.get("reason") or "health check failed", j.get("failure")
        dt, t0 = float(j.get("detectSeconds") or 0), float(j.get("startedAt") or j.get("at") or time.time())
        detail = {"failure": why, "failFast": bool(why) and not str(why).startswith("timeout")}
        again = {"resumed": True} if resumed else {}
        prev_run = j.get("prevRun") or "running"
        if cur_wl:
            self._hist(dep_id, "ROLLING_BACK", version=cur_wl.version, failed=version)
            self.action("rollback-resume" if resumed else "rollback", dep_id, version=cur_wl.version,
                        digest=cur_wl.digest, failed=version)
            with self.tracer.span("engine.rollback", self._mv_traceparent(),
                                  **{"flex.deployment": dep_id, "flex.version": cur_wl.version,
                                     "flex.failed_version": version}) as sp:
                try:
                    obs = drv.status(dep_id) if resumed else None
                    if prev_run != "running":
                        if not (obs is not None and obs.present and not obs.running and obs.digest == cur_wl.digest):
                            self._driver_call("stage", drv, prof, drv.stage, failed_wl, cur_wl)
                        back = True
                    else:
                        if not (obs is not None and obs.present and obs.running and obs.digest == cur_wl.digest):
                            self._driver_call("apply", drv, prof, drv.apply, failed_wl, cur_wl)
                        back, _ = self.health_gate(drv, dep_id, cur_wl.health, rollback=True)
                except DriverError as e:
                    back = False
                    self.log(f"rollback failed: {e}", "error", "rollback.failed", dep=dep_id, version=cur_wl.version)
                tracing.set_attrs(sp, **{"flex.healthy": back})
            self.state["journal"] = None
            if prev_run != "running":
                self.state["health"].pop(dep_id, None)
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

    def _journal_driver(self, j: dict, cur: dict | None, want: dict | None) -> tuple[str | None, Driver | None]:
        prof = j.get("driver") or (cur["dep"]["x-flex-driver"] if cur else None)
        if prof is None and want is not None:
            prof = want.get("x-flex-driver")
        if j.get("runtime"):
            drv = self.runtime_drivers.get(j["runtime"])
        elif cur is not None and j.get("phase") in ("starting", "rolling-back"):
            drv = self.driver_for(cur["dep"])
        else:
            drv = self.drivers.get(prof) if prof else None
        return prof, drv

    def resolve_journal(self):
        """Crash-resume of an install journal found at the start of a reconcile (EV-13):

        * phase `rolling-back`: the failed verdict is already durable; finish the rollback (re-apply the committed
          version, or remove a failed first install) and report Failed -- the known-bad release is not started again
        * phase `applying` whose (deployment, digest) the accepted desired state still asks for: left to the install
          path (it adopts what the executor already runs, or applies again)
        * phase `applying` no longer desired (desired state changed, deployment dropped, manifest rejected): the
          uncommitted release is taken off the executor -- the committed version is restored (action `repair`,
          report Installed) or, without one, the deployment is removed (report Removed).
        * phase `applying` still desired but a dependency (x-flex-dependsOn, EV-42) is not ready: taken off the same
          way (FE-REC-010), so the new release never runs ahead of its dependency; the install path then holds it
          and installs it in order.
        * phase `starting` (FE-4, a start of an installed release): still desired running -> left to the start path
          (start is idempotent, the gate runs again); otherwise the release is stopped again."""
        j = self.state.get("journal")
        if not j:
            return
        dep_id, digest = j.get("dep"), j.get("digest")
        version = j.get("version") or (digest or "?")[:19]
        cur = self.state["applied"].get(dep_id)
        cur_wl = Workload.from_dict(cur["wl"]) if cur else None
        doc = self.state.get("desired") or {}
        want = None
        if doc and (self.state.get("rejected") or {}).get("manifestVersion") != doc.get("manifestVersion"):
            want = next((d for d in doc.get("deployments", []) if d["metadata"]["id"] == dep_id), None)
        prof, drv = self._journal_driver(j, cur, want)
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
            if self.state["failed"].get(dep_id) != digest:
                self._mark_failed(dep_id, digest)
            self._rollback(dep_id, drv, prof, version, digest, None, cur_wl, j, resumed=True)
            return
        if j.get("phase") == "starting":
            self.state["journal"] = None
            if not (want is not None and contracts.run_state(want) == "running" and cur_wl is not None
                    and cur_wl.digest == digest and self._rev(want)["digest"] == digest):
                try:
                    self._driver_call("stop", drv, prof, drv.stop, dep_id)
                except DriverError as e:
                    self.state["journal"] = j
                    raise Offline(f"{prof} executor unreachable: {e}") from e
                self.log(f"{dep_id}: interrupted start of {version} taken back (no longer desired running)",
                         "warning", "journal.abandoned", dep=dep_id, version=version)
            self.save()
            self._dep = None
            return
        wait: list[str] = []
        if want is not None and self._rev(want)["digest"] == digest and self.state["failed"].get(dep_id) != digest:
            if contracts.run_state(want) != "running":
                return   # FE-4: an interrupted stage: the install path stages it again (or adopts it)
            wait = self._waiting_for(want, {d["metadata"]["id"]: d for d in doc.get("deployments", [])})
            if not wait:
                return   # still desired: the install path resumes it (adopt or apply)
            # EV-42: still desired, but a dependency is not ready (any more), e.g. a newer desired state also changed
            # it: the uncommitted release must not keep running ahead of its dependency; it is taken off like an
            # abandoned install and installed again, in order, once the dependency is ready
        obs = drv.status(dep_id)
        if obs.present and not obs.running and str(obs.detail).startswith("executor unreachable"):
            raise Offline(f"{prof} executor unreachable: {obs.detail}")   # resolved once it answers again
        why = f"interrupted install of {version} abandoned (no longer desired)"
        if wait:
            why = f"interrupted install of {version} held: waiting for {', '.join(wait)}"
        self.log(f"{dep_id}: {why}", "warning", "journal.abandoned", dep=dep_id, version=version)
        if cur_wl is not None:
            cdrv = self.driver_for(cur["dep"]) or drv
            if cdrv is not drv and obs.present:   # FE-4: the interrupted install was on another runtime
                self._driver_call("remove", drv, prof, drv.remove, dep_id)
                obs = cdrv.status(dep_id)
            run = cur.get("run", "running")
            if run != "running":
                if not (obs.present and not obs.running and obs.digest == cur_wl.digest):
                    self._hist(dep_id, "REPAIR", version=cur_wl.version, abandoned=version)
                    self.action("repair", dep_id, version=cur_wl.version, digest=cur_wl.digest, abandoned=version)
                    self._driver_call("stage", cdrv, prof, cdrv.stage, None, cur_wl)
                self.state["journal"] = None
                self._report_run(dep_id, cur, reason=why)
            else:
                if not (obs.present and obs.running and obs.digest == cur_wl.digest):
                    self._hist(dep_id, "REPAIR", version=cur_wl.version, abandoned=version)
                    self.action("repair", dep_id, version=cur_wl.version, digest=cur_wl.digest, abandoned=version)
                    self._driver_call("apply", cdrv, prof, cdrv.apply, None, cur_wl)
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
        for drv in self.all_drivers():
            for ev in drv.supervise():
                self.log(f"supervision: {ev}", event="supervision", dep=ev.get("dep"), supervision=ev.get("event"))
                if ev["event"] in ("restarted", "start-limit-hit"):
                    self.report(ev["dep"], "Installed" if ev["event"] == "restarted" else "Failed",
                                reason=f"supervisor: {ev['event']}", restarts=ev.get("restarts"))
        check_health = self.ticks % int(self.cfg.get("healthEveryTicks", 3)) == 0
        for dep_id, rec in list(self.state["applied"].items()):
            self._dep = dep_id
            drv = self.driver_for(rec["dep"])
            if drv is None:
                continue
            try:
                obs = drv.status(dep_id)
            except DriverError as e:   # executor unreachable: defer this deployment, never repair blindly
                self.log(f"supervise deferred: {e}", "warning", "supervise.deferred", dep=dep_id)
                continue
            wl = Workload.from_dict(rec["wl"])
            if obs.present and not obs.running and str(obs.detail).startswith("executor unreachable"):
                continue               # SPI: status() of an unreachable executor = present, not running, detail
            if rec.get("run", "running") != "running":
                self._supervise_idle(dep_id, rec, drv, wl, obs)
                continue
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

    def _supervise_idle(self, dep_id: str, rec: dict, drv: Driver, wl: Workload, obs):
        """FE-4 drift of a staged / stopped release: gone -> staged again; running (started behind the engine's
        back) -> stopped again. No health checks: nothing is supposed to run."""
        prof = rec["dep"]["x-flex-driver"]
        try:
            if not obs.present:
                self._hist(dep_id, "REPAIR", version=wl.version)
                self.action("repair", dep_id, version=wl.version, digest=wl.digest, run=rec.get("run"))
                self._driver_call("stage", drv, prof, drv.stage, None, wl)
                self._report_run(dep_id, rec, reason="drift repaired (workload was missing)")
            elif obs.running:
                self.action("stop", dep_id, version=wl.version, digest=wl.digest, why="ran while " + rec.get("run"))
                self._driver_call("stop", drv, prof, drv.stop, dep_id)
        except DriverError as e:
            self.log(f"drift repair failed: {e}", "warning", "repair.failed", dep=dep_id, version=wl.version)

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
                "applied": sorted((self.state.get("applied") or {}).copy()), **self._resource_metrics()}

    def _resource_metrics(self) -> dict:
        """UM-4, additive in telemetry v1: `resources` (DeviceResources) and `deploymentResources`
        ({dep: DeploymentResources}), each only when there is something to report."""
        out = {}
        dev = self.host_resources.sample() if self.host_resources is not None else None
        if dev:
            out["resources"] = dev
        deps = self.observer.resources()
        if deps:
            out["deploymentResources"] = deps
        return out

    def run(self):
        """Single-device loop (testing.single): SIGTERM/SIGINT stop it, the state is saved on the way out."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *a: self.request_stop())
        self.log(f"engine start: manifestVersion={self.state['manifestVersion']} applied={sorted(self.state['applied'])} "
                 f"journal={self.state.get('journal')}", event="engine.start")
        self.observer.start()
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


def _safe(s: str) -> str:
    """A file name for an id (deployment, build)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s)) or "_"


def _only_dependency_errors(doc, errs: list[str]) -> bool:
    """True when every CP-1 violation of a desired state is an x-flex-dependsOn rule (FE-DS-006, EV-42)."""
    try:
        dep = set(contracts.dependency_errors(doc))
    except Exception:  # noqa: BLE001  (schema-invalid document: the generic FE-DS-005 reason)
        return False
    return bool(dep) and set(errs) <= dep


def _run_into(fut, fn, args, kw):
    try:
        fut.set_result(fn(*args, **kw))
    except BaseException as e:  # noqa: BLE001 - handed to the caller
        fut.set_exception(e)


@functools.lru_cache(maxsize=2048)
def reason_code_of(text: str, field: str | None = None) -> str | None:
    """Stable code of a reported reason text (CP-2 registry), None when the text is not registered; `field`
    restricts the match to the entries of one field (e.g. "event")."""
    try:
        e = contracts.reason_code(str(text), field)
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
    from flexengine2.drivers.container import AnkaiosDriver
    from flexengine2.drivers.process import ProcessDriver

    out: dict[str, Driver] = {}
    dc = cfg.get("drivers", {}).get("container")
    if dc:
        if dc["runtime"] == "ankaios":
            out["container"] = AnkaiosDriver(cfg["deviceId"], dc["ankBin"], dc["serverUrl"], dc["agent"])
        else:
            out["container"] = _container_driver(cfg["deviceId"], dc["runtime"], dc, data_dir)
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


def _container_driver(device: str, runtime: str, dc: dict, data_dir: Path) -> Driver:
    """docker | podman (CLI driver) or compose (FE-4, the compose driver) of drivers.container."""
    from flexengine2.drivers.container import CliContainerDriver

    if runtime == "compose":
        from flexengine2.drivers.compose import ComposeDriver

        cmd = list(dc.get("composeCommand") or ["docker", "compose"])
        return ComposeDriver(device, data_dir, cli=cmd[0], compose=cmd,
                             allow_host_access=bool(dc.get("composeAllowHostAccess")))
    return CliContainerDriver(device, runtime, dc.get("allowedRunOptions"),
                              secrets_dir=dc.get("secretsDir"))   # EV-41 envFrom files


def build_runtime_drivers(cfg: dict, data_dir: Path) -> dict[str, Driver]:
    """FE-4: the further runtimes a device allows next to each profile's default (engine.yaml
    drivers.container.runtimes, drivers.native.runtimes), keyed by their x-flex-runtime name. Each gets its own
    state directory (<dataDir>/runtimes/<runtime>), so two drivers of one profile never adopt or reap each other's
    workloads."""
    out: dict[str, Driver] = {}
    drv = cfg.get("drivers", {})
    dc = drv.get("container")
    if isinstance(dc, dict):
        for rt in dc.get("runtimes") or []:
            if rt == dc.get("runtime"):
                continue
            d = _container_driver(cfg["deviceId"], rt, dc, Path(data_dir) / "runtimes" / rt)
            out.setdefault(d.runtime, d)
    nat = drv.get("native")
    if isinstance(nat, dict):
        default = nat.get("runtime", "systemd")
        for rt in nat.get("runtimes") or []:
            if rt == default:
                continue
            ddir = Path(data_dir) / "runtimes" / rt
            if rt == "systemd":
                from flexengine2.drivers.native.systemd import SystemdDriver

                out["systemd"] = SystemdDriver(cfg["deviceId"], ddir, scope=nat.get("scope", "system"),
                                               unit_dir=nat.get("unitDir"), user=nat.get("user"),
                                               python=nat.get("python"))
            elif rt == "process":
                from flexengine2.drivers.process import ProcessDriver

                out["process"] = ProcessDriver(cfg["deviceId"], ddir, python=nat.get("python"))
    return out

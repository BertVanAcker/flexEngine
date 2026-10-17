"""In-process harness for the high-volume reconciler properties (EV-13): real `Engine` objects on a real data
directory, a persistent fake executor (`ModelExec`, survives engine "crashes" like a systemd unit survives the
engine), and crash injection by a `BaseException` raised at named points (the engine's `tick()` only catches
`Exception`, so `Crash` unwinds it exactly like a SIGKILL: nothing after the point runs, nothing is saved).

Kill points (event names, counted per incarnation, fired before or after the k-th occurrence):
    save                 Engine.save (every state.json write: journal, reports, accept, outbox pop)
    action:<kind>        Engine.action (apply / adopt / commit / rollback / remove / reject / repair), fsynced line
    drv:<op>             executor apply / remove / commit
A "before save" crash also leaves a torn temp file next to state.json (what a kill inside atomic_write leaves).
A power cut additionally stops every workload (`ModelExec.power_cut`) and drops unsynced history lines; completed
atomic writes and fsynced action lines are kept (util.atomic_write_bytes / Engine.action contract).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from fe_unit_fakes import FakeDriver, _digest, deployment
from flexengine2.artefacts.store import VerificationError
from flexengine2.core.reconciler import Engine
from flexengine2.drivers.base import DriverError, Observed
from flexengine2.identity import crypto
from flexengine2.obs import logging as flog
from flexengine2.util import canonical_json

import prop_oracle as O

DEPS = ("a", "b", "c")
# release kinds by version: what the executor / store does with that release
KINDS = {"1.0.0": "good", "1.1.0": "good", "1.2.0": "good", "2.0.0": "bad-health", "2.1.0": "apply-fail",
         "2.2.0": "commit-fail", "2.3.0": "rejected"}
GOOD = tuple(v for v, k in KINDS.items() if k == "good")
BAD = ("bad-health", "apply-fail", "commit-fail")
EVENTS = ("save", "action:apply", "action:adopt", "action:commit", "action:rollback", "action:remove",
          "action:reject", "action:repair", "drv:apply", "drv:remove", "drv:commit")
CLOUD_KEY = crypto.generate()


def digest(dep: str, version: str) -> str:
    return _digest(f"{dep}-{version}")


DIGEST_KIND = {digest(d, v): k for d in DEPS for v, k in KINDS.items()}


class Crash(BaseException):
    """SIGKILL stand-in: not an Exception, so Engine.tick() does not catch it."""


@dataclass
class KillPoint:
    event: str
    k: int = 1
    when: str = "before"   # before | after
    seen: int = 0
    fired: bool = False

    def hit(self, event: str, when: str):
        if self.fired or event != self.event:
            return
        if when == "before":
            self.seen += 1
        if self.seen == self.k and when == self.when:
            self.fired = True
            raise Crash(f"{event}#{self.k}:{when}")


class ModelExec(FakeDriver):
    """Persistent executor: units survive engine crashes; a power cut stops them; supervise() restarts what should
    run (systemd Restart= at boot)."""

    def __init__(self):
        super().__init__()
        self.kill: KillPoint | None = None
        self.on_apply = None   # ordering oracle (EV-42): called with the workload of every executor apply

    def _hit(self, ev, when):
        if self.kill:
            self.kill.hit(ev, when)

    def apply(self, current, desired):
        self._hit("drv:apply", "before")
        if self.on_apply is not None:
            self.on_apply(desired)
        self.calls.append(("apply", desired.dep_id))
        if DIGEST_KIND.get(desired.digest) == "apply-fail":
            self.units.pop(desired.dep_id, None)       # worst case: the old one was already stopped
            raise DriverError("executor refused the workload")
        self.units[desired.dep_id] = {"digest": desired.digest, "version": desired.version, "running": True}
        self._hit("drv:apply", "after")

    def remove(self, dep_id):
        self._hit("drv:remove", "before")
        self.units.pop(dep_id, None)
        self._hit("drv:remove", "after")

    def commit(self, dep_id):
        self._hit("drv:commit", "before")
        u = self.units.get(dep_id)
        if u and DIGEST_KIND.get(u["digest"]) == "commit-fail":
            raise DriverError("executor could not confirm")
        self._hit("drv:commit", "after")

    def status(self, dep_id):
        u = self.units.get(dep_id)
        return Observed(present=bool(u), running=bool(u and u["running"]), digest=(u or {}).get("digest"),
                        version=(u or {}).get("version"))

    def health(self, dep_id, probe):
        u = self.units.get(dep_id)
        return bool(u and u["running"] and DIGEST_KIND.get(u["digest"]) != "bad-health")

    def failed(self, dep_id):
        u = self.units.get(dep_id)
        return "crashed" if u and DIGEST_KIND.get(u["digest"]) == "bad-health" else None

    def supervise(self):
        ev = []
        for dep, u in self.units.items():
            if not u["running"]:
                u["running"] = True
                ev.append({"dep": dep, "event": "restarted", "restarts": 1})
        return ev

    def power_cut(self):
        for u in self.units.values():
            u["running"] = False

    def running(self) -> dict:
        return {d: u["digest"] for d, u in self.units.items() if u["running"]}


def manifest(device: str, mv: int, spec: dict[str, str], order: dict[str, tuple] | None = None) -> dict:
    """`order` {dependent: dependencies} (EV-42): x-flex-dependsOn for the pairs present in `spec`."""
    deps = [dict(deployment(d, v, digest=f"{d}-{v}")) for d, v in sorted(spec.items(), reverse=bool(order))]
    for x in deps:
        after = [a for a in (order or {}).get(x["metadata"]["id"], ()) if a in spec]
        if after:
            x["x-flex-dependsOn"] = [{"deployment": a} for a in after]
    return {"apiVersion": "flex.dev/v1alpha1", "kind": "DesiredState", "deviceId": device, "manifestVersion": mv,
            "deployments": [dict(x, metadata=dict(x["metadata"], deviceId=device)) for x in deps]}


def _fake_verify(repo, dg, drv, cls, platform=None):
    if DIGEST_KIND.get(dg) == "rejected":
        raise VerificationError("release not signed by a trusted key (no signature)")
    return SimpleNamespace(payload_path="/nonexistent", payload_annotations={},
                           descriptor={"health": {"timeoutSeconds": 0.2, "periodSeconds": 0.005}})


class InprocDevice:
    """One device: data dir + persistent executor; `engine` is the current incarnation."""

    def __init__(self, root: Path, device: str = "prop-dev", order: dict[str, tuple] | None = None):
        self.root = Path(tempfile.mkdtemp(prefix="inproc-", dir=root))
        self.device = device
        self.order = order or {}
        self.exec = ModelExec()
        if self.order:
            self.exec.on_apply = self._check_order
        self.crashes: list[dict] = []
        self.mv_seen: list[int] = []
        self.seq_floor = 0
        self.published: list[tuple[bytes, str, dict]] = []
        self.errs: list[str] = []
        self.fresh_boot = False
        self.engine = self._boot()

    @property
    def data(self) -> Path:
        return self.root / self.device

    def _boot(self) -> Engine:
        cfg = {"deviceId": self.device, "apiUrl": "http://127.0.0.1:9", "dataDir": str(self.data),
               "onboardingToken": "t", "cloudPublicKey": crypto.public_b64(CLOUD_KEY), "releasePublicKeys": [],
               "pollInterval": 0.01, "backoff": [0.01, 0.02], "httpTimeout": 0.2, "healthEveryTicks": 1}
        eng = Engine(cfg, {"native": self.exec})
        eng.store.verify_release = _fake_verify
        self._observe_disk(eng.state)
        return eng

    # ---- durable-state observations (after every step) -------------------------------------------------
    def disk_state(self) -> dict:
        return json.loads((self.data / "state.json").read_text()) if (self.data / "state.json").exists() else {}

    def _observe_disk(self, st: dict):
        mv = st.get("manifestVersion", 0)
        if self.mv_seen and mv < self.mv_seen[-1]:
            self.errs.append(f"manifestVersion went down on disk: {self.mv_seen[-1]} -> {mv}")
        self.mv_seen.append(mv)
        ob = st.get("outbox") or []
        self.errs += O.seq_strictly_increasing(ob)
        seq = st.get("seq", 0)
        if seq < self.seq_floor:
            self.errs.append(f"persisted seq went down: {self.seq_floor} -> {seq}")
        self.seq_floor = max(self.seq_floor, seq)

    def observe(self):
        self._observe_disk(self.disk_state())

    # ---- steps ---------------------------------------------------------------------------------------
    def publish(self, spec: dict[str, str]) -> int:
        mv = max([0] + [p[2]["manifestVersion"] for p in self.published]) + 1
        doc = manifest(self.device, mv, spec, self.order)
        body = canonical_json(doc)
        self.published.append((body, crypto.sign(CLOUD_KEY, body), doc))
        self.engine.accept_manifest(body, self.published[-1][1], None)
        self.observe()
        return mv

    def replay(self, i: int):
        """Offer an older (validly signed) manifest again: must be refused, manifestVersion stays."""
        if len(self.published) < 2:
            return
        body, sig, doc = self.published[i % (len(self.published) - 1)]
        before = self.engine.state["manifestVersion"]
        self.engine.accept_manifest(body, sig, None)
        if self.engine.state["manifestVersion"] != before:
            self.errs.append(f"replayed manifest v{doc['manifestVersion']} changed the accepted version {before}")
        self.observe()

    def ticks(self, n: int):
        self.fresh_boot = False
        for _ in range(n):
            self.engine.tick()
        self.observe()

    def drift(self, dep: str):
        """The workload vanished behind the engine's back (operator, OOM...)."""
        self.exec.units.pop(dep, None)
        if self.fresh_boot and self.crashes:   # vanished while the engine was down: gone at the restart
            self.crashes[-1]["running"].pop(dep, None)

    def crash(self, kp: KillPoint, power_cut: bool = False, max_ticks: int = 4, torn_action: bool = False):
        eng = self.engine
        orig_save, orig_action = eng.save, eng.action

        def save():
            if kp.event == "save" and kp.when == "before" and not kp.fired and kp.seen + 1 == kp.k:
                (self.data / f".state.json.torn{len(self.crashes)}").write_bytes(b'{"schemaVersion": 1, "devi')
            kp.hit("save", "before")
            orig_save()
            kp.hit("save", "after")

        def action(kind, dep, **kw):
            if (torn_action and kp.event == f"action:{kind}" and kp.when == "before" and not kp.fired
                    and kp.seen + 1 == kp.k):
                # power cut inside the append: a prefix of the record reached the disk, no newline, no fsync
                line = json.dumps({"t": 0, "action": kind, "dep": dep, **kw})
                with open(eng.actions_path, "a") as f:
                    f.write(line[: max(1, len(line) // 2)])
            kp.hit(f"action:{kind}", "before")
            orig_action(kind, dep, **kw)
            kp.hit(f"action:{kind}", "after")

        eng.save, eng.action = save, action
        self.exec.kill = kp
        fired = False
        self.fresh_boot = False
        try:
            for _ in range(max_ticks):
                eng.tick()
        except Crash:
            fired = True
        finally:
            self.exec.kill = None
        if not fired:
            eng.save, eng.action = orig_save, orig_action
            self.observe()
            return False
        if power_cut:
            self.exec.power_cut()
            h = self.data / "history.jsonl"
            if h.exists():  # history appends are flushed, not fsynced: the tail may be gone
                lines = h.read_text().splitlines(keepends=True)
                h.write_text("".join(lines[: len(lines) // 2]))
        st = self.disk_state()
        j = st.get("journal")
        acts, _ = O.read_actions(self.data / "actions.jsonl")
        self.crashes.append({"line": len(acts), "journal": (j["dep"], j["digest"]) if j else None,
                             "running": self.exec.running(), "kill": f"{kp.event}#{kp.k}:{kp.when}",
                             "powerCut": power_cut})
        self._observe_disk(st)
        self.engine = self._boot()
        self.fresh_boot = True
        return True

    def _check_order(self, wl):
        """Ordering invariant (EV-42): the executor starts a new release of a deployment (one the accepted desired
        state asks for and that is not its committed one) only while every dependency is committed at its desired
        release and healthy, as far as the engine knows. Rollbacks and repairs apply the committed/previous release
        and are not checked."""
        st = self.engine.state
        doc = st.get("desired") or {}
        want = {d["metadata"]["id"]: d for d in doc.get("deployments", [])}

        def unready(dep_id):
            return [e["deployment"] for e in want[dep_id].get("x-flex-dependsOn") or []
                    if not ((st["applied"].get(e["deployment"]) or {}).get("wl", {}).get("digest")
                            == _dg(want[e["deployment"]]) and st["health"].get(e["deployment"]))]

        def new_release(dep_id, digest):   # a desired release of dep_id that is not its committed one
            cur = st["applied"].get(dep_id)
            return dep_id in want and _dg(want[dep_id]) == digest and not (cur and cur["wl"]["digest"] == digest)

        if new_release(wl.dep_id, wl.digest) and unready(wl.dep_id):
            self.errs.append(f"{wl.dep_id} {wl.version} applied while its dependencies {unready(wl.dep_id)} are not "
                             "ready")
        # nothing else runs an uncommitted new release ahead of its dependencies meanwhile (e.g. one an interrupted
        # install left behind, EV-13 crash-resume)
        for x, u in list(self.exec.units.items()):
            if x != wl.dep_id and u["running"] and new_release(x, u["digest"]) and unready(x):
                self.errs.append(f"{x} {u['version']} runs ahead of its dependencies {unready(x)} while "
                                 f"{wl.dep_id} {wl.version} is applied")

    # ---- final invariants -------------------------------------------------------------------------------
    def converged(self) -> list[str]:
        st = self.engine.state
        errs: list[str] = []
        applied = {d: r["wl"]["digest"] for d, r in st["applied"].items()}
        units = {d: u["digest"] for d, u in self.exec.units.items()}
        j = st.get("journal")
        O.check(j is None, f"journal left open: {j}", errs)
        O.check(applied == units, f"applied {applied} != executor {units}", errs)
        O.check(all(u["running"] for u in self.exec.units.values()), "a committed workload is not running", errs)
        committed = {d: g for d, g in O.committed_from_reports(st["outbox"]).items() if d in applied or d in units}
        O.check(committed == applied, f"reported {committed} != applied {applied}", errs)
        doc = st["desired"]
        if doc and (st.get("rejected") or {}).get("manifestVersion") != doc["manifestVersion"]:
            want = {d["metadata"]["id"]: d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]
                    for d in doc["deployments"]}
            deps = {d["metadata"]["id"]: [e["deployment"] for e in d.get("x-flex-dependsOn") or []]
                    for d in doc["deployments"]}

            def ready(dep, seen=()):   # good desired release whose dependencies are all ready (EV-42)
                return DIGEST_KIND[want[dep]] == "good" and all(ready(x, seen + (dep,)) for x in deps[dep]
                                                                 if x not in seen)
            for dep, dg in want.items():
                if DIGEST_KIND[dg] == "good" and not ready(dep):
                    pass   # held behind a dependency that is not ready: keeps its previous release (or none)
                elif DIGEST_KIND[dg] == "good":
                    O.check(applied.get(dep) == dg, f"{dep}: desired good {dg[:15]} not applied ({applied.get(dep)})",
                            errs)
                else:
                    O.check(applied.get(dep) != dg, f"{dep}: bad release {dg[:15]} left committed", errs)
            for dep in applied:
                O.check(dep in want, f"{dep}: still applied but absent from the desired state", errs)
        return errs

    def settle(self, bound: int = 8) -> list[str]:
        """Eventual convergence within `bound` undisturbed ticks."""
        errs = []
        for _ in range(bound):
            self.engine.tick()
            errs = self.converged()
            if not errs:
                break
        self.observe()
        return errs

    def final_check(self):
        unsettled = self.settle()
        errs = list(self.errs)   # after settle(): what its ticks observed counts too
        errs += [f"not converged within bound: {e}" for e in unsettled]
        acts, bad = O.read_actions(self.data / "actions.jsonl")
        errs += [f"unparseable action record (fsynced records must survive): {b[:80]!r}" for b in bad]
        errs += O.duplicate_applies(acts, self.crashes)
        O.raise_if(errs, f"crashes={self.crashes}\nactions={[(a['action'], a['dep'], a.get('version')) for a in acts]}")

    def close(self):
        try:
            O.record_stats("inproc", self.crashes, O.read_actions(self.data / "actions.jsonl")[0])
        except OSError:
            pass
        shutil.rmtree(self.root, ignore_errors=True)


def _dg(d: dict) -> str:
    return d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]


class quiet_logs:
    """Send the engine's structured log to /dev/null while hypothesis runs thousands of ticks."""

    def __enter__(self):
        self._old = dict(flog._cfg)
        self._f = open(os.devnull, "w")
        flog._cfg["stream"] = self._f
        return self

    def __exit__(self, *a):
        flog._cfg.update(self._old)
        self._f.close()

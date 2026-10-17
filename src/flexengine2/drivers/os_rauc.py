"""flexEngine driver `os-rauc` (EV-17, V-34; spec: docs/probes/p2-report.md §10.2): whole-OS A/B updates with RAUC
and a health-gated commit.

Payload: one signed RAUC bundle (`.raucb`) as the layer `application/vnd.flex.payload.os-rauc.v1+raucb`, annotated
`dev.flex.profile: os-rauc` and `dev.flex.compatible: <RAUC compatible>`. The engine's release signature covers the
manifest (and so the bundle's sha256); RAUC verifies the bundle's CMS signature again with its own keyring.
One OS image per device: the driver is exclusive (`maxDeployments: 1`).

SPI mapping
  platform  {"dev.flex.compatible": <rauc status compatible>}: a release with one bundle per board picks the match
  preflight engine data dir under `sharedDataPath` (state.json, outbox and this driver's state must survive the slot
            switch), annotation compatible = `rauc status` compatible, `rauc info` verifies the bundle (keyring)
  apply     `rauc install <bundle>` into the inactive slot, record {dep, digest, version, slot, bootId} in
            <dataDir>/os-rauc/state.json, run `rebootCommand` (default `systemctl reboot`) and wait: the reboot ends
            the engine (systemd stops it). After the boot the engine starts again on the new slot and finds the
            journal entry of this install: status() reports the new slot's digest as running, the reconciler ADOPTS
            it (no second apply) and runs the health gate (minSeconds soak counts from the new boot).
            Simulated reboots (tests) let the calling process survive: apply returns once the boot id changed and
            the target slot is booted. A boot into the *other* slot (the bootloader fell back) raises DriverError
            (fail fast, no gate). Rollback without re-transfer: apply(failed, previous) with `previous` still in the
            other slot = `rauc status mark-bad booted` + `mark-active other` + reboot.
  status    booted slot (`rauc status --detailed --output-format=json`) -> the digest this driver installed there
            (cross-checked with RAUC's per-slot bundle hash); a foreign/factory image = not present
  health    the booted slot is the target slot, its boot status is not bad, the release's http probe (if any) and
            the device's `healthChecks` commands pass. "All other workloads healthy" is the reconciler's part: it
            gates every deployment, and an OS image whose apps fail their gates shows up as their Failed reports
  commit    `rauc status mark-good booted` (after the passed gate)
  failed    the boot after the install went to another slot (bootloader fell back: image did not boot or boot
            counter exhausted), or the booted target slot is marked bad
  supervise once per boot, `mark-good` the committed slot again (GRUB-style boot counters count every boot)
  remove    stop managing the OS; an image that was never committed is rolled back first (mark-bad + reboot into
            the slot it came from)

A reboot that kills the engine between the rollback request and the Failed report is covered by the state file:
the next apply of the rolled-back digest raises the recorded reason once (the reconciler then reports Failed with
rolledBackTo), so a bad image is never installed twice.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload, http_probe
from flexengine2.util import atomic_write_json, read_json

MT_PAYLOAD_RAUC = "application/vnd.flex.payload.os-rauc.v1+raucb"
ANN_COMPATIBLE = "dev.flex.compatible"
BOOT_ID = "/proc/sys/kernel/random/boot_id"


class RaucUnavailable(DriverError):
    """The rauc binary (or the reboot command) cannot be executed at all (capabilities: available false)."""


@dataclass
class RaucStatus:
    compatible: str
    booted: str | None                     # slot name (rootfs.0), not the bootname
    primary: str | None
    slots: dict = field(default_factory=dict)

    def slot(self, name: str | None) -> dict:
        return self.slots.get(name) or {}

    def other(self, name: str | None) -> str | None:
        cls = self.slot(name).get("class")
        for n, d in sorted(self.slots.items()):
            if n != name and d.get("class") == cls and not d.get("parent"):
                return n
        return None

    def bundle_hash(self, name: str | None) -> str | None:
        return ((self.slot(name).get("slot_status") or {}).get("bundle") or {}).get("hash")

    def boot_status(self, name: str | None) -> str | None:
        return self.slot(name).get("boot_status")


def parse_status(text: str) -> RaucStatus:
    doc = json.loads(text)
    slots = {}
    for item in doc.get("slots") or []:
        for name, d in item.items():
            slots[name] = d
    booted = next((n for n, d in slots.items() if d.get("state") == "booted"), None)
    if booted is None and doc.get("booted"):
        booted = next((n for n, d in slots.items() if d.get("bootname") == doc["booted"]), None)
    return RaucStatus(compatible=doc.get("compatible") or "", booted=booted, primary=doc.get("boot_primary"),
                      slots=slots)


class OsRaucDriver(Driver):
    profile = "os-rauc"
    name = "rauc"
    exclusive = True            # one OS image per device
    max_deployments = 1
    reboots_on_apply = True     # the reconciler journals a rollback first (the reboot ends the engine)
    repairs_digest_drift = False   # after a bootloader fall-back the other slot runs: no re-install loop

    def __init__(self, device_id: str, data_dir: Path, cfg: dict | None = None):
        cfg = cfg or {}
        self.device = device_id
        self.data_dir = Path(data_dir)
        self.rauc = cfg.get("rauc", "rauc")
        self.conf = cfg.get("conf")
        self.keyring = cfg.get("keyring")
        self.reboot_cmd = list(cfg.get("rebootCommand") or ["systemctl", "reboot"])
        self.shared = cfg.get("sharedDataPath")
        self.boot_timeout = float(cfg.get("bootTimeoutSeconds", 600))
        self.install_timeout = float(cfg.get("installTimeoutSeconds", 1800))
        self.cli_timeout = float(cfg.get("cliTimeout", 60))
        self.boot_id_file = cfg.get("bootIdFile", BOOT_ID)
        self.health_checks = [list(c) for c in cfg.get("healthChecks") or []]
        self.root = self.data_dir / "os-rauc"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self._infos: dict = {}      # verified `rauc info` per payload file (path, size, mtime)

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        s = read_json(self.state_path, None) or {}
        s.setdefault("dep", None)
        s.setdefault("slots", {})
        s.setdefault("target", None)
        s.setdefault("committed", None)
        s.setdefault("failedDigests", {})
        s.setdefault("confirmedBootId", None)
        return s

    def _save(self, s: dict) -> None:
        atomic_write_json(self.state_path, s)

    def _exec(self, argv: list[str], timeout: float) -> str:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise RaucUnavailable(f"os-rauc: cannot execute {argv[0]} (not found: {e.strerror})") from e
        except subprocess.TimeoutExpired as e:
            raise DriverError(f"os-rauc: {' '.join(argv[:3])} gave no answer within {timeout:g} s") from e
        except OSError as e:
            raise RaucUnavailable(f"os-rauc: cannot execute {argv[0]} ({e})") from e
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip().splitlines()
            last = err[-1] if err else "no output"
            raise DriverError(f"os-rauc: {' '.join(argv[:3])} failed: {last} (exit {p.returncode})")
        return p.stdout

    def _rauc(self, *args: str, timeout: float | None = None) -> str:
        argv = [self.rauc] + ([f"--conf={self.conf}"] if self.conf else []) + list(args)
        return self._exec(argv, self.cli_timeout if timeout is None else timeout)

    def _status(self) -> RaucStatus:
        out = self._rauc("status", "--detailed", "--output-format=json")
        try:
            return parse_status(out)
        except (ValueError, AttributeError, TypeError) as e:
            raise DriverError(f"rauc status: unreadable output ({e})") from e

    def _boot_id(self) -> str | None:
        try:
            return Path(self.boot_id_file).read_text().strip() or None
        except OSError:
            return None

    @staticmethod
    def _holds(s: dict, st: RaucStatus, slot: str | None, digest: str) -> bool:
        """The slot carries the bundle this driver installed for `digest` (and RAUC still says so)."""
        rec = s["slots"].get(slot or "")
        if not rec or rec.get("digest") != digest:
            return False
        have = st.bundle_hash(slot)
        return have is None or not rec.get("hash") or have == rec["hash"]

    def _bundle_link(self, wl: Workload) -> str:
        """RAUC wants a bundle file name; the payload sits in the engine's CAS without one."""
        d = self.root / "bundles"
        d.mkdir(parents=True, exist_ok=True)
        link = d / f"{wl.digest.split(':', 1)[-1][:32]}.raucb"
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(Path(wl.payload_path).resolve())
        except OSError:
            return wl.payload_path
        return str(link)

    @staticmethod
    def _info_key(path: str):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (str(path), st.st_size, st.st_mtime_ns)

    def _on_shared(self) -> bool:
        """The engine data dir (state.json, outbox, CAS, this driver's state) lies under sharedDataPath."""
        if not self.shared:
            return False
        data, shared = self.data_dir.resolve(), Path(self.shared).resolve()
        return data == shared or shared in data.parents

    def _exclusive(self, s: dict, dep_id: str) -> None:
        if s["dep"] and s["dep"] != dep_id:
            raise DriverError(f"one OS image per device: deployment {s['dep']} already owns the OS")

    # ---- SPI ------------------------------------------------------------------------------------------------
    def platform(self) -> dict | None:
        return {ANN_COMPATIBLE: self._status().compatible}

    def preflight(self, release, dep: dict) -> str | None:
        if not self.shared:
            return ("os-rauc needs sharedDataPath (the data partition both slots mount) in its driver config: "
                    "without it manifestVersion and the outbox would not survive the slot switch")
        if not self._on_shared():
            return (f"engine data dir {self.data_dir.resolve()} is not on the shared data partition "
                    f"{Path(self.shared).resolve()} (sharedDataPath): manifestVersion and the outbox would not "
                    "survive the slot switch")
        st = self._status()                          # DriverError: unreachable -> deferred
        want = (release.payload_annotations or {}).get(ANN_COMPATIBLE)
        if not want:
            return "os-rauc payload lacks the dev.flex.compatible annotation"
        if want != st.compatible:
            return f"bundle is for compatible {want}, this device is {st.compatible}"
        args = ["info", "--output-format=json"] + ([f"--keyring={self.keyring}"] if self.keyring else [])
        try:
            key = self._info_key(release.payload_path)
            info = self._infos.get(key) if key else None
            if info is None:     # `rauc info` hashes the whole bundle: once per payload, not every tick
                info = json.loads(self._rauc(*args, release.payload_path, timeout=self.install_timeout))
                if key:
                    self._infos[key] = info
        except RaucUnavailable:
            raise
        except DriverError as e:
            if "gave no answer within" in str(e):
                raise
            return f"rauc info refused the bundle: {e}"
        except ValueError as e:
            return f"rauc info refused the bundle: unreadable output ({e})"
        if info.get("compatible") != want:
            return f"bundle manifest says compatible {info.get('compatible')}, its annotation says {want}"
        return None

    def plan(self, current: Workload | None, desired: Workload | None) -> list[str]:
        if desired is None:
            return ["stop managing the OS image (the booted slot keeps running)"]
        s = self._load()
        try:
            st = self._status()
        except DriverError:
            st = None
        if st and self._holds(s, st, st.booted, desired.digest):
            return [f"{desired.version} already runs in slot {st.booted}: nothing to install"]
        if st and self._holds(s, st, st.other(st.booted), desired.digest):
            return [f"activate slot {st.other(st.booted)} ({desired.version}, still installed: no transfer)",
                    f"reboot ({' '.join(self.reboot_cmd)})", "after the boot: health gate, then rauc status mark-good"]
        return [f"rauc install {desired.version} into the inactive slot (RAUC verifies the bundle signature)",
                f"reboot ({' '.join(self.reboot_cmd)})",
                "after the boot: health gate, then rauc status mark-good (no mark-good: the bootloader falls back)"]

    def apply(self, current: Workload | None, desired: Workload) -> None:
        s = self._load()
        self._exclusive(s, desired.dep_id)
        st = self._status()
        booted, bid = st.booted, self._boot_id()
        t = s["target"]
        if t and t.get("phase") == "rebooting" and t.get("bootId") != bid:
            t["phase"] = "booted" if booted == t.get("slot") else "fell-back"   # a reboot happened since the request
            t["bootedSlot"] = booted
            self._save(s)
        # 1. desired already runs (idempotent apply, adopt-like re-apply, rollback to the booted slot)
        if self._holds(s, st, booted, desired.digest):
            if st.primary != booted:
                self._rauc("status", "mark-active", "booted")     # cancel a switch that did not happen
            s["dep"] = desired.dep_id
            same = bool(t) and t.get("digest") == desired.digest and t.get("slot") == booted
            s["target"] = {"slot": booted, "digest": desired.digest, "version": desired.version, "phase": "booted",
                           "bootId": bid, "at": time.time(), "fromSlot": t.get("fromSlot") if same else None}
            self._save(s)
            return
        # 2. desired did not come up (bootloader fell back), or was rolled back while the engine was down
        if t and t.get("digest") == desired.digest and t.get("phase") == "fell-back":
            reason = (f"bootloader fell back to slot {booted}: {desired.version} in slot {t.get('slot')} did not "
                      "boot or was not committed")
            s["target"] = {**t, "phase": "failed", "reason": reason}
            s["failedDigests"].pop(desired.digest, None)
            self._save(s)
            raise DriverError(reason)
        recorded = s["failedDigests"].pop(desired.digest, None)
        if recorded:
            self._save(s)
            raise DriverError(recorded["reason"])
        s["dep"] = desired.dep_id
        # 3. the reboot we asked for has not happened yet (engine restarted in the same boot): ask again
        if t and t.get("digest") == desired.digest and t.get("phase") == "rebooting" and t.get("slot") != booted \
                and st.primary == t.get("slot") and self._holds(s, st, t.get("slot"), desired.digest):
            self._save(s)
            return self._reboot_and_wait(s, booted)
        # 4. desired is still installed in the other slot: activate it, no transfer
        oth = st.other(booted)
        if oth and self._holds(s, st, oth, desired.digest):
            com = s["committed"] or {}
            rollback = current is not None and self._holds(s, st, booted, current.digest) and not (
                com.get("slot") == booted and com.get("digest") == current.digest)
            if rollback:
                self._rauc("status", "mark-bad", "booted")
                s["failedDigests"][current.digest] = {"at": time.time(), "reason": (
                    f"{current.version} was rolled back: rebooted from slot {booted} into slot {oth} "
                    f"({desired.version}) after its health gate failed")}
            self._rauc("status", "mark-active", oth)
            s["target"] = {"slot": oth, "digest": desired.digest, "version": desired.version, "phase": "rebooting",
                           "bootId": bid, "at": time.time(), "fromSlot": booted,
                           "cause": "rollback" if rollback else "activate"}
            self._save(s)
            return self._reboot_and_wait(s, booted)
        # 5. install into the inactive slot
        s["target"] = {"slot": oth, "digest": desired.digest, "version": desired.version, "phase": "installing",
                       "bootId": bid, "at": time.time(), "fromSlot": booted, "cause": "install"}
        self._save(s)
        try:
            self._rauc("install", self._bundle_link(desired), timeout=self.install_timeout)
            st2 = self._status()
        except DriverError:
            s["target"]["phase"] = "install-failed"
            self._save(s)
            raise
        slot = st2.primary
        if not slot or slot == booted:
            s["target"]["phase"] = "install-failed"
            self._save(s)
            raise DriverError(f"rauc install did not activate another slot (primary {slot}, booted {booted})")
        s["slots"][slot] = {"digest": desired.digest, "version": desired.version, "hash": st2.bundle_hash(slot),
                            "installedAt": time.time()}
        s["target"].update(slot=slot, phase="rebooting", installedAt=time.time())
        self._save(s)
        return self._reboot_and_wait(s, booted)

    def _reboot_and_wait(self, s: dict, booted: str | None) -> None:
        """Request the reboot and wait for it: on a device the engine is stopped by it (the restarted engine adopts
        the booted target); a simulated reboot lets this process see the new boot id and continue."""
        t = s["target"]
        try:
            self._exec(self.reboot_cmd, self.cli_timeout)
        except DriverError as e:
            self._revert(booted)
            t["phase"] = "reboot-failed"
            self._save(s)
            raise DriverError(f"reboot request failed: {e}") from e
        end = time.monotonic() + self.boot_timeout
        while time.monotonic() < end:
            time.sleep(0.2)
            bid = self._boot_id()
            if not bid or bid == t.get("bootId"):
                continue
            try:
                st = self._status()
            except DriverError:
                continue            # rauc service not up yet after the (simulated) boot
            t["bootedSlot"] = st.booted
            if st.booted == t["slot"]:
                t["phase"] = "booted"
                self._save(s)
                return
            reason = (f"bootloader fell back to slot {st.booted}: {t['version']} in slot {t['slot']} did not "
                      "boot or was not committed")
            t.update(phase="failed", reason=reason)
            s["failedDigests"].pop(t["digest"], None)
            self._save(s)
            raise DriverError(reason)
        self._revert(booted)
        t["phase"] = "reboot-timeout"
        self._save(s)
        raise DriverError(f"no reboot within {self.boot_timeout:g} s after the request ({' '.join(self.reboot_cmd)}); "
                          f"slot {booted} stays active")

    def _revert(self, booted: str | None) -> None:
        try:
            self._rauc("status", "mark-active", "booted")
        except DriverError:
            pass

    def remove(self, dep_id: str) -> None:
        s = self._load()
        if s["dep"] != dep_id:
            return
        t, com = s["target"] or {}, s["committed"] or {}
        try:
            st = self._status()
        except DriverError:
            st = None
        uncommitted = bool(t) and st is not None and st.booted == t.get("slot") and not (
            com.get("slot") == t.get("slot") and com.get("digest") == t.get("digest"))
        back = t.get("fromSlot") if uncommitted else None
        fresh = {"dep": None, "slots": {}, "target": None, "committed": None, "confirmedBootId": None,
                 "failedDigests": s["failedDigests"]}
        if not back or back == st.booted or back not in st.slots:
            self._save(fresh)
            return
        # never committed: go back to the slot it was installed from (the bootloader would, at the next reboot)
        fresh["failedDigests"][t["digest"]] = {"at": time.time(), "reason": (
            f"{t.get('version')} was removed before it was committed: rebooted from slot {st.booted} into slot {back}")}
        self._rauc("status", "mark-bad", "booted")
        self._rauc("status", "mark-active", back)
        fresh["target"] = {"slot": back, "digest": None, "version": None, "phase": "rebooting",
                           "bootId": self._boot_id(), "at": time.time(), "fromSlot": st.booted, "cause": "remove"}
        self._save(fresh)
        try:
            self._exec(self.reboot_cmd, self.cli_timeout)
        except DriverError as e:
            raise DriverError(f"reboot request failed: {e}") from e
        end = time.monotonic() + self.boot_timeout
        while time.monotonic() < end and self._boot_id() == fresh["target"]["bootId"]:
            time.sleep(0.2)
        fresh["target"] = None
        self._save(fresh)

    def status(self, dep_id: str) -> Observed:
        s = self._load()
        if s["dep"] != dep_id:
            return Observed(present=False)
        try:
            st = self._status()
        except DriverError as e:
            t = s["target"] or {}
            return Observed(present=True, running=False, digest=t.get("digest"), version=t.get("version"),
                            detail=f"executor unreachable: {e}")
        rec = s["slots"].get(st.booted or "")
        if rec and self._holds(s, st, st.booted, rec["digest"]):
            return Observed(present=True, running=True, digest=rec["digest"], version=rec["version"],
                            detail=f"slot {st.booted} booted (boot status {st.boot_status(st.booted)})")
        return Observed(present=False, detail=f"booted slot {st.booted} runs no OS image this engine installed")

    def list(self) -> dict[str, Observed]:
        s = self._load()
        return {s["dep"]: self.status(s["dep"])} if s["dep"] else {}

    def health(self, dep_id: str, probe: dict) -> bool:
        s = self._load()
        t = s["target"]
        if s["dep"] != dep_id or not t:
            return False
        try:
            st = self._status()
        except DriverError:
            return False
        if st.booted != t.get("slot") or st.boot_status(st.booted) == "bad":
            return False
        if not self._holds(s, st, st.booted, t.get("digest")):
            return False
        probe = probe or {}
        if probe.get("port"):          # optional http probe of a local service of the new OS
            try:
                port = int(probe["port"])
            except (TypeError, ValueError):
                return False
            if not http_probe("127.0.0.1", port, probe.get("path", "/healthz"), timeout=2.0):
                return False
        for cmd in self.health_checks:
            try:
                if subprocess.run(cmd, capture_output=True, timeout=30).returncode != 0:
                    return False
            except (OSError, subprocess.TimeoutExpired):
                return False
        return True

    def failed(self, dep_id: str) -> str | None:
        s = self._load()
        t = s["target"]
        if s["dep"] != dep_id or not t:
            return None
        if t.get("phase") == "failed" and t.get("reason"):
            return t["reason"]
        try:
            st = self._status()
        except DriverError:
            return None
        if t.get("phase") in ("rebooting", "booted") and t.get("bootId") != self._boot_id() \
                and st.booted != t.get("slot"):
            return (f"bootloader fell back to slot {st.booted}: {t.get('version')} in slot {t.get('slot')} did not "
                    "boot or was not committed")
        if st.booted == t.get("slot") and st.boot_status(st.booted) == "bad":
            return f"booted slot {st.booted} is marked bad"
        return None

    def commit(self, dep_id: str) -> None:
        s = self._load()
        t = s["target"]
        st = self._status()
        if s["dep"] != dep_id or not t or st.booted != t.get("slot"):
            raise DriverError(f"booted slot {st.booted} is not the target slot {(t or {}).get('slot')}: not marking it good")
        self._rauc("status", "mark-good", "booted")
        t["phase"] = "committed"
        s["committed"] = {"slot": t["slot"], "digest": t["digest"], "version": t["version"], "at": time.time()}
        s["confirmedBootId"] = self._boot_id()
        s["failedDigests"] = {}
        self._save(s)

    def supervise(self) -> list[dict]:
        """Once per boot: the committed slot is marked good again (boot counters count every boot)."""
        try:
            s = self._load()
            com = s["committed"]
            if not com or not s["dep"]:
                return []
            bid = self._boot_id()
            if not bid or bid == s.get("confirmedBootId"):
                return []
            st = self._status()
            if st.booted != com["slot"] or (s["target"] or {}).get("slot") not in (None, com["slot"]):
                return []
            self._rauc("status", "mark-good", "booted")
            s["confirmedBootId"] = bid
            self._save(s)
            return [{"dep": s["dep"], "event": "boot-confirmed", "slot": com["slot"], "version": com["version"]}]
        except (DriverError, OSError, ValueError, KeyError):
            return []

    def capabilities(self) -> dict:
        frag = {"profile": self.profile, "driver": self.name, **self.placement()}
        if self.shared:
            frag["sharedDataPath"] = str(self.shared)
        frag["dataDirOnSharedPartition"] = self._on_shared()
        try:
            st = self._status()
        except RaucUnavailable as e:
            return {**frag, "available": False, "detail": str(e)}
        except DriverError as e:
            return {**frag, "reachable": False, "detail": str(e)}
        except Exception as e:  # noqa: BLE001 - capabilities never raise
            return {**frag, "reachable": False, "detail": f"{type(e).__name__}: {e}"}
        frag.update(compatible=st.compatible, booted=st.booted, bootPrimary=st.primary,
                    slots=[{"name": n, "bootname": d.get("bootname"), "class": d.get("class"), "state": d.get("state"),
                            "bootStatus": d.get("boot_status"),
                            "version": ((d.get("slot_status") or {}).get("bundle") or {}).get("version")}
                           for n, d in sorted(st.slots.items())])
        return frag

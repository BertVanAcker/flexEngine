"""Minimal example driver for the flexEngineV2 driver SPI v1 (docs/spi-guide.md).

The executor is a spool directory that another program (the "player") watches, for example a kiosk player on a
mounted share. Per deployment the driver writes ``<spool>/<dep>/payloads/<digest>`` and ``<spool>/<dep>/meta.json``;
the player answers in ``<spool>/<dep>/state.json`` with ``{"state": "running" | "failed", "digest", "detail"}``.
When the spool directory is missing (share not mounted) the executor is unreachable.

The driver passes the shared conformance suite (tests/contract/test_spi.py, harness ``example-spool``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload


class SpoolDriver(Driver):
    profile = "spool"          # example profile: a real one is registered in CP-1 (DriverProfile) first
    name = "spool-dir"

    def __init__(self, device_id: str, spool_dir: str | Path, max_bytes: int = 1 << 20):
        self.device = device_id
        self.spool = Path(spool_dir)
        self.max_bytes = int(max_bytes)

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _reachable(self) -> bool:
        return self.spool.is_dir()

    def _dir(self, dep_id: str) -> Path:
        return self.spool / dep_id

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, doc: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, path)          # atomic: the player never reads half a file

    def _meta(self, dep_id: str) -> dict | None:
        meta = self._read(self._dir(dep_id) / "meta.json")
        return meta if meta and meta.get("device") == self.device else None

    # ---- required ------------------------------------------------------------------------------------------
    def plan(self, current: Workload | None, desired: Workload | None) -> list[str]:
        if desired is None:
            return [f"remove {current.dep_id} from {self.spool}"] if current else []
        return [f"copy payload {desired.digest[:19]} to {self._dir(desired.dep_id)}/payloads",
                f"point {desired.dep_id} at {desired.module} {desired.version}"]

    def apply(self, current: Workload | None, desired: Workload) -> None:
        if not self._reachable():
            raise DriverError(f"executor unreachable: spool dir {self.spool} missing")
        d = self._dir(desired.dep_id)
        payloads = d / "payloads"
        target = payloads / desired.digest.replace(":", "_")
        try:
            payloads.mkdir(parents=True, exist_ok=True)
            if not target.exists():    # idempotent, and a rollback reuses the payload kept from before
                shutil.copyfile(desired.payload_path, target.with_suffix(".part"))
                os.replace(target.with_suffix(".part"), target)
            meta = {"device": self.device, "dep": desired.dep_id, "module": desired.module,
                    "version": desired.version, "digest": desired.digest, "payload": target.name}
            if self._read(d / "meta.json") != meta:
                self._write(d / "meta.json", meta)
        except OSError as e:
            raise DriverError(f"spool write failed: {e}") from e

    def remove(self, dep_id: str) -> None:
        if not self._reachable():
            raise DriverError(f"executor unreachable: spool dir {self.spool} missing")
        if self._meta(dep_id) is not None:
            shutil.rmtree(self._dir(dep_id), ignore_errors=True)

    def status(self, dep_id: str) -> Observed:
        if not self._reachable():
            return Observed(present=True, running=False, detail=f"executor unreachable: spool dir {self.spool} missing")
        meta = self._meta(dep_id)
        if meta is None:
            return Observed(present=False)
        st = self._read(self._dir(dep_id) / "state.json") or {}
        running = st.get("state") == "running" and st.get("digest") == meta["digest"]
        return Observed(present=True, running=running, digest=meta["digest"], version=meta["version"],
                        detail=str(st.get("detail", "")))

    def list(self) -> dict[str, Observed]:
        if not self._reachable():
            return {}
        try:
            deps = sorted(p.name for p in self.spool.iterdir() if p.is_dir())
        except OSError:
            return {}
        return {dep: self.status(dep) for dep in deps if self._meta(dep) is not None}

    def health(self, dep_id: str, probe: dict) -> bool:
        return self.status(dep_id).running

    def capabilities(self) -> dict:
        return {**super().capabilities(), "spoolDir": str(self.spool), "reachable": self._reachable()}

    # ---- optional ------------------------------------------------------------------------------------------
    def failed(self, dep_id: str) -> str | None:
        if not self._reachable():
            return None                # unreachable is "not known", never "failed"
        meta = self._meta(dep_id)
        st = self._read(self._dir(dep_id) / "state.json") or {}
        if meta and st.get("state") == "failed" and st.get("digest") == meta["digest"]:
            return f"spool player failed: {st.get('detail') or 'no detail'}"
        return None

    def preflight(self, release, dep: dict) -> str | None:
        # checks on the verified payload only: nothing reaches the executor, so it never defers
        try:
            size = os.path.getsize(release.payload_path)
        except OSError:
            return "spool payload missing"
        if size == 0:
            return "spool payload is empty"
        if size > self.max_bytes:
            return f"spool payload of {size} B exceeds {self.max_bytes} B"
        return None

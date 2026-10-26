"""Device state file (`<dataDir>/state.json`): schema version, migration, corruption handling (EV-05, plan §3.5).

    state = load(path, device)      # fresh state when the file is absent; migrates v0 (probe format) in memory
    save(path, state)               # atomic write + fsync of the file and its directory (util.atomic_write_json)

schemaVersion 1 = the probe P2 layout plus `schemaVersion` (and the keys later probes added: notices, journal,
health, traceparents). A file without `schemaVersion` is v0 (written by the probes / pre-EV-05 engines) and is
migrated in memory; it is written back as v1 on the next save. A file that cannot be parsed, is not a JSON object or
lacks the core keys is CORRUPT: a copy is kept as `state.json.corrupt-<unix ts>` (the original stays in place, so
every restart refuses again) and `StateCorrupt` is raised. The engine then refuses to run that device until an
operator repairs it (`flexengine doctor --repair`, EV-07). A schemaVersion newer than this engine knows is refused
with `StateTooNew` (a downgrade of the engine must not silently drop state).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from flexengine2.util import atomic_write_json

SCHEMA_VERSION = 1
CORE_KEYS = ("deviceId", "manifestVersion", "applied", "outbox", "seq")


class StateError(RuntimeError):
    """The device's state.json cannot be used; the device is not run."""

    def __init__(self, msg: str, path: Path | None = None, kept: Path | None = None):
        super().__init__(msg)
        self.path, self.kept = path, kept


class StateCorrupt(StateError):
    """state.json is unreadable / not a state document."""


class StateTooNew(StateError):
    """state.json was written by a newer engine (schemaVersion > SCHEMA_VERSION)."""


def fresh(device: str) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "deviceId": device, "onboarded": False, "etag": None,
            "manifestVersion": 0, "desired": None, "applied": {}, "failed": {}, "rejected": None, "outbox": [],
            "seq": 0, "notices": {}, "journal": None, "health": {}, "traceparents": {}}


def migrate(doc: dict, device: str) -> dict:
    """v0 (no schemaVersion) -> v1, in place; returns doc."""
    base = fresh(device)
    for k, v in base.items():
        if k not in doc:
            doc[k] = v
    doc["schemaVersion"] = SCHEMA_VERSION
    return doc


def _keep_copy(path: Path) -> Path:
    """Copy the bad file aside once per distinct content (restarts do not pile up identical copies)."""
    data = path.read_bytes()
    for old in sorted(path.parent.glob(f"{path.name}.corrupt-*")):
        try:
            if old.read_bytes() == data:
                return old
        except OSError:
            continue
    dst = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    n = 1
    while dst.exists():
        dst = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{n}")
        n += 1
    shutil.copy2(path, dst)
    return dst


def _corrupt(path: Path, device: str, why: str) -> StateCorrupt:
    kept = None
    try:
        kept = _keep_copy(path)
    except OSError:
        pass
    return StateCorrupt(
        f"device {device}: {path} is corrupt ({why}); kept a copy as {kept.name if kept else '(copy failed)'}. "
        f"The device is not run until the state is repaired: `flexengine doctor --repair` (restores a consistent "
        f"state; re-onboarding may need a new token), or restore state.json from a backup.", path, kept)


def load(path: str | Path, device: str) -> dict:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return fresh(device)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _corrupt(path, device, f"not valid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise _corrupt(path, device, f"not a JSON object but {type(doc).__name__}")
    missing = [k for k in CORE_KEYS if k not in doc]
    if missing:
        raise _corrupt(path, device, f"missing {', '.join(missing)}")
    if not isinstance(doc["applied"], dict) or not isinstance(doc["outbox"], list):
        raise _corrupt(path, device, "applied/outbox have the wrong type")
    if doc.get("deviceId") != device:
        raise _corrupt(path, device, f"belongs to device {doc.get('deviceId')!r}")
    v = doc.get("schemaVersion", 0)
    if not isinstance(v, int) or v < 0:
        raise _corrupt(path, device, f"bad schemaVersion {v!r}")
    if v > SCHEMA_VERSION:
        raise StateTooNew(f"device {device}: {path} has schemaVersion {v}, this engine knows {SCHEMA_VERSION}; "
                          f"run the newer engine (or restore a backup)", path)
    return migrate(doc, device)


def save(path: str | Path, state: dict) -> None:
    atomic_write_json(path, state)

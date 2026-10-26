"""Shared helpers of the contract tests (imported by the test modules and conftest.py)."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path

from flexengine2.drivers.base import Driver, Observed
from flexengine2.util import sha256_digest

HERE = Path(__file__).resolve().parent
FLEXENGINE = HERE.parents[1]
REPO = FLEXENGINE.parent
GOLDEN = HERE / "golden"
API_DOCS = FLEXENGINE / "api"
VECTORS = API_DOCS / "vectors"
TOOLS = API_DOCS / "tools"
SRC = FLEXENGINE / "src" / "flexengine2"


def tool(name: str):
    """Import a script from flexengine/api/tools (reasonscan, gen_reason_codes, gen_vectors)."""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(f"_api_tool_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve(ref: str) -> Path:
    kind, _, rel = ref.partition(":")
    return {"golden": GOLDEN, "vectors": VECTORS}[kind] / rel


def load_ref(ref: str):
    return json.loads(resolve(ref).read_bytes())


def recorded(pattern: str) -> list[Path]:
    """Work-dir recordings of earlier runs (gitignored; absent on a fresh checkout)."""
    return sorted(p for p in REPO.glob(pattern) if ".venv" not in p.parts)


class FakeDriver(Driver):
    """In-memory executor with a real profile name (the schema only knows real profiles)."""

    def __init__(self, profile: str = "native", healthy: bool = True, fail_reason: str | None = None):
        self.profile, self.name = profile, "fake"
        self.running: dict[str, dict] = {}
        self.healthy, self.fail_reason = healthy, fail_reason
        self.calls: list[tuple] = []

    def plan(self, current, desired):
        return [f"fake apply {desired.version}"] if desired else [f"fake remove {current.dep_id}"]

    def apply(self, current, desired):
        self.calls.append(("apply", desired.dep_id, desired.version))
        self.running[desired.dep_id] = {"digest": desired.digest, "version": desired.version}

    def remove(self, dep_id):
        self.calls.append(("remove", dep_id))
        self.running.pop(dep_id, None)

    def status(self, dep_id):
        r = self.running.get(dep_id)
        return Observed(present=bool(r), running=bool(r), digest=(r or {}).get("digest"), version=(r or {}).get("version"))

    def list(self):
        return {k: self.status(k) for k in self.running}

    def health(self, dep_id, probe):
        return self.healthy and dep_id in self.running

    def failed(self, dep_id):
        return None if self.healthy else self.fail_reason


def bundle_layout_problems(data: bytes) -> list[str]:
    """bundle.v1alpha1 rules (bundle.v1alpha1.md section 2): member names, content addressing, closure."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}
        names = [m.name for m in tf.getmembers()]
    if names[:2] != ["desired.json", "desired.sig"]:
        out.append(f"first members must be desired.json, desired.sig: {names[:2]}")
    blobs = {}
    for n, b in members.items():
        if n in ("desired.json", "desired.sig"):
            continue
        m = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", n)
        if not m:
            out.append(f"unexpected member {n}")
        elif sha256_digest(b) != "sha256:" + m[1]:
            out.append(f"{n} does not hash to its name")
        else:
            blobs["sha256:" + m[1]] = b
    desired = json.loads(members["desired.json"])
    for d in desired["deployments"]:
        md = d["spec"]["deploymentProfile"]["components"][0]["properties"]["digest"]
        if md not in blobs:
            out.append(f"release manifest {md} missing")
            continue
        man = json.loads(blobs[md])
        sigs = [json.loads(b) for b in blobs.values() if b[:1] == b"{" and b'"subject"' in b]
        if not any((s.get("subject") or {}).get("digest") == md for s in sigs):
            out.append(f"no signature manifest for {md}")
        for b in [man["config"]["digest"]] + [l["digest"] for l in man["layers"]]:
            if b not in blobs:
                out.append(f"blob {b} of {md} missing")
    return out

"""The pinned flexNode-arduino desk-runbook tooling (runner `tools/hwtest/runbook.py` + browser GUI), fetched, never
vendored (plan 8.2 option (c), DL-7).

`upstream.lock.json` names the repository, the commits and the sha256 of every file used. `ensure()` returns a cache
directory laid out like the flexNode-arduino repository (tools/hwtest/runbook.py, tools/hwtest/gui.py,
tools/hwtest/hwgui/..., LICENSE.txt) with exactly those bytes:

  1. $FLEX_HWTEST_UPSTREAM: an already extracted tree (checked, never written)
  2. the cache flexengine/hwtest/.upstream/<lock hash>/ (git-ignored), when complete and matching
  3. else filled from a local checkout ($FLEXNODE_ARDUINO_REPO; `git archive <commit>`, the checkout is only read)
     or by a shallow fetch of each commit from the repository URL into a temporary bare repository.

Any sha256 mismatch is an error: the runbooks are only validated against the pinned runner.

    python flexengine/hwtest/upstream.py ensure            # fetch/check, print the directory
    python flexengine/hwtest/upstream.py pin --repo DIR    # recompute the file hashes for the commits in the lock
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "upstream.lock.json"
CACHE = HERE / ".upstream"


class UpstreamError(RuntimeError):
    pass


def lock() -> dict:
    return json.loads(LOCK.read_text())


def lock_id(doc: dict | None = None) -> str:
    doc = doc or lock()
    return hashlib.sha256(json.dumps(doc["sources"], sort_keys=True).encode()).hexdigest()[:16]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check(root: Path, doc: dict | None = None) -> list[str]:
    """Problems of an extracted tree against the lock (empty = OK)."""
    doc = doc or lock()
    bad = []
    for src in doc["sources"]:
        for rel, want in src["files"].items():
            p = root / rel
            if not p.is_file():
                bad.append(f"{rel}: missing")
            elif _sha(p) != want:
                bad.append(f"{rel}: sha256 differs from the pin ({src['commit'][:7]})")
    return bad


def _archive(git_dir: list[str], commit: str, paths: list[str]) -> bytes:
    r = subprocess.run(["git", *git_dir, "archive", "--format=tar", commit, "--", *paths], capture_output=True,
                       timeout=120)
    if r.returncode != 0:
        raise UpstreamError(f"git archive {commit[:7]}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _extract(data: bytes, dest: Path, wanted: set[str]) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name in wanted:
                out = dest / m.name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tf.extractfile(m).read())


def _from_checkout(repo: Path, src: dict, dest: Path) -> None:
    data = _archive(["-C", str(repo)], src["commit"], sorted(src["files"]))
    _extract(data, dest, set(src["files"]))


def _from_remote(url: str, src: dict, dest: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="flex-hwtest-fetch-") as tmp:
        g = ["--git-dir", tmp]
        subprocess.run(["git", "init", "-q", "--bare", tmp], check=True, capture_output=True, timeout=60)
        r = subprocess.run(["git", *g, "fetch", "-q", "--depth", "1", url, src["commit"]], capture_output=True,
                           timeout=300)
        if r.returncode != 0:
            raise UpstreamError(f"fetch {src['commit'][:7]} from {url}: {r.stderr.decode(errors='replace').strip()}")
        _extract(_archive(g, src["commit"], sorted(src["files"])), dest, set(src["files"]))


def ensure(quiet: bool = True) -> Path:
    doc = lock()
    given = os.environ.get("FLEX_HWTEST_UPSTREAM")
    if given:
        root = Path(given).resolve()
        bad = check(root, doc)
        if bad:
            raise UpstreamError(f"$FLEX_HWTEST_UPSTREAM={root} does not match {LOCK.name}: " + "; ".join(bad))
        return root
    root = CACHE / lock_id(doc)
    if root.is_dir() and not check(root, doc):
        return root
    tmp = Path(tempfile.mkdtemp(prefix="fill-", dir=str(CACHE) if CACHE.is_dir() else None))
    try:
        repo = os.environ.get("FLEXNODE_ARDUINO_REPO")
        for src in doc["sources"]:
            done = False
            if repo:
                try:
                    _from_checkout(Path(repo), src, tmp)
                    done = True
                except UpstreamError as e:
                    if not quiet:
                        print(f"local checkout: {e}; fetching from {doc['repository']}", file=sys.stderr)
            if not done:
                _from_remote(doc["repository"], src, tmp)
        bad = check(tmp, doc)
        if bad:
            raise UpstreamError("fetched files do not match the pin: " + "; ".join(bad))
        CACHE.mkdir(exist_ok=True)
        if root.exists():
            shutil.rmtree(root)
        tmp.rename(root)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    return root


def runner_dir() -> Path:
    return ensure() / "tools" / "hwtest"


def pin(repo: Path) -> dict:
    doc = lock()
    for src in doc["sources"]:
        with tempfile.TemporaryDirectory() as tmp:
            _from_checkout(repo, src, Path(tmp))
            src["files"] = {rel: _sha(Path(tmp) / rel) for rel in sorted(src["files"])}
    LOCK.write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure")
    p = sub.add_parser("pin")
    p.add_argument("--repo", required=True, type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "pin":
        pin(a.repo)
        print(f"updated {LOCK}")
        return 0
    try:
        print(ensure(quiet=False))
    except UpstreamError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

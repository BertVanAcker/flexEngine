"""Vendored subset of probe P4's flexp4.appgen (a re-export of flexmodel.applab.appgen): only the
function ardrelease.py imports, copied from flexmodel. See ../../SOURCES.json."""

from __future__ import annotations

from pathlib import Path


def source_hashes(app_dir: Path) -> dict:
    """Contract hashes found in the two generated sides of an app folder (static skew check)."""
    out = {}
    py = (app_dir / "python" / "flex_bridge.py")
    h = (app_dir / "sketch" / "flex_bridge.h")
    if py.exists():
        for line in py.read_text().splitlines():
            if line.startswith("CONTRACT_HASH = "):
                out["python"] = line.split('"')[1]
    if h.exists():
        for line in h.read_text().splitlines():
            if "CONTRACT_HASH[] = " in line:
                out["sketch"] = line.split('"')[1]
    return out

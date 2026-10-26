#!/usr/bin/env python3
"""Regenerate packaging/requirements-lock.txt from a working venv (pip-compile-free).

    .venv/bin/python packaging/gen-lock.py [--extras mcu,applab,plc,otel] [--check]

Walks the installed metadata of `flexEngine` (+ the extras) recursively, keeps exactly the distributions that the
base package and those extras need in *this* environment, and writes them as `name==version` pins (`pip freeze`
filtered to the closure). The lock is used by install.sh and build-wheels.sh as a pip **constraints** file (`-c`),
so a dependency that only another Python/platform needs (an environment marker false here) is still resolved there,
just not pinned. Regenerate after changing pyproject.toml dependencies or upgrading the dev venv, then re-run
build-wheels.sh and the packaging tests. `--check` exits 1 when the file on disk differs.
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

ROOT_DIST = "flexEngine"
DEFAULT_EXTRAS = "mcu,applab,plc,otel"   # relay needs the repo's flexmodel (not on PyPI); dev is not shipped
LOCK = Path(__file__).with_name("requirements-lock.txt")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(extras: list[str]) -> dict[str, str]:
    seen: dict[str, str] = {}
    todo: list[tuple[str, frozenset[str]]] = [(ROOT_DIST, frozenset(extras))]
    done: set[tuple[str, frozenset[str]]] = set()
    while todo:
        name, ex = todo.pop()
        if (norm(name), ex) in done:
            continue
        done.add((norm(name), ex))
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            sys.exit(f"gen-lock: {name} is required but not installed in {sys.prefix} -- "
                     f"pip install -e '.[{','.join(extras)}]' first")
        if norm(name) != norm(ROOT_DIST):
            seen[norm(dist.metadata["Name"])] = dist.version
        for spec in dist.requires or []:
            req = Requirement(spec)
            envs = [{"extra": e} for e in ex] or [{"extra": ""}]
            if req.marker is not None and not any(req.marker.evaluate(e) for e in envs):
                continue
            todo.append((req.name, frozenset(req.extras)))
    return dict(sorted(seen.items()))


def render(pins: dict[str, str], extras: list[str]) -> str:
    head = [
        "# flexEngine dependency lock (constraints file) -- GENERATED, do not edit by hand.",
        f"# base deps + extras [{','.join(extras)}], from Python {sys.version.split()[0]} on {sys.platform}.",
        "# Regenerate: .venv/bin/python packaging/gen-lock.py   (see packaging/README.md)",
    ]
    return "\n".join(head + [f"{n}=={v}" for n, v in pins.items()]) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extras", default=DEFAULT_EXTRAS)
    ap.add_argument("--out", type=Path, default=LOCK)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    extras = [e for e in a.extras.split(",") if e]
    text = render(closure(extras), extras)
    if a.check:
        old = a.out.read_text() if a.out.exists() else ""
        strip = lambda t: [l for l in t.splitlines() if not l.startswith("#")]  # noqa: E731
        if strip(old) != strip(text):
            print(f"{a.out} is out of date; run {Path(__file__).name}", file=sys.stderr)
            return 1
        return 0
    a.out.write_text(text)
    print(f"wrote {a.out} ({len(text.splitlines()) - 3} pins)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

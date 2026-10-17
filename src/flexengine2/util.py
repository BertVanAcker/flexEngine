"""Shared helpers: canonical JSON, digests, atomic persistence, time."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON (sorted keys, no whitespace, UTF-8). Used for everything that is hashed or signed."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes, mode: int = 0o644) -> None:
    """Write-then-rename with fsync, so a crash leaves either the old or the new file, never a torn one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, obj: Any, mode: int = 0o644) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"), mode)


def repair_jsonl_tail(path: str | Path, fsync: bool = True) -> bool:
    """Make an append-only JSON Lines file safe to append to after a crash or power cut: a last line without its
    newline is either complete (then the newline is added) or a torn prefix of a record whose append never finished
    (then it is cut off, so the next record does not get glued to it). Returns True when the file was changed."""
    path = Path(path)
    try:
        with open(path, "rb+") as f:
            size = f.seek(0, os.SEEK_END)
            if size == 0:
                return False
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return False
            back = min(size, 1 << 20)
            f.seek(size - back)
            buf = f.read(back)
            cut = buf.rfind(b"\n")
            start = size - back + cut + 1 if cut >= 0 else (0 if back == size else None)
            if start is None:   # one line longer than 1 MiB: only terminate it
                f.write(b"\n")
            else:
                f.seek(start)
                tail = f.read()
                try:
                    json.loads(tail.decode("utf-8"))
                    complete = True
                except (ValueError, UnicodeDecodeError):
                    complete = False
                if complete:
                    f.seek(0, os.SEEK_END)
                    f.write(b"\n")
                else:
                    f.truncate(start)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        return True
    except FileNotFoundError:
        return False


def fsync_tree(root: str | Path) -> None:
    """fsync every regular file and directory below `root` (and `root` itself), e.g. after unpacking a payload."""
    root = Path(root)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(p).st_mode):   # no symlinks, fifos, devices
                continue
            fd = os.open(p, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        dfd = os.open(dirpath, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def fsync_dir(path: str | Path) -> None:
    dfd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def now() -> float:
    return time.time()


def iso(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"

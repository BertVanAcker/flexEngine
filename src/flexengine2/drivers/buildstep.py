"""On-device build step (FE-4, ``x-flex-build``): helpers the drivers' ``build()`` share.

* ``run_build(argv, cwd, env, request)``: runs one build command with its output (stdout + stderr) streamed line by
  line to ``request.line``; a line ``flex-progress <0..1>`` is a progress report instead. The process runs in its own
  session and is killed as a group on the deadline (``BuildTimeout``, FE-BLD-002). Exit status != 0 -> DriverError.
* ``unpack_source(request, dst)``: the verified source payload (tar.gz) into a fresh directory.
* ``native_build(request)``: the native profile's build: ``build.command`` in the unpacked tree, then ``build.output``
  (default the tree) packed as a deterministic tar.gz (sorted, mtime 0, uid/gid 0), which becomes the payload; the
  result digest is its sha256.

The build never touches what runs: everything happens under the engine's build directory of the deployment
(``request.work_dir``), kept until that deployment's next build.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path

from flexengine2.drivers.base import BuildRequest, BuildResult, BuildTimeout, DriverError
from flexengine2.util import sha256_digest

__all__ = ["run_build", "unpack_source", "native_build", "pack_tree", "LINE_MAX"]

LINE_MAX = 2000
_PROGRESS = re.compile(r"^\s*flex-progress\s+([0-9]*\.?[0-9]+)\s*$")


def run_build(argv: list[str], cwd: str | Path, env: dict, request: BuildRequest, what: str = "build") -> None:
    """Run one build command; DriverError on a non-zero exit or when it cannot run, BuildTimeout on the deadline."""
    try:
        p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError as e:
        raise DriverError(f"{what} command not found: {argv[0]} ({e.strerror or e})") from None
    except OSError as e:
        raise DriverError(f"{what} command cannot run: {argv[0]} ({e})") from None

    def pump():
        for raw in iter(p.stdout.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = _PROGRESS.match(text)
            if m:
                request.progress(float(m.group(1)))
            else:
                request.line(text[:LINE_MAX])

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    deadline = time.monotonic() + float(request.timeout)
    while p.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.wait()
        t.join(timeout=5)
        raise BuildTimeout(f"build timed out after {float(request.timeout):g} s")
    t.join(timeout=5)
    if p.returncode != 0:
        raise DriverError(f"{what} command exited with status {p.returncode}")


def unpack_source(request: BuildRequest, dst: Path) -> Path:
    """The source payload (tar.gz, members confined to the tree) into a fresh `dst`."""
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    try:
        with tarfile.open(request.source_path, "r:*") as tf:
            tf.extractall(dst, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise DriverError(f"source payload unusable: {e}") from None
    return dst


def pack_tree(src: Path, out: Path) -> str:
    """Deterministic tar.gz of a directory (sorted names, mtime 0, uid/gid 0, no owner names); returns its digest."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tf:
        for path in sorted(src.rglob("*"), key=lambda p: p.relative_to(src).as_posix()):
            rel = path.relative_to(src).as_posix()
            ti = tf.gettarinfo(str(path), arcname=rel)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
            if ti.isfile():
                with open(path, "rb") as f:
                    tf.addfile(ti, f)
            else:
                tf.addfile(ti)
    data = buf.getvalue()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return sha256_digest(data)


def native_build(request: BuildRequest, profile: str = "native") -> BuildResult:
    """build.command in the unpacked source tree; build.output (default the tree) becomes the payload."""
    spec = request.spec or {}
    cmd = spec.get("command")
    if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) and c for c in cmd)):
        raise DriverError("the release descriptor names no build command (build.command)")
    work = Path(request.work_dir)
    src = unpack_source(request, work / "src")
    request.progress(0.05)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "FLEX_BUILD_ID": request.id,
           "LC_ALL": "C.UTF-8"}
    run_build(cmd, src, env, request)
    out_rel = spec.get("output") or "."
    out = (src / out_rel).resolve()
    if not out.is_dir() or not (out == src.resolve() or src.resolve() in out.parents):
        raise DriverError(f"build output {out_rel} is not a directory of the source tree")
    request.progress(0.95)
    payload = work / "payload.tar.gz"
    digest = pack_tree(out, payload)
    return BuildResult(digest=digest, payload_path=str(payload),
                       payload_annotations={"dev.flex.profile": profile})

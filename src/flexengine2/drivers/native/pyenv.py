"""Native payloads with their own Python dependencies (EV-36) and per-platform native layers (EV-37).

Shared by the process driver and the systemd driver (profile ``native``). CP-3: flexengine/api/bundle.v1alpha1.md §2.

Payload (tar.gz, unchanged entry points: ``app.py`` or ``unit.json``) plus, optionally:

* ``requirements.lock`` -- every distribution the payload needs, pinned (``name==version``) with at least one
  ``--hash=sha256:<hex>`` each (``pip download`` + ``pip hash`` or ``pip-compile --generate-hashes``);
* ``wheels/`` -- one wheel per locked distribution, built for the target CPU and Python (no sdists: the device never
  builds).

At unpack the driver creates ``<unpack dir>/.venv`` with ``<interpreter> -m venv`` (interpreter = the driver's
``python``, default the engine's; the engine's own environment is never touched), installs offline with
``pip install --no-index --find-links wheels --require-hashes --only-binary :all: -r requirements.lock`` and writes
``<unpack dir>/.venv.complete`` after an fsync of the whole venv (a crash in between leaves no marker: the next apply
rebuilds the venv). The venv's ``python`` then runs ``app.py`` (and replaces ``{python}`` in ``unit.json``'s
``ExecStart``). A payload without ``requirements.lock`` runs exactly as before (the engine's interpreter, no venv).

Platform (``Driver.platform()`` of the native drivers): ``{"dev.flex.arch": <uname -m, normalised: x86_64, aarch64,
armv7l, ...>, "dev.flex.pyabi": <cp312, cp313t, pp310 ...>}`` of the payload interpreter. A release may carry several
native layers annotated with these keys; the store picks the layer whose annotations do not contradict the
platform (an absent key fits any value, the most specific layer wins), so a Rust binary layer carries only
``dev.flex.arch`` and an unannotated single layer fits everywhere (backwards compatible). ``preflight`` refuses a
layer annotated for another CPU or Python, and wheels whose tags do not fit the device.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

from flexengine2.drivers.base import DriverError
from flexengine2.util import atomic_write_bytes

__all__ = ["ARCH", "PYABI", "LOCK", "WHEELS", "VENV", "VENV_MARK", "interpreter_info", "platform_of",
           "normalize_arch", "wheel_tags", "wheel_fits", "parse_lock", "check_payload", "check_tar", "ensure_venv",
           "payload_python"]

ARCH = "dev.flex.arch"
PYABI = "dev.flex.pyabi"
LOCK = "requirements.lock"
WHEELS = "wheels"
VENV = ".venv"
VENV_MARK = ".venv.complete"

_ARCH_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "armv8l": "armv7l", "armv7": "armv7l",
                 "armhf": "armv7l", "i386": "i686", "i586": "i686", "x86": "i686"}
_MANYLINUX_LEGACY = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}

_INFO_SCRIPT = r"""
import json, os, platform, struct, sys, sysconfig
try:
    g = os.confstr("CS_GNU_LIBC_VERSION")
except (AttributeError, ValueError, OSError):
    g = None
musl = not g and any(n.startswith("ld-musl-") for d in ("/lib", "/usr/lib") if os.path.isdir(d) for n in os.listdir(d))
print(json.dumps({"impl": sys.implementation.name, "version": list(sys.version_info[:2]),
                  "machine": platform.machine(), "bits": struct.calcsize("P") * 8,
                  "libc": "glibc" if g else ("musl" if musl else "unknown"),
                  "glibc": g.split()[1] if g and " " in g else None,
                  "freeThreaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}))
"""

_INFO: dict[str, dict] = {}


def normalize_arch(machine: str, bits: int = 64) -> str:
    """`uname -m` -> the wheel/OCI-neutral CPU name used in dev.flex.arch (a 32-bit userland on a 64-bit kernel is
    reported as its 32-bit architecture)."""
    m = _ARCH_ALIASES.get((machine or "").lower(), (machine or "").lower()) or "unknown"
    if bits == 32 and m == "aarch64":
        return "armv7l"
    if bits == 32 and m == "x86_64":
        return "i686"
    return m


def _local_info() -> dict:
    try:
        g = os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, ValueError, OSError):
        g = None
    musl = not g and any(n.startswith("ld-musl-") for d in ("/lib", "/usr/lib") if os.path.isdir(d)
                         for n in os.listdir(d))
    return {"impl": sys.implementation.name, "version": list(sys.version_info[:2]), "machine": _platform.machine(),
            "bits": struct.calcsize("P") * 8, "libc": "glibc" if g else ("musl" if musl else "unknown"),
            "glibc": g.split()[1] if g and " " in g else None,
            "freeThreaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}


def interpreter_info(python: str | None = None, timeout: float = 30.0) -> dict:
    """Implementation, version, CPU, libc of the payload interpreter (cached per path). DriverError when it cannot
    be run (the engine defers: a misconfigured interpreter is an executor problem, not a bad release)."""
    python = python or sys.executable
    if python in _INFO:
        return _INFO[python]
    if os.path.realpath(python) == os.path.realpath(sys.executable):
        info = _local_info()
    else:
        try:
            p = subprocess.run([python, "-I", "-c", _INFO_SCRIPT], capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            info = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            raise DriverError(f"payload interpreter {python} cannot run: {e}") from None
        if not info:
            raise DriverError(f"payload interpreter {python} cannot run: {' '.join((p.stderr or '').split())[-300:]}")
    info["arch"] = normalize_arch(info.get("machine", ""), int(info.get("bits") or 64))
    maj, mnr = info["version"][:2]
    pre = {"cpython": "cp", "pypy": "pp"}.get(info["impl"], info["impl"])
    info["pyabi"] = f"{pre}{maj}{mnr}" + ("t" if info.get("freeThreaded") else "")
    _INFO[python] = info
    return info


def platform_of(info: dict) -> dict[str, str]:
    return {ARCH: info["arch"], PYABI: info["pyabi"]}


# ---- wheel tags -------------------------------------------------------------------------------------------------
class BadWheelName(ValueError):
    """Not a PEP 427 wheel file name (caught by check_payload and reported as a preflight refusal)."""


def wheel_tags(filename: str) -> tuple[str, str, str]:
    """(python tag, abi tag, platform tag) of a wheel file name (PEP 427; compressed tag sets stay '.'-joined)."""
    stem = Path(filename).name
    if not stem.endswith(".whl"):
        raise BadWheelName(f"not a wheel: {filename}")
    parts = stem[:-4].split("-")
    if len(parts) not in (5, 6):
        raise BadWheelName(f"not a wheel file name: {filename}")
    return parts[-3], parts[-2], parts[-1]


def wheel_name(filename: str) -> str:
    return _norm(Path(filename).name.split("-", 1)[0])


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _plat_fits(tag: str, info: dict) -> bool:
    if tag == "any":
        return True
    arch = info["arch"]
    if tag == f"linux_{arch}":
        return True
    m = re.fullmatch(r"(manylinux1|manylinux2010|manylinux2014)_(.+)", tag)
    if m:
        need, tarch = _MANYLINUX_LEGACY[m.group(1)], m.group(2)
    else:
        m = re.fullmatch(r"(many|musl)linux_(\d+)_(\d+)_(.+)", tag)
        if not m:
            return False
        if m.group(1) == "musl":
            return info.get("libc") == "musl" and m.group(4) == arch
        need, tarch = (int(m.group(2)), int(m.group(3))), m.group(4)
    if tarch != arch or info.get("libc") != "glibc" or not info.get("glibc"):
        return False
    try:
        have = tuple(int(x) for x in info["glibc"].split(".")[:2])
    except ValueError:
        return False
    return have >= need


def _py_fits(py: str, abi: str, info: dict) -> bool:
    maj, mnr = info["version"][:2]
    cp = info["impl"] == "cpython"
    own = info["pyabi"]
    m = re.fullmatch(r"(py|cp|pp)(\d)(\d*)", py)
    if not m:
        return False
    kind, pmaj, pmin = m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    if pmaj != maj:
        return False
    if abi == "none":
        if kind == "py":
            return pmin is None or pmin <= mnr
        return py == own.rstrip("t")
    if abi == "abi3":
        return cp and kind == "cp" and not info.get("freeThreaded") and pmin is not None and pmin <= mnr
    return abi == own and py == own.rstrip("t")


def wheel_fits(filename: str, info: dict) -> bool:
    """True when pip on that interpreter would accept the wheel's tags (CPython/PyPy on Linux; the same rules as
    packaging.tags.sys_tags, without a dependency on packaging)."""
    py, abi, plat = wheel_tags(filename)
    return (any(_plat_fits(p, info) for p in plat.split("."))
            and any(_py_fits(a, b, info) for a in py.split(".") for b in abi.split(".")))


# ---- requirements.lock ------------------------------------------------------------------------------------------
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*===?\s*([^\s;]+)(\s*;[^-]*)?((?:\s+--hash[=\s]\S+)+)\s*$")


def parse_lock(text: str) -> dict[str, dict]:
    """{normalised name: {"line", "version", "hashes"}}; DriverError on a line that is not pinned with a hash."""
    out: dict[str, dict] = {}
    logical, start = "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not logical:
            start = n
        if line.endswith("\\"):
            logical += line[:-1] + " "
            continue
        logical = (logical + line).strip()
        if logical:
            m = _REQ.match(logical)
            if not m:
                raise DriverError(f"requirements.lock line {start} is not a pinned requirement with --hash: "
                                  f"{' '.join(logical.split())[:120]}")
            hashes = {h.split(":", 1)[1].lower() for h in re.findall(r"--hash[=\s](\S+)", m.group(5))
                      if h.startswith("sha256:")}
            if not hashes:
                raise DriverError(f"requirements.lock line {start} is not a pinned requirement with --hash: "
                                  f"{' '.join(logical.split())[:120]}")
            out[_norm(m.group(1))] = {"line": start, "version": m.group(3), "hashes": hashes}
        logical = ""
    return out


def check_payload(names: set[str], read, annotations: dict | None, info: dict) -> None:
    """Preflight of a native payload against the payload interpreter. `names`: member names (no leading './');
    `read(name) -> bytes`. Raises DriverError(reason) on a refusal; returns None when it fits."""
    ann = annotations or {}
    if ann.get(ARCH) and ann[ARCH] != info["arch"]:
        raise DriverError(f"native payload is built for {ann[ARCH]} (dev.flex.arch), this device is {info['arch']}")
    if ann.get(PYABI) and ann[PYABI] != info["pyabi"]:
        raise DriverError(f"native payload is built for Python {ann[PYABI]} (dev.flex.pyabi), the device's "
                          f"interpreter is {info['pyabi']}")
    if LOCK not in names:
        return                                            # no own dependencies: runs as before
    try:
        lock = parse_lock(read(LOCK).decode("utf-8"))
    except UnicodeDecodeError:
        raise DriverError("requirements.lock is not UTF-8 text") from None
    files = sorted(n for n in names if n.startswith(WHEELS + "/") and n.count("/") == 1 and n != WHEELS + "/")
    have: dict[str, list[str]] = {}
    for f in files:
        base = f.split("/", 1)[1]
        if not base.endswith(".whl"):
            raise DriverError(f"{f} is not a wheel (the device never builds from source)")
        try:
            fits = wheel_fits(base, info)
        except ValueError:
            raise DriverError(f"{f} is not a wheel (the device never builds from source)") from None
        if not fits:
            libc = " ".join(x for x in (info.get("libc"), info.get("glibc")) if x)
            raise DriverError(f"wheel {base} does not fit this device (Python {info['pyabi']}, {info['arch']}, {libc})")
        name = wheel_name(base)
        if name not in lock:
            raise DriverError(f"wheel {base} is not in requirements.lock")
        if hashlib.sha256(read(f)).hexdigest() not in lock[name]["hashes"]:
            raise DriverError(f"wheel {base} does not match its --hash in requirements.lock")
        have.setdefault(name, []).append(base)
    for name, e in lock.items():
        if name not in have:
            raise DriverError(f"requirements.lock pins {name}=={e['version']} but wheels/ has no wheel for it")


def check_tar(path: str, annotations: dict | None, info: dict) -> None:
    """check_payload on a payload archive (DriverError(reason) on a refusal, also for an unreadable archive)."""
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = {m.name.removeprefix("./"): m for m in tf.getmembers()}

            def read(name: str) -> bytes:
                f = tf.extractfile(members[name])
                return f.read() if f else b""
            check_payload(set(members), read, annotations, info)
    except (tarfile.TarError, OSError, TypeError) as e:
        raise DriverError(f"native payload is not a tar.gz archive: {e}") from None


# ---- venv per payload -------------------------------------------------------------------------------------------
def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def payload_python(workdir: Path, default: str) -> str:
    """The interpreter that runs this unpacked payload: its venv's python when it has a requirements.lock."""
    return str(_venv_python(Path(workdir) / VENV)) if (Path(workdir) / LOCK).exists() else default


def _tail(p: subprocess.CompletedProcess) -> str:
    return " ".join(((p.stderr or "") + " " + (p.stdout or "")).split())[-400:]


def _fsync_tree(root: Path) -> None:
    for d, _dirs, files in os.walk(root):
        for f in files:
            fp = os.path.join(d, f)
            if os.path.islink(fp):
                continue
            try:
                fd = os.open(fp, os.O_RDONLY)
            except OSError:
                continue
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def ensure_venv(workdir: Path, python: str | None = None, timeout: float = 600.0) -> str | None:
    """Build (once per unpacked digest) the payload's venv; returns its python, None for a payload without
    requirements.lock. Offline: no index, pinned + hash-checked wheels from the payload only. DriverError on any
    failure (the engine rolls back)."""
    workdir = Path(workdir)
    if not (workdir / LOCK).exists():
        return None
    venv, mark = workdir / VENV, workdir / VENV_MARK
    py = _venv_python(venv)
    if mark.exists() and py.exists():
        return str(py)
    python = python or sys.executable
    shutil.rmtree(venv, ignore_errors=True)
    mark.unlink(missing_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "LANG": "C.UTF-8",
           "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PIP_NO_INPUT": "1", "PYTHONNOUSERSITE": "1"}
    steps = [("create", [python, "-m", "venv", str(venv)]),
             ("install", [str(py), "-m", "pip", "install", "--no-index", "--find-links", str(workdir / WHEELS),
                          "--require-hashes", "--only-binary", ":all:", "--no-cache-dir", "-r", str(workdir / LOCK)])]
    for what, cmd in steps:
        try:
            p = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            shutil.rmtree(venv, ignore_errors=True)
            raise DriverError(f"payload venv ({what}): no answer within {timeout:g} s") from None
        except OSError as e:
            shutil.rmtree(venv, ignore_errors=True)
            raise DriverError(f"cannot create the payload venv with {python}: {e}") from None
        if p.returncode != 0:
            shutil.rmtree(venv, ignore_errors=True)
            if what == "create":
                raise DriverError(f"cannot create the payload venv with {python}: {_tail(p)}")
            raise DriverError(f"offline install of the payload's requirements.lock failed: {_tail(p)}")
    _fsync_tree(venv)
    atomic_write_bytes(mark, b"ok\n")
    return str(py)

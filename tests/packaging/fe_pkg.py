"""Shared helpers of tests/packaging (a plain module: tests/packaging has no __init__.py on purpose -- a package
named `packaging` would shadow the PyPI `packaging` library)."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

FLEXENGINE = Path(__file__).resolve().parents[2]
PACKAGING = FLEXENGINE / "packaging"
INSTALL = PACKAGING / "install.sh"
DIST = FLEXENGINE / "dist"
VERSION = re.search(r'__version__ = "([^"]+)"', (FLEXENGINE / "src/flexengine2/__init__.py").read_text()).group(1)
IMAGE = "flexengine-pkgtest:ubuntu24.04"
CA = Path("/root/.ccr/ca-bundle.crt")   # agent proxy CA, when this runs behind one


def ca_mount() -> list[str]:
    """`-v CA:/ca.crt:ro` when the agent proxy's CA is there and readable, else nothing. On a CI runner /root is not
    readable by the runner user: Path.exists() raises PermissionError there (Python >= 3.12), os.access answers."""
    return ["-v", f"{CA}:/ca.crt:ro"] if os.access(CA, os.R_OK) else []


def make_bumped(d: Path) -> Path:
    """Copy flexengine/ (pyproject, README, src) to d with the version bumped to <ver>.post1: an "upgrade"."""
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(FLEXENGINE / "pyproject.toml", d)
    shutil.copy(FLEXENGINE / "README.md", d)
    shutil.copytree(FLEXENGINE / "src", d / "src", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    init = d / "src/flexengine2/__init__.py"
    init.write_text(init.read_text().replace(f'"{VERSION}"', f'"{VERSION}.post1"'))
    return d


def changes(out: str) -> int:
    m = re.search(r"changes: (\d+)", out)
    assert m, out
    return int(m.group(1))


def sh(cmd, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    # an installed copy under test must import itself, never the source tree a PYTHONPATH names (run_tests.sh and
    # worktree runs put src on PYTHONPATH)
    kw.setdefault("env", {k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    return subprocess.run(cmd, **kw)


# ---- docker ------------------------------------------------------------------------------------------------------------
def _apt_proxy_script() -> str:
    """apt inside the container: plain first; behind an HTTPS-only agent proxy switch the sources to https."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not proxy:
        return ""
    return (f'echo \'Acquire::https::Proxy "{proxy}"; Acquire::https::CAInfo "/ca.crt";\' '
            "> /etc/apt/apt.conf.d/99proxy; sed -i 's#http://#https://#g' /etc/apt/sources.list.d/ubuntu.sources; ")


@functools.cache
def image() -> str:
    """ubuntu:24.04 + python3 python3-venv systemd (cached as a local tag). Raises RuntimeError with the reason."""
    if sh(["docker", "image", "inspect", IMAGE]).returncode == 0:
        return IMAGE
    r = sh(["docker", "pull", "ubuntu:24.04"], timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"cannot pull ubuntu:24.04: {r.stderr.strip()[-300:]}")
    name = f"fe-pkg-prep-{os.getpid()}"
    vol = ca_mount()
    script = (_apt_proxy_script() + "apt-get update -q >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install "
              "-y -q --no-install-recommends python3 python3-venv systemd systemd-sysv dbus >/dev/null && "
              "rm -f /etc/apt/apt.conf.d/99proxy")
    r = sh(["docker", "run", "--name", name, "--network", "host", *vol, "ubuntu:24.04", "bash", "-c", script],
           timeout=900)
    try:
        if r.returncode != 0:
            raise RuntimeError(f"apt in ubuntu:24.04 failed: {r.stderr.strip()[-500:]}")
        c = sh(["docker", "commit", "--change", 'CMD ["/sbin/init"]', name, IMAGE])
        if c.returncode != 0:
            raise RuntimeError(c.stderr)
    finally:
        sh(["docker", "rm", "-f", name])
    return IMAGE


class Container:
    """One Ubuntu 24.04 container, systemd as PID 1 when the host allows it (privileged), else a plain shell."""

    def __init__(self, name: str, mounts: dict[str, str]):
        self.name = name
        vols = [a for host, ctr in mounts.items() for a in ("-v", f"{host}:{ctr}:ro")]
        common = ["docker", "run", "-d", "--name", name, *vols]
        r = sh([*common, "--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                "--tmpfs", "/run", "--tmpfs", "/run/lock", image(), "/sbin/init"])
        self.systemd = r.returncode == 0 and self._wait_systemd()
        if not self.systemd:
            sh(["docker", "rm", "-f", name])
            r = sh([*common, image(), "sleep", "infinity"])
            if r.returncode != 0:
                raise RuntimeError(r.stderr)

    def _wait_systemd(self) -> bool:
        for _ in range(40):
            st = self.sh("systemctl is-system-running 2>/dev/null").stdout.strip()
            if st in ("running", "degraded"):
                return True
            time.sleep(0.25)
        return False

    def sh(self, script: str, timeout: float = 600) -> subprocess.CompletedProcess:
        return sh(["docker", "exec", self.name, "bash", "-c", script], timeout=timeout)

    def ok(self, script: str, timeout: float = 600) -> str:
        r = self.sh(script, timeout)
        assert r.returncode == 0, f"$ {script}\n{r.stdout}\n{r.stderr}"
        return r.stdout

    def close(self):
        sh(["docker", "rm", "-f", self.name])

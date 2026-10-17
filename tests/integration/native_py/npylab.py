"""Fixtures of the EV-36/EV-37 integration tests: flexCommunicator nodes (flcpy, flcrs) as native payloads.

* flexCommunicator checkout: $FLEXCOMMUNICATOR_DIR (default /home/user/flexcommunicator; CI: a clone of
  github.com/BertVanAcker/flexCommunicator). The GettingStarted example `flexCloud_variable` is used read-only: its
  files are packed into the payload at build time, nothing is copied into this repo.
* flcpy wheels: `pip download flexCommunicator==0.1.7` for the TARGET interpreter/CPU (cached under
  tests/work/native_py/wheels/<pyabi>-<arch>); the device side installs them offline.
* flcrs binaries: cargo for x86_64-unknown-linux-gnu and aarch64-unknown-linux-gnu (cross linker
  aarch64-linux-gnu-gcc; CI: `rustup target add aarch64-unknown-linux-gnu && apt install gcc-aarch64-linux-gnu`),
  cached under tests/work/native_py/flcrs. Without the cross toolchain the aarch64 layer is a stand-in ELF stub
  (the x86_64 engine only has to refuse it, never run it).
* Redis: every flexCommunicator node needs one for its flexCloud keys (redis:7.4-alpine container on the default
  bridge, reached by IP from the host and from the systemd container).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from flexengine2.drivers.native import pyenv
from flexengine2.testing import native_release as nr

FLEXENGINE = Path(__file__).resolve().parents[3]
WORK = FLEXENGINE / "tests" / "work" / "native_py"
FC = Path(os.environ.get("FLEXCOMMUNICATOR_DIR") or "/home/user/flexcommunicator")
EXAMPLE = "flexCloud_variable"
FLCPY_REQ = os.environ.get("FLEX_FLCPY_REQUIREMENT", "flexCommunicator==0.1.7")
REDIS_IMAGE = "redis:7.4-alpine"
CONNECTOR = "oberon5@sandbox-3fg889"           # connectorID of the GettingStarted config.yaml
CONTRACT = FLEXENGINE / "tests" / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

LAUNCHER = '''"""flexEngine native entry point of the flcpy GettingStarted node {example} (EV-36 packaging).
The example imports itself as examples.GettingStarted.{example}... and reads config.yaml from its directory."""
import os, runpy, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
EX = ROOT / "examples" / "GettingStarted" / "{example}"
sys.path.insert(0, str(ROOT))
os.chdir(EX)
sys.argv = [str(EX / "main.py")]
runpy.run_path(str(EX / "main.py"), run_name="__main__")
'''


def need_fc():
    if not (FC / "examples" / "GettingStarted" / EXAMPLE / "main.py").exists():
        pytest.skip(f"flexCommunicator checkout not found at {FC} (set FLEXCOMMUNICATOR_DIR)")


class Redis:
    def __init__(self):
        self.name = f"flex-ev36-redis-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, REDIS_IMAGE], capture_output=True,
                           text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"docker run {REDIS_IMAGE}: {r.stderr.strip()[-300:]}")
        self.ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                  self.name], capture_output=True, text=True, check=True).stdout.strip().split()[0]
        end = time.time() + 30
        while time.time() < end and self.cli("ping") != "PONG":
            time.sleep(0.2)

    def cli(self, *args: str, db: int = 6) -> str:
        p = subprocess.run(["docker", "exec", self.name, "redis-cli", "-n", str(db), *args], capture_output=True,
                           text=True, timeout=30)
        return p.stdout.strip()

    def status(self, app_id: str) -> str:
        return self.cli("get", f"{CONNECTOR}:applications:{app_id}:status")

    def samples(self, app_id: str) -> int:
        v = self.cli("llen", f"{CONNECTOR}:applications:{app_id}:variables:Temperature")
        return int(v) if v.isdigit() else 0

    def close(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=120)


@pytest.fixture(scope="session")
def redis():
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    try:
        r = Redis()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no redis container: {e}")
    yield r
    r.close()


def example_config(redis_ip: str, app_id: str, src: Path) -> bytes:
    """The example's config.yaml with the Redis host of this test and a unique application id."""
    text = (src / "config.yaml").read_text()
    text = text.replace("localhost", redis_ip).replace(f"id: {EXAMPLE}", f"id: {app_id}")
    assert f"id: {app_id}" in text, "unexpected GettingStarted config.yaml layout"
    return text.encode()


def flcpy_project(tmp: Path, redis_ip: str, app_id: str) -> Path:
    """Payload tree: app.py launcher + examples/GettingStarted/<example>/ (config patched for this test)."""
    need_fc()
    proj = tmp / f"flcpy-{app_id}"
    src = FC / "examples" / "GettingStarted" / EXAMPLE
    dst = proj / "examples" / "GettingStarted" / EXAMPLE
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.log"))
    for init in (FC / "examples" / "__init__.py", FC / "examples" / "GettingStarted" / "__init__.py"):
        if init.exists():
            shutil.copyfile(init, proj / init.relative_to(FC))
    (dst / "config.yaml").write_bytes(example_config(redis_ip, app_id, src))
    (proj / "app.py").write_text(LAUNCHER.format(example=EXAMPLE))
    return proj


def flcpy_payload(tmp: Path, redis_ip: str, app_id: str, python: str, arch: str) -> tuple[Path, dict]:
    """flcpy GettingStarted node + its wheels for (python, arch), as a native payload file."""
    cache = WORK / "wheels" / f"cp{python.replace('.', '')}-{arch}"
    try:
        data, ann = nr.build_py_payload(flcpy_project(tmp, redis_ip, app_id), requirements=[FLCPY_REQ],
                                        python=python, arch=arch, cache=cache)
    except RuntimeError as e:                             # pip download needs the index once (then cached)
        shutil.rmtree(cache, ignore_errors=True)
        pytest.skip(f"cannot download the flcpy wheels: {e}")
    out = tmp / f"{app_id}.tar.gz"
    out.write_bytes(data)
    out.chmod(0o644)
    return out, ann


def host_python() -> tuple[str, str]:
    info = pyenv.interpreter_info()
    return f"{info['version'][0]}.{info['version'][1]}", info["arch"]


# ---- flcrs -------------------------------------------------------------------------------------------------------
STUB_ELF = {  # 64-byte ELF headers (e_machine aarch64 / x86_64): a stand-in that is never executed
    "aarch64": bytes.fromhex("7f454c460201010000000000000000000300b700010000000000000000000000") + bytes(32),
    "x86_64": bytes.fromhex("7f454c4602010100000000000000000003003e00010000000000000000000000") + bytes(32),
}


def _cargo_ok() -> bool:
    return bool(shutil.which("cargo"))


def _target_installed(triple: str) -> bool:
    if not shutil.which("rustup"):
        return False
    p = subprocess.run(["rustup", "target", "list", "--installed"], capture_output=True, text=True)
    return triple in p.stdout.split()


@pytest.fixture(scope="session")
def flcrs_bins() -> dict:
    """{arch: (binary Path, real: bool)} for x86_64 and aarch64 (aarch64 real when the cross toolchain exists)."""
    need_fc()
    if not _cargo_ok():
        pytest.skip("cargo not installed")
    out_dir = WORK / "flcrs"
    crate = nr.flcrs_crate(FC, EXAMPLE, out_dir)
    host = pyenv.normalize_arch(os.uname().machine)
    out = {}
    for arch in ("x86_64", "aarch64"):
        cross = arch != host
        if cross and not (_target_installed(nr.RUST_TARGETS[arch]) and shutil.which(nr.CROSS_LINKERS[arch])):
            stub = out_dir / f"{EXAMPLE}.{arch}.stub"
            stub.write_bytes(STUB_ELF[arch])
            stub.chmod(0o755)
            out[arch] = (stub, False)
            continue
        cached = out_dir / "bin" / arch / EXAMPLE         # built once; FLEX_REBUILD_FLCRS=1 rebuilds
        if cached.exists() and os.environ.get("FLEX_REBUILD_FLCRS") != "1":
            out[arch] = (cached, True)
            continue
        try:
            built = nr.cargo_build(crate, arch, binary=EXAMPLE, target_dir=out_dir / "target")
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built, cached)
            out[arch] = (cached, True)
        except RuntimeError as e:
            if not cross:
                pytest.skip(f"flcrs build failed: {e}")
            stub = out_dir / f"{EXAMPLE}.{arch}.stub"
            stub.write_bytes(STUB_ELF[arch])
            stub.chmod(0o755)
            out[arch] = (stub, False)
    shutil.rmtree(out_dir / "target", ignore_errors=True)   # ~300 MB of build products: keep only the binaries
    return out


def elf_machine(path: Path) -> str:
    b = path.read_bytes()[:20]
    assert b[:4] == b"\x7fELF", path
    return {0x3e: "x86_64", 0xb7: "aarch64", 0x28: "armv7l"}.get(int.from_bytes(b[18:20], "little"), "?")


def flcrs_payload(tmp: Path, binary: Path, arch: str, redis_ip: str, app_id: str) -> tuple[bytes, dict]:
    """The flcrs node as a unit.json payload: bin/<example> + config.yaml next to the working directory."""
    need_fc()
    src = FC / "examples" / "flcrs" / "GettingStarted" / EXAMPLE
    return nr.build_bin_payload({f"bin/{EXAMPLE}": binary, "config.yaml": example_config(redis_ip, app_id, src)},
                                [f"bin/{EXAMPLE}"], arch=arch)

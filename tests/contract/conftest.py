"""Driver contract suite (docs/flexengine-v2-plan.md §7.2 layer 2, EV-03): harness registry and tiers.

Every driver is adapted to the shared cases in test_spi.py by a harness (spi_harness/*.py):

  push tier (always)   fakes: fake docker/podman CLI, fake systemctl, fake `ank`, P4's fake arduino-app-cli, a fake
                       SMP base image, the D1 pyads/pymodbus stand-ins, an RT_HDR shm file, a fake `rauc` + boot
                       simulator; the process driver runs real
  nightly tier         real docker, real systemd (PID 1 in an Ubuntu 24.04 container, marker docker), real podman
                       (plain + under a fake `ank`), QEMU mps2-an385 with the P3 base image, real rauc 1.11.3 in a
                       privileged Ubuntu 24.04 container (boot simulator backend). Marked `docker` / `podman` / `qemu` + `slow`; collected always, but only RUN when
                       selected with -m (e.g. `-m "docker or podman or qemu"`), and skipped when the executor is
                       missing.

A harness lists the cases its driver violates (`xfail`, applied as xfail(strict=True) so a fix shows up as XPASS
-> failure until the entry is removed) and the cases that do not apply to its executor (`na`, skipped).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from spi_harness.applab import ApplabHarness  # noqa: E402
from spi_harness.container import (FakeAnkaiosHarness, FakeDockerHarness, FakePodmanHarness,  # noqa: E402
                                   RealDockerHarness, RealPodmanAnkaiosHarness, RealPodmanHarness)
from spi_harness.os_rauc import FakeRaucHarness, RealRaucHarness  # noqa: E402
from spi_harness.mcu import (FakeMcuFirmwareHarness, FakeMcuModuleHarness, QemuMcuFirmwareHarness,  # noqa: E402
                             QemuMcuModuleHarness)
from spi_harness.plc import PlcAdsHarness, PlcModbusHarness, PlcShmHarness  # noqa: E402
from spi_harness.process import ProcessHarness  # noqa: E402
from spi_harness.systemd import FakeSystemdHarness, RealSystemdHarness  # noqa: E402

PUSH = [ProcessHarness, FakeSystemdHarness, FakeDockerHarness, FakePodmanHarness, FakeAnkaiosHarness, ApplabHarness,
        FakeMcuModuleHarness, FakeMcuFirmwareHarness, PlcModbusHarness, PlcShmHarness, PlcAdsHarness, FakeRaucHarness]
NIGHTLY = [(RealDockerHarness, "docker"), (RealSystemdHarness, "docker"), (RealPodmanHarness, "podman"), (RealPodmanAnkaiosHarness, "podman"),
           (QemuMcuModuleHarness, "qemu"), (QemuMcuFirmwareHarness, "qemu"), (RealRaucHarness, "docker")]
NIGHTLY_MARKS = ("docker", "podman", "qemu")


def harness_params():
    out = [pytest.param(h, id=h.id) for h in PUSH]
    out += [pytest.param(h, id=h.id, marks=[getattr(pytest.mark, m), pytest.mark.slow]) for h, m in NIGHTLY]
    return out


def pytest_generate_tests(metafunc):
    if "harness" in metafunc.fixturenames:
        metafunc.parametrize("harness", harness_params(), indirect=True)


def pytest_collection_modifyitems(config, items):
    selected = bool(config.getoption("-m"))
    for it in items:
        if HERE not in Path(str(it.path)).resolve().parents:
            continue
        if not selected and any(it.get_closest_marker(m) for m in NIGHTLY_MARKS):
            it.add_marker(pytest.mark.skip(reason='nightly tier: select with -m "docker or podman or qemu"'))
            continue
        cs = getattr(it, "callspec", None)
        h = cs.params.get("harness") if cs else None
        if h is None:
            continue
        case = it.originalname.removeprefix("test_")
        keys = [case] + (["unreachable"] if case.startswith("unreachable") else [])
        for k in keys:
            if k in h.na:
                it.add_marker(pytest.mark.skip(reason=f"n/a for {h.id}: {h.na[k]}"))
                break
            if k in h.xfail:
                it.add_marker(pytest.mark.xfail(strict=True, reason=h.xfail[k]))
                break


@pytest.fixture
def harness(request, tmp_path, monkeypatch):
    h = request.param(tmp_path, monkeypatch)
    try:
        h.setup()
        yield h
    finally:
        h.cleanup()


@pytest.fixture(scope="session", autouse=True)
def _remove_contract_images():
    yield
    for rt in ("docker", "podman"):
        try:
            p = subprocess.run([rt, "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True,
                               timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for ref in p.stdout.split():
            if "flexnode/ct_app_" in ref:
                subprocess.run([rt, "rmi", "-f", ref], capture_output=True, timeout=60)

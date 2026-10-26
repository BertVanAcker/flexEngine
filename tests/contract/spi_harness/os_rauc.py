"""Harnesses: OsRaucDriver over a boot simulator (fakes/bootsim.py) with
  * push tier:    the fake `rauc` CLI (fakes/fakerauc.py) and fake bundles,
  * nightly tier: the REAL rauc 1.11.3 (+ rauc-service, system D-Bus) in a privileged Ubuntu 24.04 container
                  (RaucLab): two file-backed raw slots, `bootloader=custom` whose backend is bootsim, real
                  `rauc bundle` / `rauc info` / `rauc install` / `rauc status mark-*`.

No real reboot: the driver's `rebootCommand` is `bootsim.py STATE reboot`. With autoBoot (contract cases) the simulated
machine boots at once, the boot id changes and the driver process carries on as if it were the engine restarted on
the new slot; the integration tests (tests/integration/rauc) turn autoBoot off and kill/restart the engine around
`bootsim.boot()`. In the nightly container every boot restarts `rauc service --override-boot-slot=<booted>` (the
container's /proc/cmdline is the host's), so RAUC sees the simulated slot switch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from flexengine2.drivers.base import Workload
from flexengine2.drivers.os_rauc import ANN_COMPATIBLE, OsRaucDriver
from flexengine2.testing import rauc as trauc

from .base import DEVICE, FAKES, Harness, Release, deployment, digest_of, failing_shim, shim

sys.path.insert(0, str(FAKES))
import bootsim  # noqa: E402

BOOTSIM = FAKES / "bootsim.py"
CACHE = Path(__file__).resolve().parents[2] / "work" / "rauc"   # nightly: test PKI + real bundles, built once
FAKERAUC = FAKES / "fakerauc.py"
PROBE = {"type": "os-rauc", "periodSeconds": 0.1, "successThreshold": 2, "timeoutSeconds": 15}


class SimRauc:
    """One simulated A/B device: bootsim state + (fake or real) rauc + the driver config that drives it."""

    real = False

    def __init__(self, root: Path, *, auto_boot: bool = True, max_tries: int = 3, mount: Path | None = None):
        self.root = Path(root).resolve()
        self.mount = Path(mount or root).resolve()   # nightly: bind-mounted at the same path (bundles, engine data)
        self.sim = self.root / "sim"
        self.state = self.sim / "state.json"
        self.bin = self.root / "bin"
        self.devs = {"A": self.sim / "rootfs.0.img", "B": self.sim / "rootfs.1.img"}
        self.auto_boot, self.max_tries = auto_boot, max_tries

    # -- lifecycle --
    def start(self) -> "SimRauc":
        bootsim.init(self.state, {k: str(v) for k, v in self.devs.items()}, booted="A", max_tries=self.max_tries,
                     auto_boot=self.auto_boot, compatible=trauc.COMPATIBLE)
        self.keyring = self.root / "keyring.txt"
        self.keyring.write_text(trauc.FAKE_SIGNER + "\n")
        self.calls = self.root / "rauc-calls.jsonl"
        self.rauc = shim(self.bin, "rauc", FAKERAUC, {"FAKE_RAUC_STATE": self.state,
                                                      "FAKE_RAUC_KEYRING": self.keyring,
                                                      "FAKE_RAUC_CALLS": self.calls})
        return self

    def stop(self) -> None:
        pass

    def driver_cfg(self, shared: Path, **kw) -> dict:
        return {"rauc": str(self.rauc), "keyring": str(self.keyring), "sharedDataPath": str(shared),
                "rebootCommand": [sys.executable, str(BOOTSIM), str(self.state), "reboot"],
                "bootIdFile": str(bootsim.boot_id_file(self.state)), "bootTimeoutSeconds": 20,
                "cliTimeout": 30, **kw}

    # -- payloads --
    def bundle(self, out_dir: Path, version: str, **kw) -> Path:
        return trauc.build_fake_bundle(Path(out_dir) / f"os-{version}.raucb", version, **kw)

    def rogue_bundle(self, out_dir: Path, version: str) -> Path:
        return trauc.build_fake_bundle(Path(out_dir) / f"os-{version}-rogue.raucb", version, signer="rogue")

    # -- simulator access --
    def show(self) -> dict:
        return bootsim.load(self.state)

    def boot(self) -> dict:
        return bootsim.boot(self.state)

    def set(self, **kw) -> None:
        with bootsim.locked(self.state) as st:
            st.update(kw)

    def installs(self) -> int:
        return int(self.show().get("installs", 0))

    def install_started(self) -> bool:
        return any(l.startswith("install ") for l in self.show()["log"])

    def power_off(self) -> None:
        """A power cut also ends a running `rauc install` (the fake installer runs as the engine's child)."""
        pid = self.show().get("installPid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def booted_version(self) -> str | None:
        st = self.show()
        b = st.get("booted")
        return bootsim.header(st["slots"][b]["device"]).get("version") if b else None

    def foreign_image_in_booted_slot(self) -> None:
        with bootsim.locked(self.state) as st:
            b = st["booted"]
            if st["slots"][b].get("bundle"):
                st["slots"][b]["bundle"]["hash"] = "f" * 64

    def health_check_cmd(self) -> list[str]:
        return [sys.executable, str(BOOTSIM), str(self.state), "health"]


class RaucLab(SimRauc):
    """The same simulated device driven by the real rauc 1.11.3 in a privileged container (nightly tier)."""

    real = True

    def start(self) -> "RaucLab":
        if not trauc.docker_ok():
            pytest.skip("docker not available")
        trauc.ensure_image()
        self.name = f"flexrauc-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.pki = trauc.make_pki(CACHE / "pki")
        self.keyring = self.pki["keyring"]
        on_boot = [["docker", "exec", self.name, "sh", str(self.root / "svc.sh"), "{booted}"]]
        bootsim.init(self.state, {k: str(v) for k, v in self.devs.items()}, booted="A", max_tries=self.max_tries,
                     auto_boot=self.auto_boot, compatible=trauc.COMPATIBLE, on_boot=on_boot)
        for d in self.devs.values():                      # raw slots: room for the 4 KiB test images
            with open(d, "r+b") as f:
                f.truncate(8192)
        (self.root / "backend.sh").write_text(f'#!/bin/sh\nexec python3 {BOOTSIM} {self.state} backend "$@"\n')
        (self.root / "backend.sh").chmod(0o755)
        self.conf = self.root / "system.conf"
        self.conf.write_text(f"""[system]
compatible={trauc.COMPATIBLE}
bootloader=custom
statusfile={self.root}/rauc.status
max-bundle-download-size=10485760

[keyring]
path={self.keyring}

[handlers]
bootloader-custom-backend={self.root}/backend.sh
pre-install={self.root}/preinstall.sh

[slot.rootfs.0]
device={self.devs['A']}
type=raw
bootname=A

[slot.rootfs.1]
device={self.devs['B']}
type=raw
bootname=B
""")
        (self.root / "preinstall.sh").write_text(f"""#!/bin/sh
# test hook: a slow install, so a power cut can hit it (installDelay)
touch {self.root}/install-started
[ -f {self.root}/install-delay ] && sleep $(cat {self.root}/install-delay)
exit 0
""")
        (self.root / "preinstall.sh").chmod(0o755)
        (self.root / "svc.sh").write_text(f"""#!/bin/sh
# (re)start rauc service as if the machine had booted slot $1 (bootname)
# with a device's umask (0022, as on the CI runner): `docker exec` processes run with 0000 under the development
# container's Docker 29, which hid root-created files that the unprivileged host user cannot open
umask 022
[ -f {self.root}/svc.pid ] && kill $(cat {self.root}/svc.pid) 2>/dev/null || true
for i in $(seq 50); do [ -f {self.root}/svc.pid ] && kill -0 $(cat {self.root}/svc.pid) 2>/dev/null || break; sleep 0.1; done
setsid rauc service --conf={self.conf} --override-boot-slot=$1 >>{self.root}/svc.log 2>&1 &
echo $! > {self.root}/svc.pid
for i in $(seq 100); do rauc status --output-format=json >/dev/null 2>&1 && exit 0; sleep 0.1; done
echo "rauc service did not come up" >&2; exit 1
""")
        subprocess.run(["docker", "run", "-d", "--rm", "--privileged", "--name", self.name, "-v",
                        f"{self.mount}:{self.mount}", "-v", f"{FAKES}:{FAKES}:ro", "-v", f"{CACHE}:{CACHE}:ro",
                        trauc.IMAGE, "sleep", "infinity"],
                       check=True, capture_output=True, timeout=120)
        self._exec("sh", "-c", "mkdir -p /run/dbus && rm -f /usr/share/dbus-1/system-services/de.pengutronix.rauc.service"
                               " && dbus-daemon --system --fork")
        self._exec("sh", str(self.root / "svc.sh"), "A")
        self.bin.mkdir(parents=True, exist_ok=True)
        self.rauc = self.bin / "rauc"
        self.rauc.write_text(f'#!/bin/sh\nexec docker exec {self.name} rauc "$@"\n')
        self.rauc.chmod(0o755)
        return self

    def _exec(self, *cmd: str) -> str:
        p = subprocess.run(["docker", "exec", self.name, *cmd], capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError(f"{cmd}: {p.stderr.strip()}")
        return p.stdout

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=60)

    def bundle(self, out_dir: Path, version: str, **kw) -> Path:
        tag = ("-bad" if not kw.get("bootable", True) else "") + ("-sick" if not kw.get("healthy", True) else "")
        fp = hashlib.sha256(Path(self.pki["cert"]).read_bytes()).hexdigest()[:8]   # bundles of this PKI only
        return trauc.build_real_bundle(CACHE / "bundles" / f"os-{version}{tag}-{fp}.raucb", version, self.pki, **kw)

    def rogue_bundle(self, out_dir: Path, version: str) -> Path:
        rogue = trauc.make_pki(CACHE / "pki-rogue", cn="rogue")
        fp = hashlib.sha256(Path(rogue["cert"]).read_bytes()).hexdigest()[:8]
        return trauc.build_real_bundle(CACHE / "bundles" / f"os-{version}-rogue-{fp}.raucb", version, rogue)

    def set(self, **kw) -> None:
        if "installDelay" in kw:
            p = self.root / "install-delay"
            if kw["installDelay"]:
                p.write_text(f"{kw['installDelay']:g}\n")
            else:
                p.unlink(missing_ok=True)
                (self.root / "install-started").unlink(missing_ok=True)
        super().set(**kw)

    def install_started(self) -> bool:
        return (self.root / "install-started").exists()

    def power_off(self) -> None:
        pass                        # boot() restarts rauc service, which ends a running install

    def installs(self) -> int:
        log = self.root / "svc.log"
        return len(re.findall(r"installing `[^`]+` succeeded", log.read_text())) if log.exists() else 0

    def foreign_image_in_booted_slot(self) -> None:
        """Rewrite RAUC's record of the booted slot's bundle (as if an image had been put there behind the engine's
        back) and restart the service so it reads it."""
        st = self.show()
        name = st["slots"][st["booted"]]["name"]
        p = self.root / "rauc.status"
        text = p.read_text()
        text = re.sub(rf"(\[slot\.{re.escape(name)}\][^\[]*?bundle\.hash=)[0-9a-f]+", lambda m: m.group(1) + "f" * 64,
                      text, flags=re.S)
        tmp = p.with_name(p.name + ".tmp")      # rauc (root in the container) owns the file; the host user may only
        tmp.write_text(text)                    # replace it (its directory), not write into it
        os.replace(tmp, p)
        self._exec("sh", str(self.root / "svc.sh"), st["booted"])


class _Base(Harness):
    driver_cls = OsRaucDriver
    profile = "os-rauc"
    lab_cls = SimRauc
    na = {}

    def setup(self):
        self.lab = self.lab_cls(self.tmp / "dev", mount=self.tmp).start()
        self.payloads = self.tmp / "payloads"
        self.wls: dict = {}

    def cfg(self, **kw) -> dict:
        return self.lab.driver_cfg(self.tmp, **kw)

    def make_driver(self):
        return OsRaucDriver(DEVICE, self.data, self.cfg())

    def _wl(self, version: str, dep_id: str | None, **kw) -> Workload:
        key = (version, tuple(sorted(kw.items())))
        if key not in self.wls:
            self.wls[key] = self.lab.bundle(self.payloads, version, **kw)
        b = self.wls[key]
        return Workload(dep_id=dep_id or self.dep_id, module="os_image", version=version,
                        digest=digest_of(b.read_bytes()), payload_path=str(b),
                        payload_annotations={"dev.flex.profile": "os-rauc", ANN_COMPATIBLE: trauc.COMPATIBLE},
                        health=dict(PROBE))

    def workload(self, version="1.0.0", dep_id=None):
        return self._wl(version, dep_id)

    def bad_workload(self, dep_id=None):
        return self._wl("9.9.9", dep_id, bootable=False)   # rootfs without the engine: never comes up

    def refused_release(self):
        b = self.lab.rogue_bundle(self.payloads, "6.6.6")
        rel = Release(str(b), {"dev.flex.profile": "os-rauc", ANN_COMPATIBLE: trauc.COMPATIBLE}, {"health": PROBE})
        return rel, deployment(self.dep_id, "os_image", "6.6.6", "os-rauc")

    def transfers(self):
        return self.lab.installs()

    def destroy_out_of_band(self, dep_id):
        self.lab.foreign_image_in_booted_slot()
        return True

    def cleanup(self):
        self.lab.stop()

    @contextmanager
    def unreachable(self):
        down = failing_shim(self.tmp / "down-bin", "rauc",
                            'Error retrieving slot status via D-Bus: The name de.pengutronix.rauc was not provided')
        yield OsRaucDriver(DEVICE, self.data, self.cfg(rauc=str(down)))


class FakeRaucHarness(_Base):
    id = "os-rauc[fake-rauc]"
    lab_cls = SimRauc

    def break_commit(self):
        self.lab.set(refuseMarkGood=True)
        return True


class RealRaucHarness(_Base):
    id = "os-rauc[rauc-1.11]"
    lab_cls = RaucLab

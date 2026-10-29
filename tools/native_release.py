#!/usr/bin/env python3
"""Native release tooling (EV-36 / EV-37): payloads with their own Python dependencies, per-CPU binary payloads,
and one signed release carrying several native layers.

    # flcpy (or any Python) node + its wheels for the TARGET interpreter/CPU (needs the index once, here):
    native_release.py py-payload PROJECT_DIR -r 'flexCommunicator==0.1.7' --python 3.12 --arch aarch64 -o node-arm.tar.gz

    # flexCommunicator Rust GettingStarted example, one payload per CPU (cross linker for a foreign CPU):
    native_release.py flcrs --flexcommunicator ~/flexCommunicator --example flexCloud_variable \
        --arch x86_64 --arch aarch64 -o out/

    # one release, several layers (each PAYLOAD has its annotations next to it in PAYLOAD.json):
    native_release.py release --oci registry/ --key release.key --module flcrs_node --version 1.0.0 out/*.tar.gz

    # a release with its own descriptor (docs/capture-release.md):
    native_release.py release --oci registry/ --key release.key --module capture_logger --version 1.0.0 \
        --descriptor docs/examples/capture_release/descriptor.json capture.tar.gz

Every payload is written with a sidecar `<payload>.json` holding its layer annotations (dev.flex.profile, and
dev.flex.arch / dev.flex.pyabi when the payload is CPU / Python specific). The device side is
flexengine2/drivers/native/pyenv.py; the format is CP-3, api/bundle.v1alpha1.md §2.

CI recipe for aarch64 on an x86_64 runner (ubuntu-24.04):
    rustup target add aarch64-unknown-linux-gnu
    sudo apt-get install -y gcc-aarch64-linux-gnu        # linker + aarch64 glibc
    (cargo_build sets CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc)
Python wheels for another CPU need no toolchain: pip download --only-binary :all: --platform manylinux_..._aarch64.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if (SRC / "flexengine2").exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flexengine2.artefacts.oci import Layout  # noqa: E402
from flexengine2.identity import crypto  # noqa: E402
from flexengine2.testing import native_release as nr  # noqa: E402


def _write(out: Path, data: bytes, ann: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    Path(str(out) + ".json").write_text(json.dumps(ann, indent=1, sort_keys=True) + "\n")
    print(f"{out}  {json.dumps(ann, sort_keys=True)}")


def cmd_py(a) -> int:
    reqs = list(a.requirement or [])
    if a.requirements_file:
        reqs += [ln.strip() for ln in Path(a.requirements_file).read_text().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    wheels = sorted(Path(a.wheels).glob("*.whl")) if a.wheels else None
    data, ann = nr.build_py_payload(Path(a.project), requirements=reqs or None, wheels=wheels, python=a.python,
                                    arch=a.arch, glibc=a.glibc, find_links=[Path(p) for p in a.find_links or []])
    _write(Path(a.out), data, ann)
    return 0


def cmd_flcrs(a) -> int:
    fc = Path(a.flexcommunicator).expanduser()
    work = Path(a.work or Path(a.out) / ".build")
    crate = nr.flcrs_crate(fc, a.example, work)
    cfg = Path(a.config) if a.config else fc / "examples" / "flcrs" / "GettingStarted" / a.example / "config.yaml"
    for arch in a.arch:
        binary = nr.cargo_build(crate, arch, binary=a.example, target_dir=work / "target")
        files = {f"bin/{a.example}": binary}
        if cfg.exists():
            files["config.yaml"] = cfg.read_bytes()
        data, ann = nr.build_bin_payload(files, [f"bin/{a.example}"], arch=arch)
        _write(Path(a.out) / f"{a.example}-{arch}.tar.gz", data, ann)
    return 0


def cmd_release(a) -> int:
    layers = []
    for p in a.payloads:
        side = Path(str(p) + ".json")
        ann = json.loads(side.read_text()) if side.exists() else {"dev.flex.profile": "native"}
        layers.append((Path(p).read_bytes(), ann))
    key = crypto.load_private(a.key)
    if a.descriptor:            # the release's own descriptor (health, restart policy, parameters, resources ...)
        desc = json.loads(Path(a.descriptor).read_text())
        desc.update(module=a.module, releaseVersion=a.version)
        desc.setdefault("moduleVersion", a.version)
    else:
        desc = nr.native_descriptor(a.module, a.version, language=a.language)
    if a.health:
        desc["health"] = json.loads(a.health)
    info = nr.publish_native_release(Layout(a.oci), key, a.module, a.version, layers, descriptor=desc)
    print(json.dumps(info, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="native_release.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("py-payload", help="Python project + wheels/ + requirements.lock -> native payload")
    p.add_argument("project")
    p.add_argument("-r", "--requirement", action="append", help="requirement to download (repeatable)")
    p.add_argument("--requirements-file")
    p.add_argument("--wheels", help="use the wheels of this directory instead of downloading")
    p.add_argument("--find-links", action="append")
    p.add_argument("--python", default="3.12", help="target interpreter version (default 3.12)")
    p.add_argument("--arch", default="x86_64", help="target CPU: x86_64, aarch64, armv7l ... (default x86_64)")
    p.add_argument("--glibc", default="2.28", help="newest manylinux level the target supports (default 2.28)")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_py)
    p = sub.add_parser("flcrs", help="flcrs GettingStarted example -> one unit.json payload per CPU")
    p.add_argument("--flexcommunicator", required=True)
    p.add_argument("--example", default="flexCloud_variable")
    p.add_argument("--arch", action="append", required=True)
    p.add_argument("--config", help="config.yaml to ship (default: the example's)")
    p.add_argument("--work")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(fn=cmd_flcrs)
    p = sub.add_parser("release", help="payloads (+ .json annotations) -> one signed multi-layer native release")
    p.add_argument("--oci", required=True, help="OCI layout directory (created when missing)")
    p.add_argument("--key", required=True, help="Ed25519 release key file (base64 raw private key, as flexengine2.identity.crypto writes it)")
    p.add_argument("--module", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--language", default="python")
    p.add_argument("--health", help='descriptor health as JSON (default {"type": "process", ...})')
    p.add_argument("--descriptor", help="descriptor JSON file to publish instead of the minimal one (module and "
                                        "releaseVersion are set from --module / --version)")
    p.add_argument("payloads", nargs="+")
    p.set_defaults(fn=cmd_release)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())

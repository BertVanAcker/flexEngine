"""install.sh / build-wheels.sh on this host, without touching it: syntax, shellcheck, the wheel, --dry-run and the
full lifecycle staged with `--root <tmp>` (the fallback when no Ubuntu container is available), lock freshness,
the engine.yaml template, the systemd units, and the arm64 wheelhouse."""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from fe_pkg import DIST, FLEXENGINE, INSTALL, PACKAGING, VERSION, changes, sh

PREFIX = "/opt/flexengine"
PYTAG = f"cp{sys.version_info.major}{sys.version_info.minor}"


def install(*args, check=True) -> subprocess.CompletedProcess:
    r = sh(["bash", str(INSTALL), *args], timeout=900)
    if check:
        assert r.returncode == 0, f"install.sh {' '.join(args)}\n{r.stdout}\n{r.stderr}"
    return r


def snapshot(root: Path) -> dict:
    """path -> (mode, size, mtime_ns, sha) of everything under root."""
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        h = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, h)
    return out


# ---- static ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("script", ["install.sh", "build-wheels.sh"])
def test_bash_syntax(script):
    r = sh(["bash", "-n", str(PACKAGING / script)])
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["install.sh", "build-wheels.sh"])
def test_shellcheck(script):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    r = sh(["shellcheck", str(PACKAGING / script)])
    assert r.returncode == 0, r.stdout


def test_help_and_bad_options():
    assert "--uninstall" in install("--help").stdout
    assert install("--bogus", check=False).returncode == 2
    assert install("--purge", check=False).returncode == 2          # --purge needs --uninstall
    assert install("--prefix", "relative", check=False).returncode == 2


def test_units_render_and_options():
    unit = (PACKAGING / "systemd/flexengine.service").read_text()
    for line in ("Type=notify", "NotifyAccess=main", "WatchdogSec=30", "Restart=on-failure", "RestartSec=2",
                 "User=flex", "ExecStart=@PREFIX@/bin/flexengine run --config /etc/flexengine/engine.yaml",
                 "StateDirectory=flexengine", "RuntimeDirectory=flexengine", "ConfigurationDirectory=flexengine",
                 "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/var/lib/flexengine /etc/flexengine/tokens",
                 "PrivateTmp=yes", "ProtectHome=yes"):
        assert f"\n{line}\n" in unit, line
    for need in ("dialout", "docker", "podman", "RAUC", "SupplementaryGroups="):
        assert need in unit, need
    tmpl = (PACKAGING / "systemd/flexengine@.service").read_text()
    assert "--config /etc/flexengine/%i.yaml" in tmpl and "StateDirectory=flexengine-%i" in tmpl
    assert "Type=notify" in tmpl and "WatchdogSec=30" in tmpl


def test_systemd_analyze_verify_host(tmp_path):
    """Units with PREFIX resolved, verified by the host's systemd-analyze (ExecStart binary stubbed)."""
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not on this host (the docker test verifies inside Ubuntu 24.04)")
    exe = tmp_path / "opt/bin/flexengine"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    units = tmp_path / "units"
    units.mkdir()
    for u in ("flexengine.service", "flexengine@.service"):
        (units / u).write_text((PACKAGING / "systemd" / u).read_text().replace("@PREFIX@", str(tmp_path / "opt")))
    r = sh(["systemd-analyze", "verify", "--man=no", str(units / "flexengine.service"),
            str(units / "flexengine@.service")])
    if r.returncode != 0 and "Failed to" in r.stderr and "system" in r.stderr and "bus" in r.stderr.lower():
        pytest.skip(f"systemd-analyze cannot run here: {r.stderr.strip()[:200]}")
    # the instance template is only verifiable through an instance name
    errs = [l for l in r.stderr.splitlines() if "flexengine" in l and "Unit name" not in l and "template" not in l]
    assert r.returncode == 0 or not errs, r.stderr


# ---- the wheel + lock + template ---------------------------------------------------------------------------------------
def test_wheel_is_pure_python(wheel):
    assert wheel.name == f"flexengine_v2-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        meta = z.read(f"flexengine_v2-{VERSION}.dist-info/METADATA").decode()
        ep = z.read(f"flexengine_v2-{VERSION}.dist-info/entry_points.txt").decode()
    assert "flexengine2/__init__.py" in names and "flexengine2/local/sdnotify.py" in names
    assert not [n for n in names if n.endswith((".so", ".pyd", ".pyc"))]
    assert "Name: flexengine-v2" in meta and f"Version: {VERSION}" in meta
    for extra in ("mcu", "applab", "plc", "otel"):
        assert f"Provides-Extra: {extra}" in meta
    assert "flexengine = flexengine2.local.cli:main" in ep


def test_checksums(dist):
    sums = dist / "SHA256SUMS"
    if not sums.exists():
        pytest.skip("SHA256SUMS written by a full build-wheels.sh run")
    for line in sums.read_text().splitlines()[:20]:
        digest, name = line.split(maxsplit=1)
        p = dist / name.lstrip("./")
        if p.exists():
            assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, name


def test_lock_is_current():
    from importlib import metadata

    for d in ("smpclient", "pyads", "msgpack", "opentelemetry-sdk"):
        try:
            metadata.distribution(d)
        except metadata.PackageNotFoundError:
            pytest.skip(f"dev venv lacks {d}: the lock is generated from a venv with every extra")
    r = sh([sys.executable, str(PACKAGING / "gen-lock.py"), "--check"])
    assert r.returncode == 0, r.stderr
    lock = (PACKAGING / "requirements-lock.txt").read_text()
    for pin in ("cryptography==", "pyyaml==", "jsonschema==", "smpclient==7.3.0", "pyads==3.5.0"):
        assert pin in lock.lower(), pin
    assert "pytest" not in lock and "hypothesis" not in lock     # dev extras are not shipped


def test_template_validates(tmp_path):
    tmpl = (PACKAGING / "engine.yaml.template").read_text()
    assert "@DEVICE_ID@" in tmpl and "dataDir: /var/lib/flexengine" in tmpl
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    raw = tmp_path / "raw.yaml"
    raw.write_text(tmpl.replace("@DEVICE_ID@", "panda"))
    assert "${" not in tmpl                          # expanded even in comments by the loader
    filled = tmp_path / "engine.yaml"
    filled.write_text(tmpl.replace("@DEVICE_ID@", "panda").replace(
        "  # cloudPublicKey: <b64>", f"  cloudPublicKey: {key}"))
    cli = [sys.executable, "-m", "flexengine2", "config", "check"]
    probe = sh([*cli, "--help"])
    if probe.returncode != 0:
        pytest.skip("`flexengine config check` not available yet")
    r = sh([*cli, str(filled)])
    assert r.returncode == 0, r.stdout + r.stderr
    r = sh([*cli, str(raw)])                         # as installed: fails until the operator sets the key
    assert r.returncode == 1 and "cloudPublicKey" in (r.stdout + r.stderr)


# ---- --dry-run + --root lifecycle (no docker needed) -------------------------------------------------------------------
def test_dry_run_on_empty_root_changes_nothing(tmp_path, wheel):
    root = tmp_path / "root"
    r = install("--root", str(root), "--wheel", str(wheel), "--wheelhouse", str(DIST / "wheelhouse"), "--dry-run")
    assert not root.exists()
    assert "[dry-run]" in r.stdout and "nothing changed" in r.stdout and changes(r.stdout) > 0
    for planned in (f"-m venv {root}{PREFIX}", f"{root}/etc/flexengine/engine.yaml",
                    f"{root}/etc/systemd/system/flexengine.service", f"{root}/var/lib/flexengine"):
        assert planned in r.stdout, planned
    r = install("--root", str(root), "--uninstall", "--purge", "--dry-run")
    assert not root.exists() and changes(r.stdout) == 0


@pytest.mark.slow
def test_root_lifecycle(tmp_path, wheel, bumped_source):
    """install -> reinstall (0 changes) -> dry-run upgrade (nothing changes) -> upgrade (config + data kept)
    -> uninstall (state kept) -> purge, all staged under --root with this interpreter's wheelhouse."""
    wh = DIST / "wheelhouse" / f"linux_x86_64-{PYTAG}"
    if not sys.platform.startswith("linux") or not wh.is_dir():
        pytest.skip(f"no wheelhouse for this host ({wh.name}): build-wheels.sh --targets x86_64-"
                    f"{sys.version_info.major}.{sys.version_info.minor}")
    root = tmp_path / "root"
    base = ["--root", str(root), "--python", sys.executable, "--wheelhouse", str(DIST / "wheelhouse"),
            "--device-id", "panda", "--groups", "dialout,docker"]
    venv, etc, state = root / PREFIX.lstrip("/"), root / "etc/flexengine", root / "var/lib/flexengine"
    units = root / "etc/systemd/system"

    r = install(*base, "--wheel", str(wheel), "--extras", "mcu,applab")
    assert sh([str(venv / "bin/flexengine"), "version"]).stdout.strip() == VERSION
    assert f"changes: {changes(r.stdout)}" in r.stdout and changes(r.stdout) > 5
    unit = (units / "flexengine.service").read_text()
    assert f"ExecStart={PREFIX}/bin/flexengine run --config /etc/flexengine/engine.yaml" in unit
    assert "@PREFIX@" not in unit and "@PREFIX@" not in (units / "flexengine@.service").read_text()
    assert "SupplementaryGroups=dialout docker" in (units / "flexengine.service.d/10-groups.conf").read_text()
    cfg = (etc / "engine.yaml").read_text()
    assert "deviceId: panda" in cfg and "@DEVICE_ID@" not in cfg
    assert oct(etc.stat().st_mode & 0o777) == "0o750" and oct(state.stat().st_mode & 0o777) == "0o700"
    assert oct((etc / "engine.yaml").stat().st_mode & 0o777) == "0o640"
    sh([str(venv / "bin/python"), "-c", "import smpclient, msgpack"]).check_returncode()   # extras installed

    # reinstall: idempotent, nothing touched
    before = snapshot(root)
    r = install(*base, "--wheel", str(wheel), "--extras", "mcu,applab")
    assert changes(r.stdout) == 0, r.stdout
    assert snapshot(root) == before

    # operator edits + engine data
    (etc / "engine.yaml").write_text(cfg + "# edited by the operator\n")
    (state / "panda").mkdir()
    (state / "panda/state.json").write_text('{"manifestVersion": 7}')
    kept = {p: (etc / p).read_bytes() for p in ("engine.yaml",)}

    # dry-run of the upgrade: plan printed, nothing changes
    before = snapshot(root)
    r = install(*base, "--from-source", str(bumped_source), "--extras", "mcu,applab", "--dry-run")
    assert "nothing changed" in r.stdout and changes(r.stdout) >= 1
    assert snapshot(root) == before

    # upgrade: new version, config + data kept
    r = install(*base, "--from-source", str(bumped_source), "--extras", "mcu,applab")
    assert sh([str(venv / "bin/flexengine"), "version"]).stdout.strip() == f"{VERSION}.post1"
    assert f"upgraded from {VERSION}" in r.stdout
    assert (etc / "engine.yaml").read_bytes() == kept["engine.yaml"]
    assert (state / "panda/state.json").read_text() == '{"manifestVersion": 7}'
    assert "kept (exists, never overwritten)" in r.stdout

    # back to the wheel (a downgrade is an install like any other)
    install(*base, "--wheel", str(wheel), "--extras", "mcu,applab")
    assert sh([str(venv / "bin/flexengine"), "version"]).stdout.strip() == VERSION

    # uninstall: venv + units gone, config + data kept; idempotent
    r = install("--root", str(root), "--uninstall")
    assert not venv.exists() and not (units / "flexengine.service").exists()
    assert not (units / "flexengine.service.d").exists()
    assert (etc / "engine.yaml").read_bytes() == kept["engine.yaml"]
    assert (state / "panda/state.json").exists()
    assert changes(install("--root", str(root), "--uninstall").stdout) == 0

    # purge: everything gone
    install("--root", str(root), "--uninstall", "--purge")
    assert not etc.exists() and not state.exists()
    assert changes(install("--root", str(root), "--uninstall", "--purge").stdout) == 0


@pytest.mark.slow
def test_root_no_service(tmp_path, wheel):
    """--no-service (Mac/dev layout): venv + dirs + config, no unit."""
    wh = DIST / "wheelhouse" / f"linux_x86_64-{PYTAG}"
    if not sys.platform.startswith("linux") or not wh.is_dir():
        pytest.skip(f"no wheelhouse {wh.name} for this host")
    root = tmp_path / "root"
    install("--root", str(root), "--python", sys.executable, "--wheelhouse", str(DIST / "wheelhouse"),
            "--wheel", str(wheel), "--no-service")
    assert (root / "opt/flexengine/bin/flexengine").exists()
    assert (root / "etc/flexengine/engine.yaml").exists()
    assert not (root / "etc/systemd").exists()


def test_refuses_foreign_prefix(tmp_path, wheel):
    root = tmp_path / "root"
    (root / "opt/flexengine").mkdir(parents=True)
    (root / "opt/flexengine/precious.txt").write_text("not a venv")
    r = install("--root", str(root), "--python", sys.executable, "--wheel", str(wheel), check=False)
    assert r.returncode != 0 and "not a venv" in (r.stdout + r.stderr)
    r = install("--root", str(root), "--uninstall", check=False)
    assert r.returncode != 0 and (root / "opt/flexengine/precious.txt").exists()


# ---- arm64 -------------------------------------------------------------------------------------------------------------
ARM_TAGS = ("cp311", "cp313")


@pytest.mark.parametrize("cp", ARM_TAGS)
def test_arm64_wheelhouse_resolves(dist, wheel, cp, tmp_path):
    """The aarch64 wheelhouse (pip download --platform manylinux_*_aarch64 --only-binary=:all:) holds a complete,
    binary-only install set for flexengine-v2[applab,otel]: pip resolves it offline for that platform."""
    wh = dist / "wheelhouse" / f"linux_aarch64-{cp}"
    if not wh.is_dir():
        pytest.skip(f"{wh.name} not built (build-wheels.sh --targets aarch64-{cp[2]}.{cp[3:]})")
    names = [p.name for p in wh.glob("*.whl")]
    assert any(n.startswith("cryptography-") and "aarch64" in n for n in names), names
    assert any(n.startswith("pyyaml-") and "aarch64" in n.lower() for n in names), names
    assert not [n for n in names if "x86_64" in n]
    pyv = f"{cp[2]}.{cp[3:]}"
    plats = [a for t in ("manylinux_2_35", "manylinux_2_34", "manylinux_2_28", "manylinux_2_17", "manylinux2014",
                         "linux") for a in ("--platform", f"{t}_aarch64")]
    r = sh([sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--dry-run", "--quiet",
            "--report", str(tmp_path / "r.json"), "--ignore-installed", "--target", str(tmp_path / "t"),
            "--no-index", "--find-links", str(wh), "--only-binary=:all:", "--python-version", pyv,
            "--implementation", "cp", *plats, "-c", str(PACKAGING / "requirements-lock.txt"),
            f"{wheel}[applab,otel]"], timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    import json

    got = {i["metadata"]["name"].lower() for i in json.loads((tmp_path / "r.json").read_text())["install"]}
    assert {"flexengine-v2", "cryptography", "pyyaml", "jsonschema", "msgpack", "opentelemetry-sdk"} <= got


@pytest.mark.docker
@pytest.mark.slow
def test_arm64_emulated_install(dist, wheel):
    probe = sh(["docker", "run", "--rm", "--platform", "linux/arm64", "ubuntu:24.04", "true"], timeout=300)
    if probe.returncode != 0:
        pytest.skip("docker cannot run linux/arm64 here (no qemu-user binfmt): "
                    f"{(probe.stderr or probe.stdout).strip().splitlines()[-1][:160]}; "
                    "arm64 is covered by test_arm64_wheelhouse_resolves")
    wh = dist / "wheelhouse" / "linux_aarch64-cp313"
    if not wh.is_dir():
        pytest.skip("linux_aarch64-cp313 wheelhouse not built")
    r = sh(["docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
            "-v", f"{PACKAGING}:/src/packaging:ro", "-v", f"{dist}:/src/dist:ro", "python:3.13", "bash", "-c",
            f"/src/packaging/install.sh --no-service --root /tmp/r --wheel /src/dist/{wheel.name} "
            "--wheelhouse /src/dist/wheelhouse --extras applab && /tmp/r/opt/flexengine/bin/flexengine version"],
           timeout=1800)
    assert r.returncode == 0 and r.stdout.strip().endswith(VERSION), r.stdout + r.stderr


def test_flexengine_dir_layout():
    for f in ("install.sh", "build-wheels.sh", "gen-lock.py", "requirements-lock.txt", "engine.yaml.template",
              "README.md", "systemd/flexengine.service", "systemd/flexengine@.service"):
        assert (PACKAGING / f).is_file(), f
    gi = (FLEXENGINE.parent / ".gitignore").read_text().splitlines()
    assert "flexengine/dist/" in gi

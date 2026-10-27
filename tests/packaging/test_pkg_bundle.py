"""FE-3: the offline bundle a release publishes installs from itself. build-wheels.sh --bundle-only packs a given
engine wheel and wheelhouse the way release.yml does; the unpacked bundle's install.sh, run with no source options,
picks that wheel and that wheelhouse (offline: pip --no-index). No network, no docker: the wheels are stand-ins and
install.sh runs --dry-run under --root."""

from __future__ import annotations

import platform
import sys
import tarfile

import pytest

from fe_pkg import PACKAGING, VERSION, sh

ARCH = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
CP = f"cp{sys.version_info.major}{sys.version_info.minor}"
PYV = f"{sys.version_info.major}.{sys.version_info.minor}"


def _dist(tmp_path):
    dist = tmp_path / "dist"
    wh = dist / "wheelhouse" / f"linux_{ARCH}-{CP}"
    wh.mkdir(parents=True)
    (wh / "standin-1.0-py3-none-any.whl").write_bytes(b"stand-in")
    given = tmp_path / "pypi" / f"flexengine-{VERSION}-py3-none-any.whl"
    given.parent.mkdir()
    given.write_bytes(b"the wheel that went to PyPI")
    return dist, given


def test_release_bundle_installs_from_itself(tmp_path):
    dist, given = _dist(tmp_path)
    r = sh(["bash", str(PACKAGING / "build-wheels.sh"), "--out", str(dist), "--wheel", str(given), "--bundle-only",
            "--targets", f"{ARCH}-{PYV}"], timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    bundle = dist / f"flexengine-{VERSION}-linux_{ARCH}.tar.gz"
    sums = (dist / "SHA256SUMS").read_text()
    assert f"./{bundle.name}" in sums and f"./{given.name}" in sums
    top = f"flexengine-{VERSION}-linux_{ARCH}"
    with tarfile.open(bundle) as t:
        names = t.getnames()
        assert {f"{top}/packaging/install.sh", f"{top}/{given.name}",
                f"{top}/wheelhouse/linux_{ARCH}-{CP}/standin-1.0-py3-none-any.whl"} <= set(names)
        assert all(n == top or n.startswith(top + "/") for n in names)
        t.extractall(tmp_path / "unpacked", filter="data")
    b = tmp_path / "unpacked" / top
    assert (b / given.name).read_bytes() == given.read_bytes()      # the published wheel, not a rebuild
    root = tmp_path / "root"
    r = sh(["bash", str(b / "packaging" / "install.sh"), "--root", str(root), "--python", sys.executable,
            "--device-id", "panda", "--dry-run"], timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"flexEngine {VERSION} from {b}/{given.name}" in r.stdout
    assert f"offline wheelhouse {b}/wheelhouse/linux_{ARCH}-{CP}" in r.stdout
    assert "--no-index" in r.stdout and "nothing changed" in r.stdout and not root.exists()


def test_given_wheel_must_match_the_checkout_version(tmp_path):
    dist, given = _dist(tmp_path)
    wrong = given.with_name("flexengine-0.0.1-py3-none-any.whl")
    given.rename(wrong)
    r = sh(["bash", str(PACKAGING / "build-wheels.sh"), "--out", str(dist), "--wheel", str(wrong), "--bundle-only",
            "--targets", f"{ARCH}-{PYV}"], timeout=120)
    assert r.returncode == 2 and "not flexengine-" in r.stderr


@pytest.mark.parametrize("job", ["bundles"])
def test_release_workflow_bundles_the_published_wheel(job):
    import yaml

    wf = yaml.safe_load((PACKAGING.parent / ".github" / "workflows" / "release.yml").read_text())
    j = wf["jobs"][job]
    assert "build" in j["needs"]
    run = " ".join(s.get("run") or "" for s in j["steps"])
    assert "build-wheels.sh" in run and "--wheel" in run
    gh = " ".join(s.get("run") or "" for s in wf["jobs"]["github-release"]["steps"])
    assert "SHA256SUMS" in gh

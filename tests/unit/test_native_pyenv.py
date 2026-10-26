"""EV-36/37: native payloads with their own Python dependencies and per-platform native layers
(drivers/native/pyenv.py, used by drivers/process.py and drivers/native/systemd.py).

Payload checks (lock parsing, wheel tags, annotations), the venv build per digest with a tiny wheel built here (no
network), offline + hash-checked install, durability (marker after the venv), tag-mismatch refusal, the systemd
ExecStart with the venv's python, and the release tooling (testing/native_release.py)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.drivers.base import DriverError, Workload, wait_until
from flexengine2.drivers.native import pyenv
from flexengine2.drivers.native.systemd import SystemdDriver
from flexengine2.drivers.process import ProcessDriver
from flexengine2.testing import native_release as nr

FLEXENGINE = Path(__file__).resolve().parents[2]
CONTRACT = FLEXENGINE / "tests" / "contract"
if str(CONTRACT) not in sys.path:
    sys.path.insert(0, str(CONTRACT))

from spi_harness.base import Release  # noqa: E402

GLIBC = {"impl": "cpython", "version": [3, 12], "machine": "x86_64", "bits": 64, "libc": "glibc", "glibc": "2.39",
         "freeThreaded": False, "arch": "x86_64", "pyabi": "cp312"}
ARM = {**GLIBC, "machine": "aarch64", "arch": "aarch64"}
MUSL = {**GLIBC, "libc": "musl", "glibc": None}
FT = {**GLIBC, "version": [3, 13], "freeThreaded": True, "pyabi": "cp313t"}


# ---- wheel tags ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,info,fits", [
    ("redis-8.1.0-py3-none-any.whl", GLIBC, True),
    ("six-1.16.0-py2.py3-none-any.whl", GLIBC, True),
    ("x-1.0-py313-none-any.whl", GLIBC, False),                       # needs 3.13
    ("numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl", GLIBC, True),
    ("numpy-2.5.4-cp313-cp313-manylinux_2_27_x86_64.whl", GLIBC, False),   # other CPython
    ("numpy-2.5.4-cp312-cp312-manylinux_2_27_aarch64.whl", GLIBC, False),  # other CPU
    ("numpy-2.5.4-cp312-cp312-manylinux_2_27_aarch64.whl", ARM, True),
    ("x-1.0-cp312-cp312-manylinux_2_40_x86_64.whl", GLIBC, False),          # needs a newer glibc
    ("pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl", GLIBC, True),
    ("x-1.0-cp312-cp312-manylinux1_x86_64.whl", GLIBC, True),
    ("bcrypt-5.0.0-cp39-abi3-manylinux_2_28_x86_64.whl", GLIBC, True),      # abi3 >= 3.9
    ("bcrypt-5.0.0-cp313-abi3-manylinux_2_28_x86_64.whl", GLIBC, False),    # abi3 >= 3.13
    ("bcrypt-5.0.0-cp39-abi3-manylinux_2_28_x86_64.whl", FT, False),        # no abi3 on free-threaded
    ("x-1.0-cp313-cp313t-manylinux_2_28_x86_64.whl", FT, True),
    ("x-1.0-cp312-cp312-musllinux_1_2_x86_64.whl", GLIBC, False),
    ("x-1.0-cp312-cp312-musllinux_1_2_x86_64.whl", MUSL, True),
    ("x-1.0-cp312-cp312-manylinux_2_28_x86_64.whl", MUSL, False),
    ("x-1.0-cp312-cp312-linux_x86_64.whl", GLIBC, True),
    ("x-1.0-cp312-none-any.whl", GLIBC, True),
    ("x-1.0-cp311-none-any.whl", GLIBC, False),
])
def test_wheel_fits(name, info, fits):
    assert pyenv.wheel_fits(name, info) is fits


def test_wheel_tags_and_errors():
    assert pyenv.wheel_tags("a-1-1build-py3-none-any.whl") == ("py3", "none", "any")
    with pytest.raises(ValueError):
        pyenv.wheel_tags("a-1.tar.gz")
    with pytest.raises(ValueError):
        pyenv.wheel_tags("a-1.whl")


def test_normalize_arch_and_platform():
    assert pyenv.normalize_arch("AMD64") == "x86_64" and pyenv.normalize_arch("arm64") == "aarch64"
    assert pyenv.normalize_arch("aarch64", 32) == "armv7l" and pyenv.normalize_arch("x86_64", 32) == "i686"
    info = pyenv.interpreter_info()
    p = pyenv.platform_of(info)
    assert p == {"dev.flex.arch": info["arch"], "dev.flex.pyabi": f"cp{sys.version_info[0]}{sys.version_info[1]}"
                 + ("t" if info["freeThreaded"] else "")}
    assert ProcessDriver("dev", Path("/tmp/none-ev36")).platform() == p


def test_interpreter_info_other_interpreter_and_unrunnable(tmp_path):
    shim = tmp_path / "py"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    shim.chmod(0o755)
    assert pyenv.interpreter_info(str(shim))["pyabi"] == pyenv.interpreter_info()["pyabi"]
    with pytest.raises(DriverError, match="payload interpreter .* cannot run"):
        pyenv.interpreter_info(str(tmp_path / "missing-python"))
    d = ProcessDriver("dev", tmp_path / "d", python=str(tmp_path / "missing-python"))
    with pytest.raises(DriverError):                       # deferred, never a refusal
        d.platform()
    with pytest.raises(DriverError):
        d.preflight(Release(str(tmp_path / "x.tar.gz"), {}), {})
    assert "platform" not in d.capabilities()


# ---- requirements.lock --------------------------------------------------------------------------------------------
def test_parse_lock():
    lock = pyenv.parse_lock("# comment\nRedis==8.1.0 --hash=sha256:AB \\\n    --hash=sha256:cd\n\n"
                            "py_yaml[x]==6.0 ; python_version >= '3.9' --hash=sha256:ef  # trailing\n")
    assert lock == {"redis": {"line": 2, "version": "8.1.0", "hashes": {"ab", "cd"}},
                    "py-yaml": {"line": 5, "version": "6.0", "hashes": {"ef"}}}
    for bad in ("redis==8.1.0\n", "redis>=8 --hash=sha256:ab\n", "--index-url https://pypi.org/simple\n",
                "redis==8.1.0 --hash=md5:ab\n"):
        with pytest.raises(DriverError, match=r"requirements.lock line 1 is not a pinned requirement with --hash"):
            pyenv.parse_lock(bad)


# ---- tiny wheel built in the test ---------------------------------------------------------------------------------
def make_wheel(dest: Path, name: str = "tinydep", version: str = "1.0", tag: str = "py3-none-any",
               body: str = "VALUE = 'from-the-payload-venv'\n") -> Path:
    files = {f"{name}/__init__.py": body.encode(),
             f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
             f"{name}-{version}.dist-info/WHEEL": f"Wheel-Version: 1.0\nGenerator: ev36-test\nRoot-Is-Purelib: true\n"
                                                  f"Tag: {tag}\n".encode()}
    record = ""
    for n, b in files.items():
        h = base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode()
        record += f"{n},sha256={h},{len(b)}\n"
    record += f"{name}-{version}.dist-info/RECORD,,\n"
    files[f"{name}-{version}.dist-info/RECORD"] = record.encode()
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(p, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return p


APP = r'''import os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import tinydep
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (tinydep.VALUE + "|" + sys.prefix).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer.allow_reuse_address = True
srv = HTTPServer(("127.0.0.1", int(os.environ["FLEX_HEALTH_PORT"])), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
threading.Event().wait()
'''


def project(tmp: Path, app: str = APP) -> Path:
    p = tmp / "project"
    p.mkdir(parents=True, exist_ok=True)
    (p / "app.py").write_text(app)
    return p


def payload(tmp: Path, wheels: list[Path], lock: str | None = None, extra: dict | None = None) -> Path:
    data, _ = nr.build_py_payload(project(tmp), wheels=wheels)
    if lock is not None or extra:
        files = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for m in tf.getmembers():
                files[m.name] = (tf.extractfile(m).read(), m.mode)
        if lock is not None:
            files["requirements.lock"] = (lock.encode(), 0o644)
        files.update(extra or {})
        data = nr.targz_tree(files)
    out = tmp / f"payload-{hashlib.sha256(data).hexdigest()[:12]}.tar.gz"
    out.write_bytes(data)
    return out


def workload(path: Path, dep: str = "dep-py", **kw) -> Workload:
    dg = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return Workload(dep_id=dep, module="tiny", version="1.0.0", digest=dg, payload_path=str(path),
                    payload_annotations={"dev.flex.profile": "native"},
                    health={"type": "http", "path": "/healthz"}, restart={"policy": "never"}, **kw)


def _get(port: int) -> str:
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    c.request("GET", "/healthz")
    return c.getresponse().read().decode()


def test_process_driver_runs_app_in_its_own_venv(tmp_path, monkeypatch):
    # the engine's environment points pip at an unreachable index/proxy: the install must not need it
    for k in ("PIP_INDEX_URL", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9/")
    whl = make_wheel(tmp_path / "w")
    p = payload(tmp_path, [whl])
    d = ProcessDriver("dev-ev36", tmp_path / "data")
    assert d.preflight(Release(str(p), {"dev.flex.profile": "native"}), {}) is None
    wl = workload(p)
    try:
        d.apply(None, wl)
        u = d.units[wl.dep_id]
        assert wait_until(lambda: d.health(wl.dep_id, wl.health), 15, 0.1), (Path(u["dir"]) / "app.log").read_text()
        tree = Path(u["dir"])
        assert u["argv"] == [str(tree / ".venv" / "bin" / "python"), "app.py"]
        value, prefix = _get(u["port"]).split("|")
        assert value == "from-the-payload-venv" and Path(prefix) == tree / ".venv"
        assert (tree / ".venv.complete").exists()
        assert d.health(wl.dep_id, {"type": "process"})
        # idempotent re-apply and a new driver instance: no second venv build
        mark = (tree / ".venv.complete").stat().st_mtime_ns
        ProcessDriver("dev-ev36", tmp_path / "data").apply(wl, wl)
        assert (tree / ".venv.complete").stat().st_mtime_ns == mark
    finally:
        ProcessDriver("dev-ev36", tmp_path / "data").remove(wl.dep_id)
    with pytest.raises(ImportError):                       # the engine's own environment is untouched
        __import__("tinydep")


def test_payload_without_lock_runs_as_before(tmp_path):
    p = payload(tmp_path, [])
    with tarfile.open(p) as tf:
        assert "requirements.lock" not in tf.getnames()
    d = ProcessDriver("dev-ev36b", tmp_path / "data")
    tree, argv = d._prepare(workload(p))
    assert argv == [sys.executable, "app.py"] and not (tree / ".venv").exists()
    assert pyenv.ensure_venv(tree) is None and pyenv.payload_python(tree, "/usr/bin/python3") == "/usr/bin/python3"


def test_unit_json_exec_start_on_the_process_driver(tmp_path):
    files = {"unit.json": (json.dumps({"ExecStart": ["bin/node", "--flag"]}).encode(), 0o644),
             "bin/node": (b"#!/bin/sh\nexec sleep 30\n", 0o755)}
    p = tmp_path / "bin.tar.gz"
    p.write_bytes(nr.targz_tree(files))
    d = ProcessDriver("dev-ev36c", tmp_path / "data")
    wl = workload(p, dep="dep-bin")
    try:
        d.apply(None, wl)
        u = d.units[wl.dep_id]
        assert u["argv"] == [str(Path(u["dir"]) / "bin" / "node"), "--flag"]
        assert d.health(wl.dep_id, {"type": "process"}) and not d.health(wl.dep_id, {"type": "http"})
    finally:
        d.remove(wl.dep_id)


def test_venv_install_failure_is_a_driver_error_and_leaves_no_marker(tmp_path):
    whl = make_wheel(tmp_path / "w")
    bad = f"tinydep==1.0 --hash=sha256:{'0' * 64}\n"
    p = payload(tmp_path, [whl], lock=bad)
    d = ProcessDriver("dev-ev36d", tmp_path / "data")
    assert "does not match its --hash" in d.preflight(Release(str(p), {}), {})
    with pytest.raises(DriverError, match="offline install of the payload's requirements.lock failed") as ei:
        d.apply(None, workload(p))
    assert contracts.reason_code(str(ei.value), "detail")["code"].startswith("FE-NAT-")
    trees = list((tmp_path / "data" / "native" / "dep-py").iterdir())
    assert trees and not (trees[0] / ".venv.complete").exists() and not (trees[0] / ".venv").exists()


def test_venv_create_failure(tmp_path):
    whl = make_wheel(tmp_path / "w")
    p = payload(tmp_path, [whl])
    fake = tmp_path / "nopython"
    fake.write_text("#!/bin/sh\necho 'No module named venv' >&2\nexit 1\n")
    fake.chmod(0o755)
    tree = tmp_path / "tree"
    with tarfile.open(p) as tf:
        tf.extractall(tree, filter="data")
    with pytest.raises(DriverError, match="cannot create the payload venv with .*No module named venv"):
        pyenv.ensure_venv(tree, str(fake))


def test_interrupted_venv_build_is_rebuilt(tmp_path):
    whl = make_wheel(tmp_path / "w")
    p = payload(tmp_path, [whl])
    tree = tmp_path / "tree"
    with tarfile.open(p) as tf:
        tf.extractall(tree, filter="data")
    (tree / ".venv" / "bin").mkdir(parents=True)        # a crash before the marker: half a venv
    (tree / ".venv" / "bin" / "python").write_text("broken")
    py = pyenv.ensure_venv(tree)
    assert py == str(tree / ".venv" / "bin" / "python") and Path(py).resolve().is_file()
    assert (tree / ".venv.complete").exists()
    assert pyenv.ensure_venv(tree) == py                   # marker: no rebuild


# ---- preflight refusals -------------------------------------------------------------------------------------------
def test_preflight_refusals(tmp_path):
    info = pyenv.interpreter_info()
    other_arch = "aarch64" if info["arch"] != "aarch64" else "x86_64"
    whl = make_wheel(tmp_path / "w")
    d = ProcessDriver("dev-ev36e", tmp_path / "data")
    sd = SystemdDriver("dev-ev36e", tmp_path / "sdata", unit_dir=tmp_path / "units")
    ok = payload(tmp_path, [whl])
    cases = {
        "arch": (ok, {"dev.flex.arch": other_arch}, f"native payload is built for {other_arch} (dev.flex.arch), this "
                                                    f"device is {info['arch']}"),
        "pyabi": (ok, {"dev.flex.pyabi": "cp27"}, f"native payload is built for Python cp27 (dev.flex.pyabi), the "
                                                  f"device's interpreter is {info['pyabi']}"),
        "tags": (payload(tmp_path, [whl, make_wheel(tmp_path / "w2", "armdep", "2.0",
                                                    f"cp312-cp312-manylinux_2_17_{other_arch}")]), {},
                 f"wheel armdep-2.0-cp312-cp312-manylinux_2_17_{other_arch}.whl does not fit this device"),
        "sdist": (payload(tmp_path, [whl], extra={"wheels/tinydep-1.0.tar.gz": (b"x", 0o644)}), {},
                  "wheels/tinydep-1.0.tar.gz is not a wheel (the device never builds from source)"),
        "unlocked": (payload(tmp_path, [whl], lock=""), {}, "wheel tinydep-1.0-py3-none-any.whl is not in "
                                                            "requirements.lock"),
        "missing": (payload(tmp_path, [], extra={"requirements.lock": (b"other==1.0 --hash=sha256:aa\n", 0o644)}), {},
                    "requirements.lock pins other==1.0 but wheels/ has no wheel for it"),
        "unpinned": (payload(tmp_path, [whl], lock="tinydep\n"), {},
                     "requirements.lock line 1 is not a pinned requirement with --hash: tinydep"),
    }
    for case, (path, ann, want) in cases.items():
        for drv in (d, sd):
            r = drv.preflight(Release(str(path), {"dev.flex.profile": "native", **ann}, {"restartPolicy": {}}), {})
            assert r and r.startswith(want), (case, drv.name, r)
            assert contracts.reason_code(r, "detail"), (case, r)
    assert not (tmp_path / "data" / "native" / "dep-py").exists()   # nothing unpacked by preflight
    nontar = tmp_path / "x.tar.gz"
    nontar.write_bytes(b"not a tar")
    assert d.preflight(Release(str(nontar), {}), {}).startswith("native payload is not a tar.gz archive")
    matching = {"dev.flex.profile": "native", **pyenv.platform_of(info)}
    assert d.preflight(Release(str(ok), matching), {}) is None
    assert sd.preflight(Release(str(ok), matching, {"restartPolicy": {}}), {}) is None


def test_systemd_exec_start_uses_the_venv_python(tmp_path):
    sd = SystemdDriver("panda", tmp_path, unit_dir=tmp_path / "units", python="/usr/bin/python3")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "app.py").write_text("")
    assert sd._exec_start(tree) == ["/usr/bin/python3", "app.py"]
    (tree / "requirements.lock").write_text("")
    assert sd._exec_start(tree) == [str(tree / ".venv" / "bin" / "python"), "app.py"]
    (tree / "unit.json").write_text(json.dumps({"ExecStart": ["{python}", "-m", "node"]}))
    assert sd._exec_start(tree) == [str(tree / ".venv" / "bin" / "python"), "-m", "node"]
    caps = sd.capabilities()
    assert caps["payloadPython"] == "/usr/bin/python3"


def test_build_drivers_passes_the_payload_interpreter(tmp_path):
    from flexengine2.core.reconciler import build_drivers

    d = build_drivers({"deviceId": "x", "drivers": {"native": {"runtime": "process", "python": "/opt/py"}}}, tmp_path)
    assert type(d["native"]).__name__ == "ProcessDriver" and d["native"].python == "/opt/py"
    d = build_drivers({"deviceId": "x", "drivers": {"native": {"runtime": "systemd", "python": "/opt/py",
                                                                "unitDir": str(tmp_path)}}}, tmp_path)
    assert d["native"].python == "/opt/py"
    assert build_drivers({"deviceId": "x", "drivers": {"native": True}}, tmp_path)["native"].python == sys.executable


# ---- release tooling ----------------------------------------------------------------------------------------------
def test_lock_from_wheels_and_annotations(tmp_path):
    a = make_wheel(tmp_path / "w", "pure", "1.0")
    b = make_wheel(tmp_path / "w", "Plat_Dep", "2.0", "cp312-cp312-manylinux_2_17_aarch64")
    lock = nr.lock_from_wheels([b, a])
    parsed = pyenv.parse_lock(lock)
    assert list(parsed) == ["plat-dep", "pure"]
    assert parsed["pure"]["hashes"] == {hashlib.sha256(a.read_bytes()).hexdigest()}
    _, ann = nr.build_py_payload(project(tmp_path), wheels=[a], python="3.12", arch="aarch64")
    assert ann == {"dev.flex.profile": "native"}           # pure wheels: fits any CPU/Python
    _, ann = nr.build_py_payload(project(tmp_path), wheels=[a, b], python="3.12", arch="aarch64")
    assert ann == {"dev.flex.profile": "native", "dev.flex.arch": "aarch64", "dev.flex.pyabi": "cp312"}
    with pytest.raises(ValueError, match="two wheels"):
        nr.lock_from_wheels([a, make_wheel(tmp_path / "w3", "pure", "1.1")])
    with pytest.raises(ValueError, match="needs app.py or unit.json"):
        (tmp_path / "empty").mkdir()
        nr.build_py_payload(tmp_path / "empty")


def test_pip_platforms():
    tags = nr.pip_platforms("aarch64", "2.28")
    assert tags[0] == "manylinux_2_28_aarch64" and "manylinux_2_17_aarch64" in tags
    assert "manylinux2014_aarch64" in tags and tags[-1] == "linux_aarch64" and "manylinux1_aarch64" not in tags
    assert "manylinux1_x86_64" in nr.pip_platforms("x86_64")


def test_bin_payload_and_deterministic_tar(tmp_path):
    exe = tmp_path / "node"
    exe.write_bytes(b"\x7fELF...")
    data, ann = nr.build_bin_payload({"bin/node": exe, "config.yaml": b"a: 1\n"}, ["bin/node"], arch="aarch64")
    assert ann == {"dev.flex.profile": "native", "dev.flex.arch": "aarch64"}
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        modes = {m.name: m.mode for m in tf.getmembers()}
        assert json.loads(tf.extractfile("unit.json").read()) == {"ExecStart": ["bin/node"]}
    assert modes == {"bin/node": 0o755, "config.yaml": 0o644, "unit.json": 0o644}
    assert nr.build_bin_payload({"bin/node": exe}, ["bin/node"], arch="aarch64")[0] == \
        nr.build_bin_payload({"bin/node": exe}, ["bin/node"], arch="aarch64")[0]
    with pytest.raises(ValueError):
        nr.build_bin_payload({"x": b""}, ["bin/node"], arch="x86_64")


def test_native_descriptor_validates():
    contracts.validate("descriptor", nr.native_descriptor("flcpy_node", "1.0.0"))


def test_store_picks_the_fitting_layer(tmp_path):
    """Layer selection (artefacts/store.py): absent keys fit, contradicting keys do not, the most specific wins."""
    from flexengine2.artefacts.oci import Layout
    from flexengine2.artefacts.store import ArtefactStore, VerificationError
    from flexengine2.identity import crypto

    key = crypto.generate()
    lay = Layout(tmp_path / "oci")
    plat = {"dev.flex.arch": "x86_64", "dev.flex.pyabi": "cp312"}
    layers = [(b"generic", {"dev.flex.profile": "native"}),
              (b"arm", {"dev.flex.profile": "native", "dev.flex.arch": "aarch64"}),
              (b"x86-cp312", {"dev.flex.profile": "native", **plat}),
              (b"x86-cp313", {"dev.flex.profile": "native", "dev.flex.arch": "x86_64", "dev.flex.pyabi": "cp313"})]
    info = nr.publish_native_release(lay, key, "multi", "1.0.0", [(nr.targz_tree({"app.py": (d, 0o644)}), a)
                                                                  for d, a in layers])
    store = ArtefactStore(tmp_path / "eng", None, [crypto.public_b64(key)])
    for blob in (tmp_path / "oci" / "blobs" / "sha256").iterdir():
        store.put("sha256:" + blob.name, blob.read_bytes())
    idx = store.root.parent / "referrers" / info["digest"].split(":")[1]
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps(lay.referrers(info["digest"])))

    def pick(p):
        rel = store.verify_release(info["repository"], info["digest"], "native", platform=p)
        with tarfile.open(rel.payload_path) as tf:
            return tf.extractfile("app.py").read()
    assert pick(plat) == b"x86-cp312"
    assert pick({"dev.flex.arch": "aarch64", "dev.flex.pyabi": "cp312"}) == b"arm"
    assert pick({"dev.flex.arch": "riscv64", "dev.flex.pyabi": "cp312"}) == b"generic"
    assert pick(None) == b"generic"
    info2 = nr.publish_native_release(lay, key, "multi", "2.0.0", [(nr.targz_tree({"app.py": (d, 0o644)}), a)
                                                                   for d, a in layers[1:2] + layers[3:]])
    for blob in (tmp_path / "oci" / "blobs" / "sha256").iterdir():
        store.put("sha256:" + blob.name, blob.read_bytes())
    (idx.parent / info2["digest"].split(":")[1]).write_text(json.dumps(lay.referrers(info2["digest"])))
    with pytest.raises(VerificationError, match="release has no 'native' payload for platform"):
        store.verify_release(info2["repository"], info2["digest"], "native", platform=plat)


def test_reason_texts_are_registered():
    for text in ("native payload is built for aarch64 (dev.flex.arch), this device is x86_64",
                 "native payload is built for Python cp313 (dev.flex.pyabi), the device's interpreter is cp312",
                 "wheel x-1-cp312-cp312-linux_armv7l.whl does not fit this device (Python cp312, x86_64, glibc 2.39)",
                 "payload interpreter /opt/py cannot run: boom",
                 "payload venv (install): no answer within 600 s",
                 "cannot create the payload venv with /usr/bin/python3: No module named venv"):
        assert contracts.reason_code(text, "detail"), text


def test_native_release_cli(tmp_path):
    """flexengine/tools/native_release.py: py-payload (offline, from local wheels) + release (multi-layer, signed)."""
    import importlib.util

    from flexengine2.artefacts.oci import Layout
    from flexengine2.identity import crypto

    spec = importlib.util.spec_from_file_location("native_release_cli", FLEXENGINE / "tools" / "native_release.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    w = tmp_path / "w"
    make_wheel(w)
    make_wheel(w, "armdep", "2.0", "cp312-cp312-manylinux_2_17_aarch64")
    out = tmp_path / "out" / "node-arm.tar.gz"
    assert cli.main(["py-payload", str(project(tmp_path)), "--wheels", str(w), "--python", "3.12", "--arch",
                     "aarch64", "-o", str(out)]) == 0
    ann = json.loads(Path(str(out) + ".json").read_text())
    assert ann == {"dev.flex.profile": "native", "dev.flex.arch": "aarch64", "dev.flex.pyabi": "cp312"}
    with tarfile.open(out) as tf:
        assert {"app.py", "requirements.lock", "wheels/armdep-2.0-cp312-cp312-manylinux_2_17_aarch64.whl"} <= \
            set(tf.getnames())
    key = tmp_path / "release.key"
    crypto.load_or_create(key)
    generic = tmp_path / "out" / "generic.tar.gz"
    generic.write_bytes(nr.targz_tree({"app.py": (b"", 0o644)}))
    assert cli.main(["release", "--oci", str(tmp_path / "oci"), "--key", str(key), "--module", "node", "--version",
                     "1.0.0", str(out), str(generic)]) == 0
    lay = Layout(tmp_path / "oci")
    desc = lay.resolve("flexnodes/node:1.0.0")
    man = json.loads(lay.get_blob(desc["digest"]))
    contracts.validate("oci-profile#ReleaseManifest", man)
    assert [l["annotations"].get("dev.flex.arch") for l in man["layers"]] == ["aarch64", None, None]
    assert lay.referrers(desc["digest"])["manifests"]

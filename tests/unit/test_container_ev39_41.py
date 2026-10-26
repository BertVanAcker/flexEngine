"""Unit tests: container health types (EV-39), container layer selection (EV-40), env / ports / volumes run options
(EV-41) and the strict MCU module layer selection (DA-15). Driver behaviour on fake and real runtimes is in
tests/contract/test_container.py, the engine path in tests/integration/container."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from flexengine2 import contracts
from flexengine2.artefacts.oci import Layout
from flexengine2.artefacts.store import ArtefactStore, VerificationError
from flexengine2.core.reconciler import build_drivers
from flexengine2.drivers.base import DriverError
from flexengine2.drivers.container import (RUN_OPTION_KEYS, exec_probe_ok, norm_arch, resolve_run_options,
                                           run_option_args)
from flexengine2.identity import crypto
from flexengine2.identity.secrets import looks_secret
from flexengine2.testing import native_release as nr


# ---- EV-41: run options -------------------------------------------------------------------------------------------
def test_simple_run_options_unchanged():
    assert run_option_args({"devices": "/dev/dri", "gpus": ["all"]}, {"devices": ["/dev/dri"], "gpus": ["all"]}) == [
        "--device", "/dev/dri", "--gpus", "all"]
    assert RUN_OPTION_KEYS == ["devices", "env", "envFrom", "gpus", "groupAdd", "ports", "runtime", "volumes"]


def test_env_ports_volumes_args(tmp_path):
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "pw").write_text("hunter2\n")
    (sd / "pw").chmod(0o600)
    spec = resolve_run_options(
        {"env": {"APP_MODE": "demo", "LOG_LEVEL": "debug"}, "envFrom": {"DB_PASSWORD": {"file": "pw"}},
         "ports": ["8080:80", "127.0.0.1:9001:9001/udp"], "volumes": ["data:/var/lib/app", "cache:/cache:ro"]},
        {"env": ["APP_*", "LOG_LEVEL", "DB_PASSWORD"], "ports": ["8000-8100", "127.0.0.1:9001/udp"],
         "volumes": ["data", "cache"]}, secrets_dir=str(sd), volume_name=lambda n: f"flex_d_x_{n}")
    assert spec.args == ["-e", "APP_MODE=demo", "-e", "LOG_LEVEL=debug", "-e", "DB_PASSWORD",
                         "-p", "8080:80", "-p", "127.0.0.1:9001:9001/udp",
                         "-v", "flex_d_x_data:/var/lib/app", "-v", "flex_d_x_cache:/cache:ro"]
    assert spec.cli_env == {"DB_PASSWORD": "hunter2"}          # the value goes to the CLI's environment only
    assert spec.volumes == ["flex_d_x_data", "flex_d_x_cache"]
    assert resolve_run_options({"envFrom": {"DB_PASSWORD": {"file": "pw"}}}, {"env": ["*"]}, secrets_dir=str(sd),
                               read_secrets=False).cli_env == {}


@pytest.mark.parametrize("port,allowed,ok", [
    ("8080:80", ["8080"], True), ("8080:80/tcp", ["8080/tcp"], True), ("8080:80/udp", ["8080"], False),
    ("8085:80", ["8080-8090"], True), ("8091:80", ["8080-8090"], False),
    ("127.0.0.1:8080:80", ["8080"], True), ("127.0.0.1:8080:80", ["127.0.0.1:8080"], True),
    ("8080:80", ["127.0.0.1:8080"], False), ("0.0.0.0:8080:80", ["127.0.0.1:8080"], False),
    ("[::1]:8080:80", ["[::1]:8080"], True), ("8080:80", [], False), ("70000:80", ["70000"], False),
])
def test_port_allow_list(port, allowed, ok):
    if ok:
        assert resolve_run_options({"ports": [port]}, {"ports": allowed}).args == ["-p", port]
    else:
        with pytest.raises(DriverError):
            resolve_run_options({"ports": [port]}, {"ports": allowed})


@pytest.mark.parametrize("vol", ["/etc:/x", "./d:/x", "~/d:/x", "d", "d:rel", "D:/x", "a/b:/x", "d:/x:z", "d:/x,y"])
def test_volumes_are_named_only(vol):
    with pytest.raises(DriverError, match="named volumes only"):
        resolve_run_options({"volumes": [vol]}, {"volumes": ["*"]})


def test_secret_heuristic():
    assert looks_secret("API_TOKEN") and looks_secret("db_password") and looks_secret("AWS_SECRET_ACCESS_KEY")
    assert looks_secret("CERT", "-----BEGIN EC PRIVATE KEY-----\nabc") and looks_secret("GH", "ghp_abcdef")
    assert not looks_secret("APP_MODE", "demo") and not looks_secret("LOG_LEVEL", "debug")


def test_exec_probe_check():
    assert exec_probe_ok({"type": "http"}) and exec_probe_ok(None) and exec_probe_ok({"type": "process"})
    assert exec_probe_ok({"type": "exec", "command": ["true"]})
    assert not exec_probe_ok({"type": "exec"}) and not exec_probe_ok({"type": "exec", "command": "true"})
    assert not exec_probe_ok({"type": "exec", "command": []}) and not exec_probe_ok({"type": "exec", "command": [""]})


def test_norm_arch():
    assert [norm_arch(a) for a in ("amd64", "arm64", "arm", "386", "x86_64", "riscv64")] == [
        "x86_64", "aarch64", "armv7l", "i686", "x86_64", "riscv64"]


def test_build_drivers_passes_options(tmp_path):
    drv = build_drivers({"deviceId": "d1", "drivers": {"container": {
        "runtime": "podman", "allowedRunOptions": {"volumes": ["data"]}, "secretsDir": "/etc/flex/secrets"}},
        "native": False}, tmp_path)["container"]
    assert drv.rt == "podman" and drv.allowed_run_options == {"volumes": ["data"]}
    assert drv.secrets_dir == "/etc/flex/secrets" and drv.vname("dep", "data") == "flex_d1_dep_data"


# ---- contracts: descriptor fields, reason codes ---------------------------------------------------------------------
def _desc(**kw) -> dict:
    d = nr.native_descriptor("m", "1.0.0")
    d["profiles"] = ["container"]
    d.update(kw)
    return d


def test_descriptor_schema_accepts_the_new_fields():
    contracts.validate("descriptor", _desc(
        health={"type": "exec", "command": ["sh", "-c", "pgrep node"], "execTimeoutSeconds": 1.5},
        runOptions={"env": {"APP_MODE": "x"}, "envFrom": {"DB_PASSWORD": {"file": "db-password"}},
                    "ports": ["8080:80", "127.0.0.1:53:53/udp"], "volumes": ["data:/data", "cache:/c:ro"]}))
    contracts.validate("descriptor", _desc(health={"type": "process"}))


@pytest.mark.parametrize("bad", [
    {"health": {"type": "exec", "command": []}},
    {"runOptions": {"volumes": ["/etc:/x"]}},
    {"runOptions": {"ports": ["80"]}},
    {"runOptions": {"env": {"FLEX_X": "1"}}},
    {"runOptions": {"env": {"A": 1}}},
    {"runOptions": {"envFrom": {"A": {"file": "../x"}}}},
    {"runOptions": {"privileged": True}},
])
def test_descriptor_schema_refuses(bad):
    with pytest.raises(Exception):
        contracts.validate("descriptor", _desc(**bad))


def test_reason_texts_are_registered():
    for text in ("container image is built for aarch64 (dev.flex.arch), this device is x86_64",
                 "health type exec needs a non-empty command list",
                 "runOptions.env.API_TOKEN looks like a secret: use runOptions.envFrom with a device-local secret file",
                 "runOptions.env.APP_MODE is not in this device's allow-list",
                 "runOptions.envFrom.DB_PASSWORD: this device has no drivers.container.secretsDir",
                 "runOptions.envFrom.DB_PASSWORD: secret file 'x' is not usable (secret /s/x: mode 0644 is group/other "
                 "accessible (expected 0600; set fixPermissions to repair))",
                 "runOptions.ports='8080:80' is not in this device's allow-list",
                 "runOptions.volumes='/etc:/x': expected name:/absolute/target[:ro] (named volumes only, never a host "
                 "path)",
                 "runOptions.devices='/dev/mem' is not in this device's allow-list",
                 "mcu-module layer without dev.flex.floatAbi cannot be selected (DA-15: mcu-module layers must name "
                 "dev.flex.arch, dev.flex.floatAbi)"):
        assert contracts.reason_code(text, "detail"), text
    assert contracts.reason_code("runOptions.ports='8080:80' is not in this device's allow-list",
                                 "detail")["code"] == "FE-CTR-PRE-012"
    assert contracts.reason_code("runOptions.devices='/dev/mem' is not in this device's allow-list",
                                 "detail")["code"] == "FE-CTR-004"


# ---- EV-40 / DA-15: store layer selection ---------------------------------------------------------------------------
@pytest.fixture
def pub(tmp_path):
    key = crypto.generate()
    lay = Layout(tmp_path / "oci")
    store = ArtefactStore(tmp_path / "eng", None, [crypto.public_b64(key)])
    n = [0]

    def publish(layers: list[tuple[bytes, dict]]) -> dict:
        n[0] += 1
        info = nr.publish_native_release(lay, key, "multi", f"1.0.{n[0]}", [
            (nr.targz_tree({"app.py": (d, 0o644)}), a) for d, a in layers])
        for blob in (tmp_path / "oci" / "blobs" / "sha256").iterdir():
            store.put("sha256:" + blob.name, blob.read_bytes())
        idx = store.root.parent / "referrers" / info["digest"].split(":")[1]
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(json.dumps(lay.referrers(info["digest"])))
        return info

    def pick(info, profile, platform):
        rel = store.verify_release(info["repository"], info["digest"], profile, platform=platform)
        with tarfile.open(rel.payload_path) as tf:
            return tf.extractfile("app.py").read()
    return publish, pick


def test_container_layer_per_cpu(pub):
    publish, pick = pub
    c = {"dev.flex.profile": "container", "dev.flex.imageRef": "docker.io/x@sha256:" + "0" * 64}
    info = publish([(b"arm", {**c, "dev.flex.arch": "aarch64"}), (b"x86", {**c, "dev.flex.arch": "x86_64"})])
    assert pick(info, "container", {"dev.flex.arch": "x86_64"}) == b"x86"
    assert pick(info, "container", {"dev.flex.arch": "aarch64"}) == b"arm"
    with pytest.raises(VerificationError, match="release has no 'container' payload for platform"):
        pick(info, "container", {"dev.flex.arch": "riscv64"})
    # permissive (DA-15 keeps it for containers): an unannotated single image fits any CPU
    assert pick(publish([(b"any", c)]), "container", {"dev.flex.arch": "riscv64"}) == b"any"


def test_mcu_module_layers_are_strict(pub):
    publish, pick = pub
    m = {"dev.flex.profile": "mcu-module"}
    v7 = {"dev.flex.platform": "x", "dev.flex.arch": "armv7m", "dev.flex.floatAbi": "soft"}
    plat = {"dev.flex.arch": "armv7m", "dev.flex.floatAbi": "soft"}
    assert pick(publish([(b"v7", {**m, **v7})]), "mcu-module", plat) == b"v7"
    with pytest.raises(VerificationError) as e:
        pick(publish([(b"noabi", {**m, "dev.flex.arch": "armv7m"})]), "mcu-module", plat)
    assert str(e.value) == ("mcu-module layer without dev.flex.floatAbi cannot be selected "
                            "(DA-15: mcu-module layers must name dev.flex.arch, dev.flex.floatAbi)")
    assert contracts.reason_code(str(e.value), "detail")["code"] == "FE-ART-REL-004"
    with pytest.raises(VerificationError, match="without dev.flex.arch, dev.flex.floatAbi cannot be selected"):
        pick(publish([(b"bare", m)]), "mcu-module", plat)
    # an unannotated layer next to a complete one is never picked (it is not selectable), the complete one is
    assert pick(publish([(b"bare", m), (b"v7", {**m, **v7})]), "mcu-module", plat) == b"v7"
    with pytest.raises(VerificationError, match="release has no 'mcu-module' payload for platform"):
        pick(publish([(b"bare", m), (b"v8", {**m, "dev.flex.arch": "armv8m.main", "dev.flex.floatAbi": "hard"}),
                      (b"v7h", {**m, "dev.flex.arch": "armv7m", "dev.flex.floatAbi": "hard"})]), "mcu-module", plat)
    # native keeps the permissive match
    assert pick(publish([(b"n", {"dev.flex.profile": "native"})]), "native", {"dev.flex.arch": "x86_64"}) == b"n"


def test_process_probe_sees_restarts(monkeypatch):
    """`process`: running and not restarting, and no restart since the previous probe (EV-39)."""
    from flexengine2.drivers.container import CliContainerDriver

    d = CliContainerDriver("d1", "docker")
    state = {"Id": "c1", "State": {"Running": True, "Restarting": False}, "RestartCount": 0}
    monkeypatch.setattr(d, "_inspect", lambda ref: json.loads(json.dumps(state)))
    p = {"type": "process"}
    assert d.health("x", p) and d.health("x", p)
    state["RestartCount"] = 1
    assert d.health("x", p) is False          # restarted since the last probe
    assert d.health("x", p) is True           # stable again
    state["State"]["Restarting"] = True
    assert d.health("x", p) is False
    state["State"] = {"Running": False}
    assert d.health("x", p) is False
    monkeypatch.setattr(d, "_inspect", lambda ref: (_ for _ in ()).throw(DriverError("down")))
    assert d.health("x", p) is False


# ---- image resolution on graph-driver stores (classic Docker, Podman) ------------------------------------------------
def _docker_save_tar(path: Path, config_hex: str) -> Path:
    import io

    docs = {"index.json": {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + "d" * 64}]},
            "manifest.json": [{"Config": f"blobs/sha256/{config_hex}", "RepoTags": ["flexnode/x:1"], "Layers": []}]}
    with tarfile.open(path, "w") as tf:
        for name, doc in docs.items():
            data = json.dumps(doc).encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


def test_archive_image_ids(tmp_path):
    from flexengine2.drivers.container import archive_image_ids

    assert archive_image_ids(_docker_save_tar(tmp_path / "a.tar", "c" * 64)) == ["sha256:" + "c" * 64]
    (tmp_path / "fake.json").write_text("{}")
    assert archive_image_ids(tmp_path / "fake.json") == [] and archive_image_ids(tmp_path / "missing") == []


@pytest.mark.parametrize("store", ["containerd", "graph"])
def test_loaded_image_runs_by_whatever_content_reference_the_store_resolves(tmp_path, monkeypatch, store):
    """A containerd image store (Docker >= 29) resolves the release's manifest-digest reference after `docker load`;
    a graph-driver store (Docker < 29, Podman) knows the loaded image only by its ID (config digest). Both are the
    verified content; the driver must run either way (it used to fail with 'payload did not contain image')."""
    import subprocess

    from flexengine2.drivers import container as ctr
    from flexengine2.drivers.base import Workload

    tar = _docker_save_tar(tmp_path / "a.tar", "c" * 64)
    ref = "docker.io/flexnode/x@sha256:" + "d" * 64
    known: set[str] = set()
    loads = [ref if store == "containerd" else "sha256:" + "c" * 64]   # what `load` makes resolvable
    calls = []

    def run(cmd, timeout=120, check=True, env=None):
        calls.append(cmd[1:])
        rc = 0
        if cmd[1:3] == ["image", "inspect"]:
            rc = 0 if cmd[3] in known else 1
        elif cmd[1] == "load" and loads:
            known.add(loads.pop(0))
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(ctr, "_run", run)
    d = ctr.CliContainerDriver("dev", runtime="podman")
    wl = Workload(dep_id="d", module="x", version="1", digest="sha256:" + "1" * 64, payload_path=str(tar),
                  payload_annotations={"dev.flex.imageRef": ref})
    want = ref if store == "containerd" else "sha256:" + "c" * 64
    assert d.ensure_image(wl) == want
    assert d.ensure_image(wl) == want and [c for c in calls if c[0] == "load"] == [["load", "-q", "-i", str(tar)]]
    known.clear()
    loads.append("flexnode/x:1")                                   # resolvable by tag only: not pinned, refused
    with pytest.raises(DriverError, match="payload did not contain image"):
        d.ensure_image(wl)

"""FE-4 helpers without an executor: the compose driver's host-access policy and project names, the on-device build
step of the native profile (drivers/buildstep.py), and the store's choice of payload / source layers."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from flexengine2.drivers.base import BuildRequest, BuildTimeout, DriverError, Workload, tree_key
from flexengine2.drivers.buildstep import native_build, pack_tree
from flexengine2.drivers.compose import ComposeDriver, compose_policy, project_name


@pytest.mark.parametrize("svc,why", [
    ({"image": "x"}, None),
    ({"image": "x", "volumes": ["data:/data"]}, None),
    ({"image": "x", "privileged": True}, "asks for privileged"),
    ({"image": "x", "cap_add": ["NET_ADMIN"]}, "asks for cap_add"),
    ({"image": "x", "devices": ["/dev/mem"]}, "asks for devices"),
    ({"image": "x", "network_mode": "host"}, "asks for network_mode: host"),
    ({"image": "x", "pid": "host"}, "asks for pid: host"),
    ({"image": "x", "volumes": ["/etc:/host-etc"]}, "mounts a host path"),
    ({"image": "x", "volumes": ["./cfg:/cfg"]}, "mounts a host path"),
    ({"image": "x", "volumes": [{"type": "bind", "source": "data", "target": "/d"}]}, "mounts a host path"),
])
def test_compose_policy_denies_host_access_by_default(svc, why):
    got = compose_policy({"services": {"app": svc}})
    assert (got is None) if why is None else (why in got), got
    assert compose_policy({"services": {"app": svc}}, allow_host_access=True) is None


def test_compose_policy_needs_services():
    assert compose_policy({}) == "compose file has no services"
    assert compose_policy({"services": {"a": "x"}}) == "compose service a is not a mapping"


def test_project_names_are_valid_and_distinct():
    assert project_name("panda", "pg") == "flex_panda_pg"
    a, b = project_name("Panda.1", "My.App"), project_name("panda-1", "my-app")
    assert a != b and a.startswith("flex_panda-1_my-app-") and all(c.islower() or c in "_-" or c.isdigit() for c in a)


def _targz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n, d in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(d)
            tf.addfile(ti, io.BytesIO(d))
    return buf.getvalue()


def test_compose_preflight_reads_the_payload(tmp_path):
    drv = ComposeDriver("d1", tmp_path)
    ok = tmp_path / "ok.tar.gz"
    ok.write_bytes(_targz({"compose.yaml": b"services:\n  a:\n    image: x\n"}))
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(_targz({"compose.yaml": b"services:\n  a:\n    image: x\n    privileged: true\n"}))
    none = tmp_path / "none.tar.gz"
    none.write_bytes(_targz({"readme": b"x"}))

    def rel(p, desc=None):
        return type("R", (), {"payload_path": str(p), "payload_annotations": {}, "descriptor": desc or {}})()
    assert drv.preflight(rel(ok), {}) is None
    assert "privileged" in drv.preflight(rel(bad), {})
    assert drv.preflight(rel(none), {}).startswith("compose payload has no compose file")
    assert drv.preflight(rel(ok, {"runOptions": {"gpus": "all"}}), {}).startswith("runOptions are not supported")
    assert drv.preflight(rel(tmp_path / "missing"), {}).startswith("compose payload unusable")


def test_native_build_runs_the_command_and_packs_the_output(tmp_path):
    src = tmp_path / "src.tar.gz"
    src.write_bytes(_targz({"build.sh": b"echo one\necho flex-progress 0.5\nmkdir -p out\necho hi > out/app.py\n"}))
    seen = []
    req = BuildRequest(id="b1", source_path=str(src), spec={"command": ["sh", "build.sh"], "output": "out"},
                       work_dir=str(tmp_path / "w"), emit=lambda k, v: seen.append((k, v)), timeout=30)
    res = native_build(req)
    assert ("line", "one") in seen and ("progress", 0.5) in seen
    with tarfile.open(res.payload_path) as tf:
        assert tf.getnames() == ["app.py"]
    again = native_build(req)
    assert again.digest == res.digest, "a deterministic payload: same output, same result digest"


@pytest.mark.parametrize("spec,err", [
    ({}, "the release descriptor names no build command (build.command)"),
    ({"command": ["sh", "-c", "exit 4"]}, "build command exited with status 4"),
    ({"command": ["/no/such/tool"]}, "build command not found: /no/such/tool"),
    ({"command": ["true"], "output": "nothere"}, "build output nothere is not a directory of the source tree"),
])
def test_native_build_failures(tmp_path, spec, err):
    src = tmp_path / "src.tar.gz"
    src.write_bytes(_targz({"x": b"y"}))
    req = BuildRequest(id="b1", source_path=str(src), spec=spec, work_dir=str(tmp_path / "w"), timeout=30)
    with pytest.raises(DriverError) as e:
        native_build(req)
    assert str(e.value).startswith(err)


def test_native_build_timeout_kills_the_build(tmp_path):
    src = tmp_path / "src.tar.gz"
    src.write_bytes(_targz({"x": b"y"}))
    req = BuildRequest(id="b1", source_path=str(src), spec={"command": ["sleep", "30"]}, work_dir=str(tmp_path / "w"),
                       timeout=0.5)
    with pytest.raises(BuildTimeout) as e:
        native_build(req)
    assert str(e.value) == "build timed out after 0.5 s"


def test_an_unusable_source_payload(tmp_path):
    p = tmp_path / "src.tar.gz"
    p.write_bytes(b"not a tar")
    req = BuildRequest(id="b1", source_path=str(p), spec={"command": ["true"]}, work_dir=str(tmp_path / "w"))
    with pytest.raises(DriverError, match="source payload unusable"):
        native_build(req)


def test_tree_key_tells_a_rebuild_apart():
    wl = Workload("d", "m", "1", "sha256:" + "a" * 64, "", {})
    assert tree_key(wl) == "a" * 16
    wl.build = {"id": "b1", "resultDigest": "sha256:" + "b" * 64}
    assert tree_key(wl) == "a" * 16 + "-" + "b" * 12


def test_pack_tree_is_deterministic(tmp_path):
    (tmp_path / "t" / "d").mkdir(parents=True)
    (tmp_path / "t" / "d" / "f").write_text("x")
    a = pack_tree(tmp_path / "t", tmp_path / "a.tgz")
    os.utime(tmp_path / "t" / "d" / "f", (1, 1))
    assert pack_tree(tmp_path / "t", tmp_path / "b.tgz") == a


def test_the_store_keeps_source_and_compose_layers_apart(tmp_path):
    from flexengine2.artefacts.oci import MT_PAYLOAD_COMPOSE, MT_PAYLOAD_NATIVE, MT_PAYLOAD_SOURCE, Layout
    from flexengine2.artefacts.store import ArtefactStore, VerificationError
    from flexengine2.identity import crypto
    from flexengine2.testing.release import publish_release, sbom_stub, sign_release

    key = crypto.generate()
    lay = Layout(tmp_path / "oci")
    desc = {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": "m", "releaseVersion": "1",
            "profiles": ["native"], "safetyClass": "none"}

    def release(layers):
        m = publish_release(lay, "flexnodes/m", desc, layers, sbom_stub("m", "1", "native"))
        sign_release(lay, key, "flexnodes/m", m, "1")
        return m["digest"]
    src_only = release([(MT_PAYLOAD_SOURCE, b"src", {"dev.flex.profile": "native"})])
    both = release([(MT_PAYLOAD_SOURCE, b"src2", {"dev.flex.profile": "native"}),
                    (MT_PAYLOAD_NATIVE, b"bin", {"dev.flex.profile": "native"}),
                    (MT_PAYLOAD_COMPOSE, b"cmp", {"dev.flex.profile": "native"})])

    class Local:
        def request(self, method, path, headers=None):
            kind, digest = path.split("/")[-2:]
            if kind == "referrers":
                import json
                return 200, {}, json.dumps(lay.referrers(digest)).encode()
            return 200, {}, lay.get_blob(digest)
    store = ArtefactStore(tmp_path / "eng", Local(), [crypto.public_b64(key)])
    with pytest.raises(VerificationError, match="no payload"):
        store.verify_release("flexnodes/m", src_only, "native")
    r = store.verify_release("flexnodes/m", src_only, "native", source=True)
    assert r.payload_path == "" and Path(r.source_path).read_bytes() == b"src"
    r = store.verify_release("flexnodes/m", both, "native")
    assert Path(r.payload_path).read_bytes() == b"bin" and r.source_path is None
    r = store.verify_release("flexnodes/m", both, "native", media_types=(MT_PAYLOAD_COMPOSE,))
    assert Path(r.payload_path).read_bytes() == b"cmp"

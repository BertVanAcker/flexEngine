"""Container-driver contract cases beyond the SPI core: health types `exec` / `process` (EV-39), multi-CPU image
layers (EV-40) and env / envFrom / ports / named volumes from the signed descriptor (EV-41).

Same harnesses as test_spi.py (fixture `ctr_harness`: fake docker/podman/ank on push, real docker/podman nightly).
"""

from __future__ import annotations

import dataclasses
import json
import os
import time

import pytest

from flexengine2.drivers.base import DriverError, http_probe, wait_until

from spi_harness.base import deployment, free_port
from test_spi import gate, install, running

FAST = {"periodSeconds": 0.15, "successThreshold": 2, "timeoutSeconds": 20}


def _with(wl, **kw):
    return dataclasses.replace(wl, **kw)


# ---- EV-39: health types ------------------------------------------------------------------------------------------
def test_health_exec(ctr_harness):
    h = ctr_harness
    d = h.new()
    ok = {"type": "exec", "command": h.exec_command(True), **FAST}
    wl = _with(h.workload("1.0.0"), health=ok)
    assert d.preflight(h.release(wl), deployment(wl.dep_id, wl.module, wl.version, "container")) is None
    install(h, d, wl)
    assert d.health(wl.dep_id, ok) is True
    assert d.health(wl.dep_id, {"type": "exec", "command": h.exec_command(False)}) is False
    t0 = time.monotonic()
    assert d.health(wl.dep_id, {"type": "exec", "command": ["sleep", "5"], "execTimeoutSeconds": 0.5}) is False
    assert time.monotonic() - t0 < 4, "exec probe not bounded by execTimeoutSeconds"
    assert d.health(wl.dep_id, {"type": "exec", "command": []}) is False
    assert d.health("never-deployed", ok) is False


def test_health_exec_gate_fails(ctr_harness):
    """A failing exec command fails the reconciler's gate (timeout), while the container keeps running."""
    h = ctr_harness
    d = h.new()
    wl = h.workload("1.0.0")
    d.apply(None, wl)
    running(d, wl)
    ok, _, why = gate(d, wl.dep_id, {"type": "exec", "command": h.exec_command(False), "periodSeconds": 0.15,
                                     "successThreshold": 1, "timeoutSeconds": 1.5})
    assert not ok and "timeout" in (why or ""), why


def test_health_exec_without_command_refused(ctr_harness):
    h = ctr_harness
    d = h.new()
    wl = h.workload("1.0.0")
    rel = h.release(wl)
    rel.descriptor["health"] = {"type": "exec"}
    assert d.preflight(rel, deployment(wl.dep_id, wl.module, wl.version, "container")) == \
        "health type exec needs a non-empty command list"


def test_health_process(ctr_harness):
    h = ctr_harness
    d = h.new()
    proc = {"type": "process", **FAST}
    wl = _with(h.workload("1.0.0"), health=proc)
    install(h, d, wl)
    assert d.health(wl.dep_id, proc) is True
    assert d.health("never-deployed", proc) is False
    bad = h.bad_workload()
    d.apply(wl, _with(bad, health=proc))
    # the bad container exits for good (failed() is not mapped for Ankaios: watch the observed state instead)
    assert wait_until(lambda: not d.status(bad.dep_id).running, 30, 0.1), "bad workload never exited"
    assert d.health(bad.dep_id, proc) is False, "process probe healthy for an exited container"


def test_health_unreachable(ctr_harness):
    h = ctr_harness
    with h.unreachable() as d:
        for probe in ({"type": "exec", "command": ["true"]}, {"type": "process"}):
            assert d.health(h.dep_id, probe) is False


# ---- EV-40: platform ----------------------------------------------------------------------------------------------
def test_platform_and_other_cpu_refused(ctr_harness):
    h = ctr_harness
    d = h.new()
    p = d.platform()
    assert set(p) == {"dev.flex.arch"} and p["dev.flex.arch"] == h.arch, p
    caps = d.capabilities()
    assert caps.get("platform") == p and "exec" in caps.get("healthTypes", []), caps
    wl = h.workload("1.0.0")
    other = "aarch64" if p["dev.flex.arch"] != "aarch64" else "x86_64"
    rel = h.release(_with(wl, payload_annotations={**wl.payload_annotations, "dev.flex.arch": other}))
    t0 = h.transfers()
    reason = d.preflight(rel, deployment(wl.dep_id, wl.module, wl.version, "container"))
    assert reason == f"container image is built for {other} (dev.flex.arch), this device is {p['dev.flex.arch']}"
    assert h.transfers() == t0


def test_platform_unreachable_defers(ctr_harness):
    h = ctr_harness
    wl = h.workload("1.0.0")
    with h.unreachable() as d:
        with pytest.raises(DriverError):
            d.platform()
        with pytest.raises(DriverError):   # preflight of an arch-annotated layer is deferred, never a refusal
            d.preflight(h.release(wl), deployment(wl.dep_id, wl.module, wl.version, "container"))


# ---- EV-41: env / envFrom / ports / volumes ---------------------------------------------------------------------
def _secret(h, value="s3cret-value") -> str:
    sd = h.tmp / "secrets"
    sd.mkdir(mode=0o700, exist_ok=True)
    f = sd / "db-password"
    f.write_text(value + "\n")
    f.chmod(0o600)
    return str(sd)


def _options_driver(h, allowed, secrets=True):
    d = h.option_driver(allowed, _secret(h) if secrets else None)
    if d is None:
        pytest.skip(f"{h.id}: runOptions are refused by this driver (covered by the refusal case)")
    h.drivers.append(d)
    return d


def test_run_options_env_ports_volumes(ctr_harness):
    h = ctr_harness
    port = free_port()
    allowed = {"env": ["APP_*", "DB_PASSWORD"], "ports": [f"127.0.0.1:{port}"], "volumes": ["data"]}
    d = _options_driver(h, allowed)
    opts = {"env": {"APP_MODE": "demo"}, "envFrom": {"DB_PASSWORD": {"file": "db-password"}},
            "ports": [f"127.0.0.1:{port}:8080/tcp"], "volumes": ["data:/data"]}
    wl1 = _with(h.workload("1.0.0"), run_options=opts)
    rel = h.release(wl1)
    rel.descriptor["runOptions"] = opts
    assert d.preflight(rel, deployment(wl1.dep_id, wl1.module, wl1.version, "container")) is None
    install(h, d, wl1)
    vol = f"flex_{h.device}_{wl1.dep_id}_data"
    assert h.volumes() == [vol]
    p = h.exec(wl1.dep_id, "sh", "-c", 'test "$APP_MODE" = demo && test "$DB_PASSWORD" = s3cret-value')
    assert p.returncode == 0, (p.stdout, p.stderr)
    # the secret is never on a CLI argv (it reaches `run` through the CLI's environment)
    assert not any("s3cret-value" in a for c in h.calls for a in c), "secret value on a runtime CLI argv"
    if h.real_fs:
        assert wait_until(lambda: http_probe("127.0.0.1", port, "/healthz"), 15, 0.2), "published port not reachable"
        assert h.exec(wl1.dep_id, "sh", "-c", "echo kept > /data/f").returncode == 0
    else:
        info = json.loads(h._rt("container", "inspect", f"flex_{h.device}_{wl1.dep_id}").stdout)[0]
        assert info["HostConfig"]["PortBindings"] == [f"127.0.0.1:{port}:8080/tcp"]
        assert [m["Name"] for m in info["Mounts"]] == [vol]
    # update: the volume survives (only the container is replaced)
    wl2 = _with(h.workload("2.0.0"), run_options=opts)
    d.apply(wl1, wl2)
    running(d, wl2)
    assert h.volumes() == [vol]
    if h.real_fs:
        p = h.exec(wl2.dep_id, "cat", "/data/f")
        assert p.stdout.strip() == "kept", (p.stdout, p.stderr)
    # rollback keeps it as well
    d.apply(wl2, wl1)
    running(d, wl1)
    assert h.volumes() == [vol]
    # removing the deployment deletes its volumes
    d.remove(wl1.dep_id)
    assert h.volumes() == []
    d.remove(wl1.dep_id)


@pytest.mark.parametrize("opts,allowed,why", [
    ({"env": {"APP_MODE": "x"}}, {}, "runOptions.env.APP_MODE is not in this device's allow-list"),
    ({"env": {"API_TOKEN": "abc"}}, {"env": ["*"]},
     "runOptions.env.API_TOKEN looks like a secret: use runOptions.envFrom with a device-local secret file"),
    ({"env": {"CERT": "-----BEGIN PRIVATE KEY-----\nMII"}}, {"env": ["*"]},
     "runOptions.env.CERT looks like a secret: use runOptions.envFrom with a device-local secret file"),
    ({"env": {"FLEX_PARAM_X": "1"}}, {"env": ["*"]},
     "runOptions.env.FLEX_PARAM_X: invalid variable name (FLEX_* is the engine's)"),
    ({"envFrom": {"DB_PASSWORD": {"file": "missing"}}}, {"env": ["*"]}, None),
    ({"envFrom": {"DB_PASSWORD": {"file": "../etc/shadow"}}}, {"env": ["*"]},
     "runOptions.envFrom.DB_PASSWORD: expected an object naming a secret file (file: <name>)"),
    ({"ports": ["8080:80"]}, {"ports": ["9090"]}, "runOptions.ports='8080:80' is not in this device's allow-list"),
    ({"ports": ["8080:80/udp"]}, {"ports": ["8080"]}, "runOptions.ports='8080:80/udp' is not in this device's allow-list"),
    ({"ports": ["8080:80"]}, {"ports": ["127.0.0.1:8080"]}, "runOptions.ports='8080:80' is not in this device's allow-list"),
    ({"ports": ["80"]}, {"ports": ["80"]}, "runOptions.ports='80': expected [ip:]hostPort:containerPort[/tcp|udp|sctp]"),
    ({"volumes": ["/etc:/host-etc"]}, {"volumes": ["*"]},
     "runOptions.volumes='/etc:/host-etc': expected name:/absolute/target[:ro] (named volumes only, never a host path)"),
    ({"volumes": ["./data:/data"]}, {"volumes": ["*"]},
     "runOptions.volumes='./data:/data': expected name:/absolute/target[:ro] (named volumes only, never a host path)"),
    ({"volumes": ["data:/data"]}, {}, "runOptions.volumes='data:/data' is not in this device's allow-list"),
    ({"privileged": True}, {}, None),
], ids=["env-not-allowed", "env-secret-name", "env-secret-value", "env-flex-reserved", "envfrom-missing-file",
        "envfrom-path", "port-not-allowed", "port-proto", "port-address", "port-malformed", "volume-bind-abs",
        "volume-bind-rel", "volume-not-allowed", "unknown-key"])
def test_run_options_refused(ctr_harness, opts, allowed, why):
    """Deny by default, refused at preflight (previous version keeps running), and apply re-checks (backstop)."""
    h = ctr_harness
    d = _options_driver(h, allowed)
    wl = _with(h.workload("1.0.0"), run_options=opts)
    rel = h.release(wl)
    rel.descriptor["runOptions"] = opts
    t0 = h.transfers()
    reason = d.preflight(rel, deployment(wl.dep_id, wl.module, wl.version, "container"))
    assert reason, "preflight accepted a run option the device does not allow"
    if why:
        assert reason == why
    assert h.transfers() == t0
    with pytest.raises(DriverError):
        d.apply(None, wl)
    assert d.status(wl.dep_id).present is False and h.volumes() == []


def test_envfrom_insecure_secret_file_refused(ctr_harness):
    h = ctr_harness
    d = _options_driver(h, {"env": ["DB_PASSWORD"]})
    os.chmod(h.tmp / "secrets" / "db-password", 0o644)
    wl = _with(h.workload("1.0.0"), run_options={"envFrom": {"DB_PASSWORD": {"file": "db-password"}}})
    rel = h.release(wl)
    rel.descriptor["runOptions"] = wl.run_options
    reason = d.preflight(rel, deployment(wl.dep_id, wl.module, wl.version, "container"))
    assert reason and reason.startswith("runOptions.envFrom.DB_PASSWORD: secret file 'db-password' is not usable"), reason
    assert "0644" in reason


def test_envfrom_without_secrets_dir_refused(ctr_harness):
    h = ctr_harness
    d = _options_driver(h, {"env": ["DB_PASSWORD"]}, secrets=False)
    rel = h.release(h.workload("1.0.0"))
    rel.descriptor["runOptions"] = {"envFrom": {"DB_PASSWORD": {"file": "db-password"}}}
    assert d.preflight(rel, deployment(h.dep_id, "ct_app", "1.0.0", "container")) == \
        "runOptions.envFrom.DB_PASSWORD: this device has no drivers.container.secretsDir"

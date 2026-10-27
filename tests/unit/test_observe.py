"""UM-4: device resources (obs/resources.py), the workload observer (obs/workloads.py: per-deployment resources and
workload stdout/stderr), their place in Engine.metrics() and GET /v1/metrics / GET /v1/logs (source workload)."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from flexengine2 import contracts
from flexengine2.drivers.base import supports
from flexengine2.drivers.container import _stat_of, sum_stats
from flexengine2.local.api import EngineProvider, LocalApi, OfflineProvider
from flexengine2.local.cli import format_log
from flexengine2.obs import workloads
from flexengine2.obs.resources import HostSampler, ProcCpu, parse_percent, parse_size, proc_rss, rfc3339_ns
from flexengine2.obs.workloads import WorkloadObserver, clean_resources
from flexengine2.testing.observe import ObservableFakeDriver, fake_host, sampler_for

from _local_fixtures import DEVICE, installed_engine, write_config


@pytest.fixture
def lab(tmp_path):
    cfg, _ = write_config(tmp_path, socket=False)
    drv = ObservableFakeDriver()
    eng = installed_engine(cfg, drv)
    deps = sorted(eng.state["applied"])
    assert deps, "the vector bundle installs deployments"
    return cfg, eng, drv, deps


def api_for(eng, cfg=None):
    return LocalApi(EngineProvider([eng], cfg), socket_path=None, log=lambda m: None)


# ---- device resources ---------------------------------------------------------------------------------------------
def test_host_sampler_reads_cpu_memory_storage_temperature(tmp_path):
    root = fake_host(tmp_path / "host", cpu=(100, 1000), memory=(8 << 30, 6 << 30), temperature=48.5)
    s = sampler_for(root, tmp_path)
    fake_host(tmp_path / "host", cpu=(400, 2000), memory=(8 << 30, 6 << 30), temperature=48.5)
    r = s.sample()
    contracts.validate("telemetry#DeviceResources", r)
    assert r["cpu"] == {"count": 4, "load1": 0.5, "load5": 0.4, "load15": 0.3, "percent": 30.0}   # 300 of 1000
    assert r["memory"] == {"totalBytes": 8 << 30, "usedBytes": 2 << 30, "availableBytes": 6 << 30, "percent": 25.0}
    st = os.statvfs(tmp_path)
    assert r["storage"]["path"] == str(tmp_path) and r["storage"]["totalBytes"] == st.f_blocks * st.f_frsize
    assert 0 <= r["storage"]["percent"] <= 100
    assert r["temperatureCelsius"] == 48.5


def test_host_sampler_rate_limit_and_missing_parts(tmp_path):
    now = [1000.0]
    root = fake_host(tmp_path / "host", temperature=None)
    s = HostSampler(tmp_path, proc=str(root / "proc"), sys_root=str(root / "sys"), clock=lambda: now[0])
    a = s.sample()
    assert "temperatureCelsius" not in a and "percent" not in a["cpu"]      # no time passed in jiffies
    now[0] += 0.5
    assert s.sample() is a                                                  # within min_interval: same sample
    now[0] += 1.0
    assert s.sample() is not a
    empty = HostSampler(tmp_path / "gone", proc=str(tmp_path / "noproc"), sys_root=str(tmp_path / "nosys"))
    e = empty.sample()                                                      # no procfs: only what os tells
    assert e is None or set(e) <= {"sampledAt", "cpu"} and set(e.get("cpu", {})) <= {"count"}
    blank = HostSampler(tmp_path, proc=str(tmp_path / "noproc"), sys_root=str(tmp_path / "nosys"))
    blank._cpu_count, blank._storage = lambda: None, lambda: None
    assert blank.sample() is None                                           # nothing known: no key at all


def test_host_sampler_prefers_cpu_zone_and_ignores_garbage(tmp_path):
    root = fake_host(tmp_path / "host", temperature=40.0)
    th = root / "sys" / "class" / "thermal"
    for name, typ, temp in (("thermal_zone1", "acpitz", "90000"), ("thermal_zone2", "x86_pkg_temp", "55000"),
                            ("thermal_zone3", "cpu-broken", "-273000"), ("thermal_zone4", "soc", "junk")):
        (th / name).mkdir()
        (th / name / "type").write_text(typ)
        (th / name / "temp").write_text(temp)
    assert sampler_for(root, tmp_path).sample()["temperatureCelsius"] == 55.0


def test_parsers():
    assert parse_size("1.5MiB") == 1572864 and parse_size("12kB") == 12000 and parse_size("0B") == 0
    assert parse_size("7.6GiB") == int(7.6 * 1024 ** 3) and parse_size("--") is None and parse_size("") is None
    assert parse_percent("12.50%") == 12.5 and parse_percent("--") is None
    assert rfc3339_ns("2026-10-27T10:00:00.5Z") == (1793095200, 500000000)
    assert rfc3339_ns("2026-10-27T12:00:00+02:00") == (1793095200, 0)
    assert rfc3339_ns("yesterday") is None
    assert rfc3339_ns("2026-10-27T10:00:00.000000001Z") > rfc3339_ns("2026-10-27T10:00:00Z")


def test_proc_cpu_and_rss_of_this_process():
    pc = ProcCpu()
    if pc.cpu_seconds(os.getpid()) is None:
        pytest.skip("no procfs")
    assert pc.percent("me", pc.cpu_seconds(os.getpid())) is None            # first sample
    end = time.time() + 0.2
    while time.time() < end:
        pass
    assert pc.percent("me", pc.cpu_seconds(os.getpid())) > 10
    assert proc_rss(os.getpid()) > 1 << 20
    assert proc_rss(2 ** 22 + 12345) is None
    pc.forget(set())
    assert pc.percent("me", None) is None


def test_container_stats_rows_and_sum():
    a = _stat_of({"ID": "abc", "CPUPerc": "12.50%", "MemUsage": "100MiB / 1GiB", "MemPerc": "9.77%", "PIDs": "3"})
    assert a == {"cpuPercent": 12.5, "memoryBytes": 100 << 20, "memoryLimitBytes": 1 << 30, "memoryPercent": 9.77,
                 "pids": 3}
    b = _stat_of({"cpu_percent": "--", "mem_usage": "28MiB / 2GiB", "pids": "x"})       # podman json names
    assert b == {"memoryBytes": 28 << 20, "memoryLimitBytes": 2 << 30}
    s = sum_stats([a, b])
    assert s == {"cpuPercent": 12.5, "memoryBytes": 128 << 20, "pids": 3, "memoryLimitBytes": 2 << 30,
                 "memoryPercent": 6.25}
    assert sum_stats([a]) == a


def test_clean_resources_keeps_only_the_contract():
    assert clean_resources({"cpuPercent": 3, "memoryBytes": 1.9, "pids": -1, "evil": "x", "memoryPercent": True,
                            "memoryLimitBytes": float("nan")}) == {"cpuPercent": 3.0, "memoryBytes": 1}
    assert clean_resources(None) == {}


# ---- engine metrics -------------------------------------------------------------------------------------------------
def test_metrics_carry_device_and_deployment_resources(lab, tmp_path):
    cfg, eng, drv, deps = lab
    eng.host_resources = sampler_for(fake_host(tmp_path / "host"), eng.dir)
    drv.set_resources(deps[0], cpuPercent=12.5, memoryBytes=64 << 20, memoryLimitBytes=512 << 20,
                      memoryPercent=12.5, pids=3)
    m = eng.metrics()
    assert "deploymentResources" not in m                                  # nothing sampled yet
    eng.observer.step()
    m = eng.metrics()
    contracts.validate("telemetry#Metrics", m)
    assert m["resources"]["memory"]["percent"] == 25.0
    r = m["deploymentResources"]
    assert set(r) == {deps[0]}                                             # the other one: not measured
    assert r[deps[0]]["driver"] == "fake-observable" and r[deps[0]]["pids"] == 3 and r[deps[0]]["sampledAt"] > 0
    st, body = 0, None
    resp = api_for(eng, cfg).handle("GET", "/v1/metrics")
    st, body = resp.status, json.loads(resp.body)
    assert st == 200 and body[DEVICE]["deploymentResources"] == r
    drv.remove(deps[0])                                                    # gone: left out of the next sample
    eng.observer.sample_resources()
    assert "deploymentResources" not in eng.metrics()


def test_real_host_sample_validates(lab):
    _, eng, _, _ = lab
    m = eng.metrics()
    contracts.validate("telemetry#Metrics", m)
    if os.path.exists("/proc/meminfo"):
        assert m["resources"]["memory"]["totalBytes"] > 0 and m["resources"]["storage"]["path"] == str(eng.dir)


def test_child_device_reports_no_host_resources(tmp_path):
    from flexengine2.core.reconciler import Engine
    from fe_unit_fakes import make_engine

    base = make_engine(tmp_path)
    child = Engine(dict(base.cfg, deviceId="mcu-1", dataDir=str(tmp_path / "mcu-1")), {}, role="child-device")
    assert child.host_resources is None and "resources" not in child.metrics()
    contracts.validate("telemetry#Metrics", child.metrics())


def test_drivers_without_observation_are_skipped(tmp_path):
    from _local_fixtures import FakeDriver

    cfg, _ = write_config(tmp_path, socket=False)
    eng = installed_engine(cfg, FakeDriver())
    assert not supports(FakeDriver, "resources") and not supports(FakeDriver, "logs")
    eng.observer.step()
    assert "deploymentResources" not in eng.metrics()
    assert not (eng.dir / workloads.LOG_FILE).exists()


def test_a_driver_that_raises_is_logged_once_and_never_stops_the_observer(lab, monkeypatch):
    _, eng, drv, deps = lab
    seen = []
    monkeypatch.setattr(eng, "log", lambda msg, level="info", event=None, **kv: seen.append((event, level, msg)))
    drv.fail_observation = True
    for _ in range(3):
        eng.observer.sample_resources()
        eng.observer.collect_logs()
    warn = [s for s in seen if s[0] == "observe.failed"]
    assert len(warn) == 1 + len(deps), warn                                 # once per (kind, driver/deployment)
    assert all(s[1] == "warning" for s in warn)
    drv.fail_observation = False
    drv.write(deps[0], "back")
    assert eng.observer.collect_logs() == 1


# ---- workload logs ----------------------------------------------------------------------------------------------
def _file(eng):
    p = eng.dir / workloads.LOG_FILE
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_workload_lines_are_collected_once_and_served_as_source_workload(lab):
    cfg, eng, drv, deps = lab
    d = deps[0]
    drv.write(d, "listening on 8080", t=1793095200.25)
    drv.write(d, "disk almost full", stream="stderr", t=1793095201.0)
    drv.write(d, "x" * 5000, stream=None, t=1793095202.0)
    assert eng.observer.collect_logs() == 3
    assert eng.observer.collect_logs() == 0                                 # the cursor moved on
    recs = _file(eng)
    assert [r["msg"][:17] for r in recs] == ["listening on 8080", "disk almost full", "x" * 17]
    assert recs[2]["truncated"] is True and len(recs[2]["msg"]) == workloads.MSG_MAX and "stream" not in recs[2]
    lines = [json.loads(x) for x in api_for(eng, cfg).handle("GET", "/v1/logs?tail=500").body.splitlines()]
    wl = [x for x in lines if x["source"] == "workload"]
    assert len(wl) == 3
    for x in wl:
        contracts.validate("telemetry#WorkloadLogRecord", x)
        assert x["device"] == DEVICE and x["dep"] == x["deployment"] == d and x["event"] == "workload"
    assert wl[0]["t"] == 1793095200.25 and wl[0]["ts"].startswith("2026-10-27T10:00:00.25")
    assert wl[1]["stream"] == "stderr"
    assert "(stderr)" in format_log(wl[1]) and format_log(wl[0]).endswith(f"{d}: listening on 8080")
    # offline (CLI without a running engine) reads the same file
    off = LocalApi(OfflineProvider(cfg), socket_path=None, log=lambda m: None)
    assert sum(1 for x in off.handle("GET", "/v1/logs?tail=500").body.splitlines()
               if json.loads(x)["source"] == "workload") == 3


def test_cursor_survives_an_engine_restart(lab):
    _, eng, drv, deps = lab
    drv.write(deps[0], "one")
    eng.observer.collect_logs()
    again = WorkloadObserver(eng, None)                                     # a new engine start: cursor from disk
    drv.write(deps[0], "two")
    assert again.collect_logs() == 1
    assert [r["msg"] for r in _file(eng)] == ["one", "two"]
    assert json.loads((eng.dir / workloads.CURSOR_FILE).read_text())[deps[0]] == "2"


def test_lines_without_time_get_the_collection_time_and_a_batch_is_bounded(lab):
    _, eng, drv, deps = lab
    for i in range(workloads.LINES_PER_CALL + 50):
        drv._lines.setdefault(deps[0], []).append({"msg": f"l{i}"})        # no t, no stream
    before = time.time()
    assert eng.observer.collect_logs() == workloads.LINES_PER_CALL          # the fake hands the newest `limit`
    recs = _file(eng)
    assert recs[-1]["msg"] == f"l{workloads.LINES_PER_CALL + 49}" and recs[0]["t"] >= before - 1


def test_workload_log_rotation_bounds_the_disk(lab):
    cfg, eng, drv, deps = lab
    eng.observer.cfg["workloadLogMaxBytes"] = 4096
    for i in range(400):
        drv.write(deps[0], f"line {i:04d} " + "y" * 40, t=1793095200.0 + i)
        eng.observer.collect_logs()
    cur, old = eng.dir / workloads.LOG_FILE, eng.dir / (workloads.LOG_FILE + ".1")
    assert old.exists() and cur.stat().st_size <= 4096 and old.stat().st_size <= 4096
    lines = [json.loads(x) for x in api_for(eng, cfg).handle("GET", "/v1/logs?tail=10000").body.splitlines()]
    msgs = [x["msg"][:9] for x in lines if x["source"] == "workload"]
    in_old = json.loads(old.read_text().splitlines()[0])["msg"][:9]
    assert msgs[-1] == "line 0399" and msgs == sorted(msgs) and msgs[0] == in_old   # tail spans .1 and the file


def test_follow_continues_across_a_rotation(lab):
    cfg, eng, drv, deps = lab
    eng.observer.cfg["workloadLogMaxBytes"] = 4096
    api = api_for(eng, cfg)
    done = threading.Event()
    r = api.handle("GET", "/v1/logs?follow=1&tail=0", closed=done.is_set)
    got: list[str] = []

    def read():
        for chunk in r.body:
            got.extend(json.loads(x)["msg"][:9] for x in chunk.splitlines() if b'"workload"' in x)
            if "line 0119" in got:
                done.set()
                return
    t = threading.Thread(target=read, daemon=True)
    t.start()
    for i in range(120):
        drv.write(deps[0], f"line {i:04d} " + "z" * 40, t=1793095200.0 + i)
        eng.observer.collect_logs()
        time.sleep(0.01)
    t.join(15)
    done.set()
    assert (eng.dir / (workloads.LOG_FILE + ".1")).exists()
    assert "line 0119" in got and len(set(got)) == len(got), got[-5:]      # nothing twice after the rotation


# ---- driving and configuration ------------------------------------------------------------------------------------
def test_step_schedule_and_off(lab):
    _, eng, drv, deps = lab
    obs = WorkloadObserver(eng, {"resourcesEverySeconds": 10, "workloadLogsEverySeconds": 4})
    assert obs.step(100.0) == 4.0
    assert obs.step(102.0) == 2.0
    off = WorkloadObserver(eng, {"resourcesEverySeconds": 0, "workloadLogsEverySeconds": 0})
    assert off.step(0.0) == workloads.IDLE and off.start() is None


def test_observer_thread_runs_with_the_engine_and_stops(lab):
    _, eng, drv, deps = lab
    eng.observer.cfg.update(resourcesEverySeconds=0.05, workloadLogsEverySeconds=0.05)
    drv.set_resources(deps[0], memoryBytes=1 << 20)
    drv.write(deps[0], "hello from the thread")
    stop = threading.Event()
    t = eng.observer.start(stop)
    assert eng.observer.start(stop) is t                                    # idempotent
    end = time.time() + 5
    while time.time() < end and not (eng.metrics().get("deploymentResources") and _file(eng)):
        time.sleep(0.02)
    stop.set()
    t.join(5)
    assert not t.is_alive() and _file(eng)[0]["msg"] == "hello from the thread"


def test_config_observe_block(tmp_path):
    from flexengine2.config.load import validate

    base, _ = write_config(tmp_path, socket=False)
    assert validate(base) == []
    assert validate({**base, "observe": {"resourcesEverySeconds": 0, "workloadLogsEverySeconds": 2.5,
                                         "workloadLogMaxBytes": 65536}}) == []
    errs = validate({**base, "observe": {"resourcesEvery": 1}})
    assert errs and errs[0]["path"].startswith("/observe")
    assert validate({**base, "observe": {"workloadLogMaxBytes": 10}})
    assert workloads.settings(None) == {"resourcesEverySeconds": 10.0, "workloadLogsEverySeconds": 5.0,
                                        "workloadLogMaxBytes": 1 << 20}


def test_host_gives_every_engine_the_observe_block(tmp_path):
    from flexengine2.host import device_cfg

    cfg, _ = write_config(tmp_path, socket=False)
    cfg["observe"] = {"workloadLogsEverySeconds": 1}
    assert device_cfg(cfg, cfg["devices"][0])["observe"] == {"workloadLogsEverySeconds": 1}

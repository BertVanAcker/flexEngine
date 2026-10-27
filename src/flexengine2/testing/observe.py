"""Test helpers for UM-4 (resources and workload logs), for this repository and for consumers of the local API
(flexMonitor, flexDeploy) that test against a real `Engine` without a container runtime.

    drv = ObservableFakeDriver()                         # in-memory SPI driver (profile native) with observation
    eng = Engine(cfg, {"native": drv})                   # Engine.run() starts the observer thread itself
    drv.set_resources("d1", cpuPercent=12.5, memoryBytes=64 << 20, memoryLimitBytes=512 << 20, pids=3)
    drv.write("d1", "listening on 8080")                 # a stdout line of deployment d1
    drv.write("d1", "disk almost full", stream="stderr")
    eng.observer.step()                                  # or wait for the observer thread (observe.* seconds)
    eng.metrics()["deploymentResources"]["d1"]           # GET /v1/metrics
    # GET /v1/logs?device=...: the two lines with source "workload", dep "d1", stream stdout / stderr

    root = fake_host(tmp_path, cpu=(100, 300), memory=(8 << 30, 6 << 30), temperature=48.5)
    HostSampler(data_dir, proc=str(root / "proc"), sys_root=str(root / "sys"))   # fixed device resources
    engine.host_resources = sampler_for(root, data_dir)                          # into a running Engine

The fake driver keeps everything in memory; `fail_observation = True` makes both members raise (the engine must
survive a driver that breaks the "never raises" rule).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from flexengine2.drivers.base import Driver, Observed
from flexengine2.obs.resources import HostSampler

__all__ = ["ObservableFakeDriver", "fake_host", "sampler_for"]


class ObservableFakeDriver(Driver):
    """In-memory executor: apply = running, healthy unless `bad_versions`; resources() and logs() from what the
    test sets (`set_resources`, `write`)."""

    profile = "native"
    name = "fake-observable"

    def __init__(self, bad_versions: set[str] | None = None, profile: str | None = None):
        if profile:
            self.profile = profile
        self.bad_versions = set(bad_versions or ())
        self.running: dict[str, dict] = {}
        self._res: dict[str, dict] = {}
        self._lines: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self.fail_observation = False

    # ---- SPI ------------------------------------------------------------------------------------------------
    def plan(self, current, desired):
        return [f"fake apply {desired.version}"] if desired else [f"fake remove {current.dep_id}"]

    def apply(self, current, desired):
        self.running[desired.dep_id] = {"digest": desired.digest, "version": desired.version}

    def remove(self, dep_id):
        self.running.pop(dep_id, None)

    def status(self, dep_id):
        r = self.running.get(dep_id)
        return Observed(present=bool(r), running=bool(r), digest=(r or {}).get("digest"),
                        version=(r or {}).get("version"))

    def list(self):
        return {k: self.status(k) for k in list(self.running)}

    def health(self, dep_id, probe):
        r = self.running.get(dep_id)
        return bool(r) and r["version"] not in self.bad_versions

    def failed(self, dep_id):
        r = self.running.get(dep_id)
        return "process exited (Restart=never)" if r and r["version"] in self.bad_versions else None

    # ---- observation ----------------------------------------------------------------------------------------
    def set_resources(self, dep_id: str, **values) -> None:
        """DeploymentResources of a deployment (cpuPercent, memoryBytes, memoryLimitBytes, memoryPercent, pids);
        no values: the driver cannot measure it."""
        with self._lock:
            if values:
                self._res[dep_id] = dict(values)
            else:
                self._res.pop(dep_id, None)

    def write(self, dep_id: str, msg: str, stream: str | None = "stdout", t: float | None = None) -> None:
        """One line of the deployment's workload (t: the executor's time; None: now)."""
        with self._lock:
            ln = {"msg": msg, "t": time.time() if t is None else t}
            if stream:
                ln["stream"] = stream
            self._lines.setdefault(dep_id, []).append(ln)

    def resources(self, dep_ids):
        if self.fail_observation:
            raise RuntimeError("observation broken (test)")
        with self._lock:
            return {d: dict(self._res[d]) for d in dep_ids if d in self._res and d in self.running}

    def logs(self, dep_id, cursor, limit):
        if self.fail_observation:
            raise RuntimeError("observation broken (test)")
        with self._lock:
            lines = list(self._lines.get(dep_id, []))
        start = int(cursor) if cursor and cursor.isdigit() else max(0, len(lines) - limit)
        new = lines[start:]
        out = {"lines": new[-limit:], "cursor": str(len(lines))}
        if len(new) > limit:
            out["dropped"] = len(new) - limit
        return out


def fake_host(root: str | Path, *, cpu: tuple[int, int] = (250, 1000), cpus: int = 4,
              load: tuple[float, float, float] = (0.5, 0.4, 0.3), memory: tuple[int, int] = (8 << 30, 6 << 30),
              temperature: float | None = 47.0) -> Path:
    """A /proc and /sys tree under `root` for HostSampler: cpu = (busy, total) jiffies so far, memory = (total,
    available) bytes, temperature of a `cpu-thermal` zone (None: no thermal zone). Call it again with larger cpu
    jiffies to move the CPU percent: (busy delta) / (total delta)."""
    root = Path(root)
    proc, zone = root / "proc", root / "sys" / "class" / "thermal" / "thermal_zone0"
    proc.mkdir(parents=True, exist_ok=True)
    busy, total = cpu
    idle = total - busy
    line = f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n"
    per = "".join(f"cpu{i} {busy // cpus} 0 0 {idle // cpus} 0 0 0 0 0 0\n" for i in range(cpus))
    (proc / "stat").write_text(line + per + "intr 0\nctxt 0\n")
    (proc / "loadavg").write_text(f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f} 1/100 4242\n")
    tot, avail = memory
    (proc / "meminfo").write_text(f"MemTotal:       {tot // 1024} kB\nMemFree:        {avail // 2048} kB\n"
                                  f"MemAvailable:   {avail // 1024} kB\n")
    if temperature is not None:
        zone.mkdir(parents=True, exist_ok=True)
        (zone / "type").write_text("cpu-thermal\n")
        (zone / "temp").write_text(f"{int(temperature * 1000)}\n")
    return root


def sampler_for(root: str | Path, data_dir: str | Path, min_interval: float = 0.0) -> HostSampler:
    """A HostSampler on a fake_host() tree (min_interval 0: every call samples)."""
    root = Path(root)
    return HostSampler(data_dir, proc=str(root / "proc"), sys_root=str(root / "sys"), min_interval=min_interval)

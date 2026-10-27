"""Device resources (UM-4): CPU, memory, storage of the engine's data volume and temperature of the host.

    s = HostSampler(data_dir)          # takes the CPU baseline now
    s.sample()                         # -> DeviceResources (telemetry.v1 `$defs/DeviceResources`) or None

The engine serves the sample as `resources` in `Engine.metrics()` (GET /v1/metrics, CP-8 `Metrics`). Bounded cost:
a sample reads a handful of small kernel files (`/proc/loadavg`, `/proc/stat`, `/proc/meminfo`,
`/sys/class/thermal/thermal_zone*/{type,temp}`) and one `statvfs` of the data directory; it never runs a process and
never blocks. Two calls within `min_interval` seconds answer the same sample (a client polling the API often costs
nothing more). The CPU percent is the busy share of all CPUs between two samples (the first one: since the sampler
was made). What a platform does not tell (macOS has no procfs: no CPU percent, no memory) is left out, never
guessed; each part of the sample is optional.

Helpers shared by the drivers' `resources()` (drivers/base.py, SPI 1.0 optional member): `parse_size` ("1.5MiB"),
`parse_percent` ("12.5%"), `ProcCpu` (CPU percent of processes from /proc/<pid>/stat between two calls).
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

MIN_INTERVAL = 1.0           # seconds: samples closer together than this are served from the last one
THERMAL_MAX_ZONES = 16       # bounded scan of /sys/class/thermal
CPU_ZONE = re.compile(r"cpu|soc|pkg|x86|core|package|tcpu|ap", re.I)

__all__ = ["HostSampler", "ProcCpu", "parse_percent", "parse_size", "proc_rss", "rfc3339_ns", "MIN_INTERVAL"]


def _read(path: Path) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read(65536)
    except OSError:
        return None


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 2) if whole > 0 else None


class HostSampler:
    """Device resources of the host the engine runs on (thread-safe; see the module docstring)."""

    def __init__(self, data_dir: str | os.PathLike, *, min_interval: float = MIN_INTERVAL, proc: str = "/proc",
                 sys_root: str = "/sys", clock=time.time):
        self.data_dir = str(data_dir)
        self.min_interval = float(min_interval)
        self.proc, self.sys_root = Path(proc), Path(sys_root)
        self.clock = clock
        self._lock = threading.Lock()
        self._last: dict | None = None
        self._last_at: float | None = None
        self._cpu_prev = self._cpu_times()

    # ---- parts ------------------------------------------------------------------------------------------------
    def _cpu_times(self) -> tuple[int, int] | None:
        """(busy, total) jiffies of all CPUs from the first line of /proc/stat."""
        text = _read(self.proc / "stat")
        if not text or not text.startswith("cpu "):
            return None
        try:
            v = [int(x) for x in text.splitlines()[0].split()[1:]]
        except ValueError:
            return None
        if len(v) < 4:
            return None
        idle = v[3] + (v[4] if len(v) > 4 else 0)            # idle + iowait
        total = sum(v[:8])                                   # guest time is already in user/nice
        return total - idle, total

    def _cpu_count(self) -> int | None:
        text = _read(self.proc / "stat")
        if text:
            n = sum(1 for ln in text.splitlines() if re.match(r"cpu\d+ ", ln))
            if n:
                return n
        return os.cpu_count()

    def _cpu(self) -> dict | None:
        out: dict = {}
        n = self._cpu_count()
        if n:
            out["count"] = n
        text = _read(self.proc / "loadavg")
        load = None
        if text:
            try:
                load = tuple(float(x) for x in text.split()[:3])
            except ValueError:
                load = None
        if load is None and not (self.proc / "loadavg").exists() and str(self.proc) == "/proc":
            try:
                load = os.getloadavg()                       # macOS, BSD: no procfs
            except OSError:
                load = None
        if load and len(load) == 3:
            out.update(load1=round(load[0], 2), load5=round(load[1], 2), load15=round(load[2], 2))
        now = self._cpu_times()
        prev, self._cpu_prev = self._cpu_prev, now or self._cpu_prev
        if now and prev and now[1] > prev[1]:
            p = _pct(max(0, now[0] - prev[0]), now[1] - prev[1])
            if p is not None:
                out["percent"] = min(100.0, p)
        return out or None

    def _memory(self) -> dict | None:
        text = _read(self.proc / "meminfo")
        if not text:
            return None
        kv = {}
        for ln in text.splitlines():
            k, _, rest = ln.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                kv[k.strip()] = int(parts[0]) * (1024 if parts[1:2] == ["kB"] else 1)
        total = kv.get("MemTotal")
        if not total:
            return None
        avail = kv.get("MemAvailable")
        if avail is None:                                    # kernels before 3.14
            avail = kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0)
        used = max(0, total - avail)
        return {"totalBytes": total, "usedBytes": used, "availableBytes": avail, "percent": _pct(used, total)}

    def _storage(self) -> dict | None:
        try:
            st = os.statvfs(self.data_dir)
        except OSError:
            return None
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        free = st.f_bavail * st.f_frsize                     # what the engine's user can still write
        used = max(0, (st.f_blocks - st.f_bfree) * st.f_frsize)
        # df's percent: used of what is usable by unprivileged users (used + available)
        return {"path": self.data_dir, "totalBytes": total, "usedBytes": used, "freeBytes": free,
                "percent": _pct(used, used + free)}

    def _temperature(self) -> float | None:
        base = self.sys_root / "class" / "thermal"
        try:
            zones = sorted(p for p in base.iterdir() if p.name.startswith("thermal_zone"))[:THERMAL_MAX_ZONES]
        except OSError:
            return None
        best, first = None, None
        for z in zones:
            raw = _read(z / "temp")
            try:
                c = int(raw.strip()) / 1000.0 if raw else None
            except ValueError:
                c = None
            if c is None or not (-60.0 < c < 200.0):        # absent or a sensor that reports garbage
                continue
            first = c if first is None else first
            if CPU_ZONE.search((_read(z / "type") or "").strip()):
                best = c if best is None else max(best, c)
        t = best if best is not None else first
        return round(t, 1) if t is not None else None

    # ---- the sample -------------------------------------------------------------------------------------------
    def sample(self) -> dict | None:
        """DeviceResources, or None when the platform tells nothing at all. Never raises."""
        with self._lock:
            now = self.clock()
            if self._last_at is not None and 0 <= now - self._last_at < self.min_interval:
                return self._last
            out: dict = {"sampledAt": round(now, 3)}
            try:
                for key, fn in (("cpu", self._cpu), ("memory", self._memory), ("storage", self._storage)):
                    v = fn()
                    if v:
                        out[key] = {k: x for k, x in v.items() if x is not None}
                t = self._temperature()
                if t is not None:
                    out["temperatureCelsius"] = t
            except Exception:  # noqa: BLE001 - diagnostics never break metrics()
                pass
            self._last = out if len(out) > 1 else None
            self._last_at = now
            return self._last


# ---- helpers for the drivers' resources() ----------------------------------------------------------------------
_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?i?B|[kKMGTP]?B?)?\s*$")
_MULT = {"": 1, "B": 1, "K": 1000, "KB": 1000, "M": 1000 ** 2, "MB": 1000 ** 2, "G": 1000 ** 3, "GB": 1000 ** 3,
         "T": 1000 ** 4, "TB": 1000 ** 4, "P": 1000 ** 5, "PB": 1000 ** 5, "KIB": 1024, "MIB": 1024 ** 2,
         "GIB": 1024 ** 3, "TIB": 1024 ** 4, "PIB": 1024 ** 5}


def parse_size(text) -> int | None:
    """`docker stats` sizes: "1.5MiB", "12kB", "0B", "--" -> bytes (None when unreadable)."""
    m = _SIZE.match(str(text or ""))
    if not m:
        return None
    mult = _MULT.get((m.group(2) or "").upper())
    return int(float(m.group(1)) * mult) if mult else None


def parse_percent(text) -> float | None:
    """"12.50%" -> 12.5 (None for "--" or anything else)."""
    s = str(text or "").strip().rstrip("%").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def proc_rss(pid: int, proc: str = "/proc") -> int | None:
    """Resident memory of one process in bytes (/proc/<pid>/statm), None when it is gone or there is no procfs."""
    text = _read(Path(proc) / str(int(pid)) / "statm")
    try:
        return int(text.split()[1]) * os.sysconf("SC_PAGE_SIZE") if text else None
    except (ValueError, IndexError, OSError):
        return None


class ProcCpu:
    """CPU percent of processes between two calls (100 = one CPU), from utime + stime of /proc/<pid>/stat."""

    def __init__(self, proc: str = "/proc", clock=time.monotonic):
        self.proc, self.clock = Path(proc), clock
        self._prev: dict[str, tuple[float, float]] = {}       # key -> (cpu seconds, at)
        try:
            self._hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            self._hz = 100

    def cpu_seconds(self, pid: int) -> float | None:
        text = _read(self.proc / str(int(pid)) / "stat")
        if not text:
            return None
        try:
            f = text.rsplit(") ", 1)[1].split()
            return (int(f[11]) + int(f[12])) / float(self._hz)
        except (IndexError, ValueError):
            return None

    def percent(self, key: str, cpu_seconds: float | None) -> float | None:
        """Percent since the last call with this key (None on the first one, or when the counter went back)."""
        now = self.clock()
        if cpu_seconds is None:
            self._prev.pop(key, None)
            return None
        prev = self._prev.get(key)
        self._prev[key] = (cpu_seconds, now)
        if prev is None or now <= prev[1] or cpu_seconds < prev[0]:
            return None
        return round(100.0 * (cpu_seconds - prev[0]) / (now - prev[1]), 2)

    def forget(self, keep: set[str]) -> None:
        for k in [k for k in self._prev if k not in keep]:
            del self._prev[k]


_RFC3339 = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")


def rfc3339_ns(ts: str) -> tuple[int, int] | None:
    """`docker logs --timestamps` time ("2026-10-27T10:00:00.123456789Z") -> (unix seconds, nanoseconds), exact to
    the nanosecond (a float is not: two lines in one microsecond must still compare right). None when unreadable."""
    import datetime as _dt

    m = _RFC3339.match(str(ts or ""))
    if not m:
        return None
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        sec = int(_dt.datetime.fromisoformat(m.group(1) + zone).timestamp())
    except ValueError:
        return None
    return sec, int((m.group(2) or "0").ljust(9, "0"))

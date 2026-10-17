"""Native driver on real systemd (EV-19, V-33): every deployment is a systemd unit that systemd owns.

Design (docs/flexengine-v2-plan.md §5 EV-19):

* **Unit files, not transient units.** A workload is the unit file ``flex-<deviceId>-<depId>.service`` in the unit
  directory (system manager: ``/etc/systemd/system``; ``scope: user``: ``~/.config/systemd/user``), enabled for
  ``multi-user.target`` (user: ``default.target``). A unit file survives an engine restart, an engine upgrade and a
  reboot, so the workload runs without the engine; a transient unit (``systemd-run``) is gone after a reboot and
  would need the engine to come back first.
* **Identity in the unit file.** ``X-Flex-Device``, ``X-Flex-Deployment``, ``X-Flex-Module``, ``X-Flex-Version``,
  ``X-Flex-Digest`` and ``X-Flex-Port`` in ``[Unit]`` (systemd ignores ``X-`` keys). ``list()`` / ``status()``
  rebuild everything from the unit files plus ``systemctl show``: a new driver instance adopts what an earlier one
  started without a second ``apply``.
* **Payload** (the verified tar.gz) is unpacked once per digest into ``<dataDir>/native/<dep>/<digest16>/`` (atomic:
  ``.partial`` + rename); the current, the previous and the rolled-back-from trees are kept, so a rollback never
  unpacks again. ``ExecStart`` is ``<engine python> app.py`` or the ``ExecStart`` list of the payload's ``unit.json``
  (``{python}`` = the engine's interpreter; a relative program path is resolved in the payload tree). The
  workload's writable directory is ``<dataDir>/native/<dep>/data`` (``FLEX_DATA_DIR``).
* **Restart policy** from the signed descriptor's ``restartPolicy``: ``Restart=`` on-failure | always | no (never),
  ``RestartSec=restartSec`` (default 0.5 s), ``StartLimitIntervalSec=60`` and ``StartLimitBurst=maxRetries + 1``
  (the first start counts: ``maxRetries`` automatic restarts within 60 s, the next failure is ``start-limit-hit``).
* **Environment**: only explicit variables (``FLEX_UNIT``, ``FLEX_HEALTH_PORT``, ``FLEX_DATA_DIR``,
  ``FLEX_PARAM_<NAME>`` and two Python constants); never the engine's environment (systemd gives the unit its own
  minimal environment).
* **Hardening** (system manager): ``NoNewPrivileges``, ``PrivateTmp``, ``ProtectSystem=strict`` with
  ``ReadWritePaths=`` the workload's data directory, ``User=`` (config ``user``; default the engine's user, no line
  when the engine runs as root). The user manager gets ``NoNewPrivileges`` only (mount namespacing in user units
  needs unprivileged user namespaces).
* **CLI only**: ``systemctl [--user] --no-pager show -p … | daemon-reload | enable | disable | restart | stop |
  reset-failed`` with timeouts; no D-Bus library needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload, http_probe, wait_until
from flexengine2.util import atomic_write_bytes, atomic_write_json, fsync_dir, fsync_tree, read_json

__all__ = ["SystemdDriver", "unit_name", "sanitize"]

SHOW = ["ActiveState", "SubState", "Result", "MainPID", "NRestarts", "LoadState", "ExecMainStatus",
        "ExecMainExitTimestampMonotonic"]
RESTART = {"on-failure": "on-failure", "always": "always", "never": "no"}
START_LIMIT_INTERVAL = 60
SYSTEM_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system")
_BAD = re.compile(r"[^A-Za-z0-9_.]")


def sanitize(s: str, limit: int = 80) -> str:
    """A unit-name-safe form of an id: [A-Za-z0-9_.] kept, anything else -> '_'; a changed or shortened id gets a
    short hash of the original so two ids never map to the same name."""
    out = _BAD.sub("_", s)
    if out != s or len(out) > limit or not out:
        out = f"{out[:limit - 9]}_{hashlib.sha256(s.encode()).hexdigest()[:8]}"
    return out


def unit_name(device_id: str, dep_id: str) -> str:
    return f"flex-{sanitize(device_id)}-{sanitize(dep_id)}.service"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _extract(src: str, dst: Path) -> None:
    """Unpack the verified payload (the driver's only "transfer"; tests count calls)."""
    with tarfile.open(src, "r:gz") as tf:
        tf.extractall(dst, filter="data")


def _q(s: str, exec_line: bool = False) -> str:
    """One double-quoted word for a unit file (C escapes, %-specifiers off, $-expansion off in Exec lines)."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    s = s.replace("%", "%%")
    if exec_line:
        s = s.replace("$", "$$")
    return f'"{s}"'


def _line(v) -> str:
    return " ".join(str(v).split())


def _param_env(k: str) -> str:
    return "FLEX_PARAM_" + re.sub(r"[^A-Z0-9_]", "_", str(k).upper())


def exec_argv(names: set[str], unit_json: bytes | None) -> list[str]:
    """ExecStart argv template of a payload ({python} unresolved). DriverError when the payload cannot run."""
    if unit_json is not None:
        try:
            spec = json.loads(unit_json)
        except ValueError as e:
            raise DriverError(f"unit.json is not valid JSON: {e}") from None
        argv = spec.get("ExecStart") if isinstance(spec, dict) else None
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
            raise DriverError("unit.json: ExecStart must be a non-empty list of strings")
        return list(argv)
    if "app.py" not in names:
        raise DriverError("native payload has neither app.py nor unit.json")
    return ["{python}", "app.py"]


class SystemdDriver(Driver):
    profile = "native"
    name = "systemd"

    def __init__(self, device_id: str, data_dir: Path, *, scope: str = "system", unit_dir: str | Path | None = None,
                 user: str | None = None, systemctl: str = "systemctl", python: str | None = None,
                 timeout: float = 30.0):
        if scope not in ("system", "user"):
            raise ValueError(f"scope must be system or user, got {scope!r}")
        self.device = device_id
        self.scope = scope
        self.root = Path(data_dir).absolute() / "native"
        self.systemctl = systemctl
        self.python = python or sys.executable
        self.timeout = float(timeout)
        if unit_dir is None:
            if scope == "system":
                unit_dir = SYSTEM_UNIT_DIRS[0]
            else:
                cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
                unit_dir = os.path.join(cfg, "systemd", "user")
        self.unit_dir = Path(unit_dir)
        self.user = user

    # ---- names, files ---------------------------------------------------------------------------------------
    def unit(self, dep_id: str) -> str:
        return unit_name(self.device, dep_id)

    def unit_path(self, dep_id: str) -> Path:
        return self.unit_dir / self.unit(dep_id)

    def dep_dir(self, dep_id: str) -> Path:
        return self.root / sanitize(dep_id)

    def _standard_dir(self) -> bool:
        if self.scope == "system":
            return str(self.unit_dir) in SYSTEM_UNIT_DIRS
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        return str(self.unit_dir) in (os.path.join(cfg, "systemd", "user"), "/etc/systemd/user")

    def _read_unit(self, path: Path) -> dict | None:
        """X-Flex-* identity of one of our unit files (None: missing, unreadable or not this device's)."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        meta = {}
        for ln in text.splitlines():
            if ln.startswith("X-Flex-") and "=" in ln:
                k, v = ln.split("=", 1)
                meta[k[len("X-Flex-"):].lower()] = v
        if meta.get("device") != self.device or not meta.get("deployment"):
            return None
        meta["text"] = text
        return meta

    def _units(self) -> dict[str, dict]:
        out = {}
        try:
            paths = sorted(self.unit_dir.glob(f"flex-{sanitize(self.device)}-*.service"))
        except OSError:
            return out
        for p in paths:
            meta = self._read_unit(p)
            if meta and p.name == self.unit(meta["deployment"]):
                out[meta["deployment"]] = meta
        return out

    def _meta(self, dep_id: str) -> dict | None:
        meta = self._read_unit(self.unit_path(dep_id))
        return meta if meta and meta["deployment"] == dep_id else None

    def _state_path(self, dep_id: str) -> Path:
        return self.dep_dir(dep_id) / "systemd-state.json"

    def _unit_user(self) -> str | None:
        if self.scope == "user":
            return None
        if self.user:
            return self.user
        if os.geteuid() == 0:
            return None
        import pwd

        try:
            return pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return str(os.geteuid())

    # ---- systemctl --------------------------------------------------------------------------------------------
    def _ctl(self, *args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
        cmd = [self.systemctl, *(["--user"] if self.scope == "user" else []), "--no-pager", *args]
        t = timeout or self.timeout
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=t, stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise DriverError(f"systemctl: executable not found ({e})") from None
        except subprocess.TimeoutExpired:
            raise DriverError(f"systemctl {args[0]}: no answer within {t:g} s") from None
        except OSError as e:
            raise DriverError(f"systemctl: cannot run ({e})") from None
        if check and p.returncode != 0:
            raise DriverError(f"systemctl {' '.join(args)}: {_line(p.stderr or p.stdout)} (exit {p.returncode})")
        return p

    def _show(self, unit: str) -> dict:
        p = self._ctl("show", "-p", ",".join(SHOW), unit, timeout=min(self.timeout, 10))
        out = {}
        for ln in p.stdout.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                out[k] = v
        if "ActiveState" not in out:
            raise DriverError(f"systemctl show {unit}: unreadable output")
        return out

    # ---- payload, rendering ---------------------------------------------------------------------------------
    def _unpack(self, wl: Workload) -> Path:
        d = self.dep_dir(wl.dep_id) / wl.digest.split(":")[-1][:16]
        if (d / ".complete").exists():
            return d
        part = d.with_name(d.name + ".partial")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)
        try:
            _extract(wl.payload_path, part)
        except (tarfile.TarError, OSError) as e:
            shutil.rmtree(part, ignore_errors=True)
            raise DriverError(f"native payload unusable: {e}") from None
        fsync_tree(part)   # durable before it is marked complete (EV-13: power cut after unpack)
        atomic_write_bytes(part / ".complete", wl.digest.encode())
        shutil.rmtree(d, ignore_errors=True)
        os.replace(part, d)
        fsync_dir(d.parent)
        return d

    def _exec_start(self, workdir: Path) -> list[str]:
        uj = workdir / "unit.json"
        names = {p.name for p in workdir.iterdir()}
        argv = exec_argv(names, uj.read_bytes() if uj.exists() else None)
        argv = [a.replace("{python}", self.python) for a in argv]
        if "/" in argv[0] and not os.path.isabs(argv[0]):
            argv[0] = str(workdir / argv[0])
        return argv

    def environment(self, wl: Workload, port: int) -> dict[str, str]:
        env = {"FLEX_UNIT": f"{self.device}/{wl.dep_id}", "FLEX_HEALTH_PORT": str(port),
               "FLEX_DATA_DIR": str(self.dep_dir(wl.dep_id) / "data"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        for k, v in sorted((wl.parameters or {}).items()):
            env[_param_env(k)] = str(v)
        return env

    def render(self, wl: Workload, workdir: Path, port: int, argv: list[str] | None = None) -> str:
        """The unit file of a workload (deterministic for fixed inputs: golden-tested)."""
        argv = argv or self._exec_start(workdir)
        pol = wl.restart or {}
        policy = pol.get("policy") or "on-failure"
        burst = int(pol.get("maxRetries", 5)) + 1
        rsec = int(round(float(pol.get("restartSec", 0.5)) * 1000))
        data = self.dep_dir(wl.dep_id) / "data"
        user = self._unit_user()
        lines = [
            "# Managed by flexEngineV2 (native driver: systemd). Do not edit: the engine rewrites it on every apply.",
            "[Unit]",
            f"Description=flex {_line(wl.module)} {_line(wl.version)} ({self.device}/{wl.dep_id})".replace("%", "%%"),
            "After=network-online.target",
            "Wants=network-online.target",
            f"StartLimitIntervalSec={START_LIMIT_INTERVAL}",
            f"StartLimitBurst={burst}",
            f"X-Flex-Device={self.device}",
            f"X-Flex-Deployment={wl.dep_id}",
            f"X-Flex-Module={_line(wl.module)}",
            f"X-Flex-Version={_line(wl.version)}",
            f"X-Flex-Digest={_line(wl.digest)}",
            f"X-Flex-Port={port}",
            "",
            "[Service]",
            "Type=exec",
            f"WorkingDirectory={str(workdir).replace('%', '%%')}",
            "ExecStart=" + " ".join(_q(a, exec_line=True) for a in argv),
        ]
        lines += [f"Environment={_q(f'{k}={v}')}" for k, v in self.environment(wl, port).items()]
        lines += [f"Restart={RESTART.get(policy, 'on-failure')}", f"RestartSec={rsec}ms", "TimeoutStopSec=10"]
        if user:
            lines.append(f"User={user}")
        lines.append("NoNewPrivileges=yes")
        if self.scope == "system":
            lines += ["PrivateTmp=yes", "ProtectSystem=strict", f"ReadWritePaths={_q(data)}"]
        lines += ["", "[Install]", f"WantedBy={'multi-user.target' if self.scope == 'system' else 'default.target'}",
                  ""]
        return "\n".join(lines)

    def _prune(self, dep_id: str, keep: set[str]) -> None:
        d = self.dep_dir(dep_id)
        keep = {k.split(":")[-1][:16] for k in keep if k}
        for p in d.iterdir() if d.exists() else []:
            if p.is_dir() and p.name not in keep and p.name != "data":
                shutil.rmtree(p, ignore_errors=True)

    # ---- SPI --------------------------------------------------------------------------------------------------
    def plan(self, current, desired):
        steps = []
        if desired:
            steps.append(f"unpack {desired.module} {desired.version} -> native/{sanitize(desired.dep_id)}/")
            steps.append(f"write {self.unit_dir / self.unit(desired.dep_id)} + daemon-reload + enable")
        if current:
            steps.append(f"stop {self.unit(current.dep_id)} ({current.version})")
        if desired:
            pol = (desired.restart or {}).get("policy") or "on-failure"
            steps.append(f"start {self.unit(desired.dep_id)} (Restart={RESTART.get(pol, 'on-failure')})")
        return steps

    def apply(self, current, desired):
        unit, path = self.unit(desired.dep_id), self.unit_path(desired.dep_id)
        old = self._meta(desired.dep_id)
        props = self._show(unit)                         # executor reachable? (DriverError otherwise)
        workdir = self._unpack(desired)
        data = self.dep_dir(desired.dep_id) / "data"
        data.mkdir(parents=True, exist_ok=True)
        user = self._unit_user()
        if user and os.geteuid() == 0:
            try:
                shutil.chown(data, user)
            except (LookupError, OSError) as e:
                raise DriverError(f"cannot hand the data directory to user {user}: {e}") from None
        port = int(old["port"]) if old and str(old.get("port", "")).isdigit() else _free_port()
        text = self.render(desired, workdir, port)
        keep = {desired.digest, current.digest if current else "", old.get("digest", "") if old else ""}
        if old and old["text"] == text and props.get("ActiveState") == "active" and props.get("LoadState") == "loaded":
            self._prune(desired.dep_id, keep)            # idempotent: the same unit already runs
            return
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, text.encode("utf-8"), 0o644)
        self._ctl("daemon-reload")
        self._ctl("enable", unit if self._standard_dir() else str(path))
        self._ctl("reset-failed", unit, check=False)   # also resets the start rate limit
        self._ctl("restart", unit)
        st = {}

        def settled():
            st.update(self._show(unit))
            return st.get("ActiveState") == "failed" or (st.get("ActiveState") == "active"
                                                         and st.get("MainPID", "0") not in ("", "0"))
        if not wait_until(settled, 10, 0.1) or st.get("ActiveState") != "active":
            raise DriverError(f"unit {unit} did not start ({st.get('ActiveState')}/{st.get('SubState')}, "
                              f"result {st.get('Result')})")
        atomic_write_json(self._state_path(desired.dep_id), {"restarts": int(st.get("NRestarts") or 0)})
        self._prune(desired.dep_id, keep)

    def remove(self, dep_id):
        unit, path = self.unit(dep_id), self.unit_path(dep_id)
        if path.exists() or path.is_symlink():
            p = self._ctl("stop", unit, check=False)
            if p.returncode not in (0, 5):               # 5: unit not loaded (already gone)
                raise DriverError(f"systemctl stop {unit}: {_line(p.stderr)} (exit {p.returncode})")
            self._ctl("disable", unit, check=False)
            path.unlink(missing_ok=True)
            self._ctl("daemon-reload")
            self._ctl("reset-failed", unit, check=False)
        shutil.rmtree(self.dep_dir(dep_id), ignore_errors=True)

    def status(self, dep_id):
        meta = self._meta(dep_id)
        if not meta:
            return Observed(present=False)
        dg, ver = meta.get("digest"), meta.get("version")
        try:
            pr = self._show(self.unit(dep_id))
        except DriverError as e:
            return Observed(present=True, running=False, digest=dg, version=ver, detail=f"executor unreachable: {e}")
        a, sub, res = pr.get("ActiveState", ""), pr.get("SubState", ""), pr.get("Result", "")
        detail = f"{a}/{sub}" + (f" ({res})" if res and res != "success" else "")
        if pr.get("LoadState") not in (None, "loaded"):
            detail += f", unit {pr.get('LoadState')}"
        try:
            n = int(pr.get("NRestarts") or 0)
        except ValueError:
            n = 0
        return Observed(present=True, running=a == "active", digest=dg, version=ver, restarts=n, detail=detail)

    def list(self):
        try:
            return {dep: self.status(dep) for dep in self._units()}
        except Exception:  # noqa: BLE001 -- SPI: never raises
            return {}

    def health(self, dep_id, probe):
        meta = self._meta(dep_id)
        if not meta or not str(meta.get("port", "")).isdigit():
            return False
        try:
            if self._show(self.unit(dep_id)).get("ActiveState") != "active":
                return False
        except DriverError:
            return False
        return http_probe("127.0.0.1", int(meta["port"]), (probe or {}).get("path", "/healthz"))

    def failed(self, dep_id):
        """Terminal failure: systemd will not start the unit again (start limit hit, Restart=no, clean exit)."""
        meta = self._meta(dep_id)
        if not meta:
            return None
        unit = self.unit(dep_id)
        try:
            pr = self._show(unit)
        except DriverError:
            return None
        a, res, code = pr.get("ActiveState"), pr.get("Result", ""), pr.get("ExecMainStatus", "?")
        if a == "failed":
            if res == "start-limit-hit":
                return f"unit {unit} failed: start limit hit after {pr.get('NRestarts', '?')} restarts"
            return f"unit {unit} failed ({res}, exit status {code}) and will not be restarted"
        if a == "inactive" and pr.get("ExecMainExitTimestampMonotonic", "0") not in ("", "0"):
            return f"unit {unit} exited (status {code}) and will not be restarted"
        return None

    def supervise(self):
        """systemd restarts the unit itself; this reports what it did (NRestarts deltas, start limit, exit)."""
        events = []
        try:
            for dep_id in self._units():
                try:
                    pr = self._show(self.unit(dep_id))
                except DriverError:
                    return events                        # executor unreachable: nothing to report this tick
                sp = self._state_path(dep_id)
                st = read_json(sp, {}) or {}
                n = int(pr.get("NRestarts") or 0)
                new = dict(st)
                if n > int(st.get("restarts", 0)):
                    events.append({"dep": dep_id, "event": "restarted", "restarts": n})
                new["restarts"] = n
                if pr.get("ActiveState") == "failed":
                    if pr.get("Result") == "start-limit-hit":
                        if not st.get("limitReported"):
                            events.append({"dep": dep_id, "event": "start-limit-hit", "restarts": n})
                        new["limitReported"] = True
                    elif not st.get("exitReported"):
                        events.append({"dep": dep_id, "event": "exited", "result": pr.get("Result"),
                                       "status": pr.get("ExecMainStatus")})
                        new["exitReported"] = True
                elif pr.get("ActiveState") == "active":
                    new.pop("limitReported", None)
                    new.pop("exitReported", None)
                if new != st:
                    atomic_write_json(sp, new)
        except Exception:  # noqa: BLE001 -- SPI: never raises
            pass
        return events

    def preflight(self, release, dep):
        """Payload shape (before anything reaches systemd): a tar.gz with app.py or a valid unit.json, no member
        outside the payload tree, a supported restart policy."""
        path = getattr(release, "payload_path", None)
        try:
            with tarfile.open(path, "r:gz") as tf:
                members = tf.getmembers()
                names = {m.name.removeprefix("./") for m in members}
                for m in members:
                    parts = Path(m.name).parts
                    if m.name.startswith("/") or ".." in parts:
                        return f"native payload member {m.name!r} escapes the payload directory"
                uj = None
                if "unit.json" in names:
                    f = tf.extractfile(next(m for m in members if m.name.removeprefix("./") == "unit.json"))
                    uj = f.read() if f else b""
        except (tarfile.TarError, OSError, TypeError) as e:
            return f"native payload is not a tar.gz archive: {e}"
        try:
            exec_argv(names, uj)
        except DriverError as e:
            return str(e)
        pol = ((getattr(release, "descriptor", None) or {}).get("restartPolicy") or {}).get("policy")
        if pol is not None and pol not in RESTART:
            return f"restartPolicy.policy {pol!r} is not supported by the systemd driver (on-failure, always, never)"
        return None

    def capabilities(self):
        out = {"profile": self.profile, "driver": self.name, "executor": f"systemd ({self.scope} manager)",
               "python": sys.version.split()[0], "scope": self.scope, "unitDir": str(self.unit_dir)}
        try:
            if not (shutil.which(self.systemctl) or os.path.isfile(self.systemctl)):
                out.update(available=False, detail=f"{self.systemctl} not found")
                return out
            p = self._ctl("show", "-p", "Version", "--value", timeout=5)
            v = p.stdout.strip()
            if v:
                out["systemd"] = v
        except DriverError as e:
            out.update(reachable=False, detail=str(e))
        except Exception as e:  # noqa: BLE001 -- SPI: never raises
            out.update(reachable=False, detail=f"{type(e).__name__}: {e}")
        return out

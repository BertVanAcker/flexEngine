"""flexEngine driver `applab` (probe P4): runs P2 release artefacts that carry an Arduino App Lab release
archive (.ard) on an Arduino UNO Q / VENTUNO Q by calling `arduino-app-cli` (CLI first; its REST API is
OpenAPI 0.1.0 and pre-1.0, see verification-log V40).

SPI mapping (flexp2/engine/drivers/base.py):
  plan     dry run: install / stop previous / start / handshake
  apply    pre-install checks (board, contract hashes in the archive = descriptor), one-app-per-board check,
           `app install <rel>.ard --yes` (skipped if installed), parameters -> <release>/data/flex_params.json,
           `app stop <previous>`, `app start release:<rel>`
  remove   `app stop` + `app destroy` of every release this deployment installed
  status   `app ps --all` / `app list --releases` (state rebuilt from the executor + a small unit table)
  health   app status == running AND the FLEX01 handshake is complete on both sides: flex.status.mcu and
           flex.status.mpu, asked through the router socket, report the descriptor's contract hash and
           verified/enabled (fallback: "FLEX_CONTRACT_OK <hash>" in `app logs`)
  supervise restart a deployment whose app went to `failed` (Restart=on-failure, bounded)
Rollback is the engine's: on a failed health gate it calls apply(failed, previous), which re-starts the
previous release; it is still installed because the driver keeps the last two releases per deployment.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from flexengine2.util import atomic_write_json, read_json
from flexengine2.drivers.base import Driver, DriverError, Observed, Workload

from .ard import ReleaseError, contract_of, read_ard
from .rpc import RawClient, RpcError


class ApplabDriver(Driver):
    profile = "applab"
    name = "arduino-app-cli"
    runtime = "applab"
    exclusive = True           # one app per board (arduino-app-cli StartApp); flexCloud refuses a 2nd at plan time
    max_deployments = 1

    def __init__(self, device_id: str, data_dir: Path, cfg: dict | None = None):
        cfg = cfg or {}
        self.device = device_id
        self.cli = cfg.get("cli", "arduino-app-cli")
        self.board = cfg.get("board", "unoq")
        self.router = cfg.get("routerSocket", "/var/run/arduino-router.sock")
        self.health_via = cfg.get("healthVia", "router")         # router | logs
        self.takeover = bool(cfg.get("takeoverForeignApps", False))
        self.keep = int(cfg.get("keepReleases", 2))
        self.cli_timeout = float(cfg.get("cliTimeout", 600))
        # remote mode: the engine runs elsewhere (e.g. the LattePanda) and drives the board over SSH
        self.ssh = cfg.get("ssh")                                   # "arduino@unoq.local"
        self.ssh_cmd = list(cfg.get("sshCmd", ["ssh", "-o", "BatchMode=yes"]))
        self.scp_cmd = list(cfg.get("scpCmd", ["scp", "-q", "-o", "BatchMode=yes"]))
        self.staging = cfg.get("remoteStaging", "/tmp/flex-applab")
        if self.ssh and "healthVia" not in cfg:
            self.health_via = "logs"  # the router socket is board-local (or forward it: ssh -L sock:sock)
        self.root = Path(data_dir) / "applab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.units_path = self.root / "units.json"
        self.units: dict = read_json(self.units_path, {})
        self.calls_path = self.root / "cli-calls.jsonl"

    # ---- arduino-app-cli -----------------------------------------------------------------------
    def _cli(self, *args: str, check: bool = True, parse: bool = True):
        cmd = [self.cli, *args, "--format", "json"]
        if self.ssh:
            cmd = [*self.ssh_cmd, self.ssh, shlex.join(cmd)]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cli_timeout)
        except subprocess.TimeoutExpired as e:
            raise DriverError(f"arduino-app-cli {' '.join(args[:2])}: no answer within {self.cli_timeout} s") from e
        except OSError as e:   # FileNotFoundError: the CLI (or ssh) is not installed
            raise DriverError(f"arduino-app-cli {' '.join(args[:2])}: cannot run {cmd[0]} ({e})") from e
        with open(self.calls_path, "a") as f:
            f.write(json.dumps({"t": t0, "dt": round(time.time() - t0, 3), "args": list(args), "rc": r.returncode})
                    + "\n")
        if r.returncode != 0:
            try:
                msg = json.loads(r.stderr.strip().splitlines()[-1])["error"]
            except (ValueError, KeyError, IndexError):
                msg = (r.stderr or r.stdout).strip()[-400:]
            if check:
                raise DriverError(f"arduino-app-cli {' '.join(args[:2])}: {msg} (exit {r.returncode})")
            return {"error": msg, "rc": r.returncode}
        if not parse:
            return r.stdout
        return json.loads(r.stdout) if r.stdout.strip() else {}

    def _run(self, cmd: list[str], stdin: str | None = None):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, input=stdin, timeout=self.cli_timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DriverError(f"{cmd[0]}: {e}") from e
        if r.returncode != 0:
            raise DriverError(f"{cmd[0]}: {(r.stderr or r.stdout).strip()[-300:]} (exit {r.returncode})")
        return r.stdout

    def _remote(self, script: str, stdin: str | None = None):
        return self._run([*self.ssh_cmd, self.ssh, script], stdin)

    def _apps(self, ps: bool) -> dict[str, dict]:
        res = self._cli("app", "ps", "--all") if ps else self._cli("app", "list", "--releases")
        return {a.get("release_id") or a["id"]: a for a in res.get("apps", [])}

    def _save(self):
        atomic_write_json(self.units_path, self.units)

    # ---- helpers ------------------------------------------------------------------------------
    def _inspect(self, wl: Workload) -> tuple[Path, dict]:
        """Copy the verified payload to <releaseName>.ard and run the static pre-install checks."""
        rel = wl.payload_annotations.get("dev.flex.releaseName")
        if not rel:
            raise DriverError("payload is not an App Lab release (no dev.flex.releaseName)")
        cache = self.root / "ard"
        cache.mkdir(exist_ok=True)
        ard = cache / f"{rel}.ard"
        if not ard.exists():
            shutil.copyfile(wl.payload_path, ard)
        try:
            info = read_ard(ard)
        except ReleaseError as e:
            raise DriverError(f"bad release archive: {e}")
        if info["releaseName"] != rel:
            raise DriverError(f"archive root {info['releaseName']} != annotation {rel}")
        if info["manifest"]["target"] != self.board:
            raise DriverError(f"release built for {info['manifest']['target']}, this board is a {self.board}")
        want = wl.payload_annotations.get("dev.flex.contractHash")
        got = contract_of(info)
        if want and (got.get("python") != want or got.get("sketch") != want):
            raise DriverError(f"contract hash mismatch in the archive sources: descriptor {want}, found {got}")
        return ard, info

    def _foreign_running(self, own: set[str]) -> list[dict]:
        return [a for k, a in self._apps(ps=True).items()
                if a.get("status") in ("running", "starting") and k not in own]

    # ---- SPI --------------------------------------------------------------------------------
    def plan(self, current, desired):
        steps = []
        if desired:
            rel = desired.payload_annotations.get("dev.flex.releaseName")
            steps.append(f"check {rel}.ard: target {self.board}, contract {desired.payload_annotations.get('dev.flex.contractHash', '')[:18]}…")
            steps.append(f"arduino-app-cli app install {rel}.ard --yes")
        if current:
            steps.append(f"arduino-app-cli app stop release:{current.payload_annotations.get('dev.flex.releaseName')}")
        if desired:
            steps.append(f"arduino-app-cli app start release:{desired.payload_annotations.get('dev.flex.releaseName')}")
            steps.append("health: app running + FLEX01 handshake (flex.status.mcu / flex.status.mpu)")
        return steps

    def apply(self, current, desired):
        unit = self.units.get(desired.dep_id) or {"installed": []}
        rel = desired.payload_annotations.get("dev.flex.releaseName") or "?"
        # Point the unit at the desired release FIRST: if a check below refuses it, health() sees "not running"
        # and the engine rolls back. An apply that failed must never pass the health gate on the old app.
        unit.update(release=rel, digest=desired.digest, version=desired.version, wantRunning=True,
                    contractHash=desired.payload_annotations.get("dev.flex.contractHash"),
                    restarts=0, startedAt=time.time(), startLimitHit=False)
        self.units[desired.dep_id] = unit
        self._save()
        ard, info = self._inspect(desired)
        others = {u["release"] for d, u in self.units.items() if d != desired.dep_id and u.get("wantRunning")}
        if others:
            raise DriverError(f"one app per board: {sorted(others)} already deployed on this board")
        foreign = self._foreign_running(set(unit["installed"]) | {rel})
        if foreign:
            if not self.takeover:
                raise DriverError("one app per board: foreign app running: "
                                  + ", ".join(f"{a['name']} ({a.get('status')})" for a in foreign))
            for a in foreign:
                self._cli("app", "stop", _alias(a["id"]))
        if rel not in self._apps(ps=False):
            target = str(ard)
            if self.ssh:  # copy the archive to the board first
                self._remote(f"mkdir -p {shlex.quote(self.staging)}")
                self._run([*self.scp_cmd, str(ard), f"{self.ssh}:{self.staging}/"])
                target = f"{self.staging}/{ard.name}"
            res = self._cli("app", "install", target, "--yes")
            unit["paths"] = {**unit.get("paths", {}), rel: res.get("path")}
        if rel not in unit["installed"]:
            unit["installed"].append(rel)
        path = (unit.get("paths") or {}).get(rel)
        if path and desired.parameters:
            params = _split_params(desired.parameters)
            if self.ssh:
                d = shlex.quote(f"{path}/data")
                self._remote(f"mkdir -p {d} && cat > {d}/flex_params.json", json.dumps(params))
            else:
                data = Path(path) / "data"
                data.mkdir(exist_ok=True)
                atomic_write_json(data / "flex_params.json", params)
        self._save()
        ps = self._apps(ps=True)
        keep = {rel, current.payload_annotations.get("dev.flex.releaseName") if current else None}
        if ps.get(rel, {}).get("status") == "running":
            # e.g. a rollback to the app that a refused update never stopped: adopt, do not restart
            self._gc(desired.dep_id, keep)
            return
        for r in list(unit["installed"]):  # stop what this deployment runs now (or a half-applied attempt)
            if r != rel and ps.get(r, {}).get("status") in ("running", "starting", "failed"):
                self._cli("app", "stop", f"release:{r}")
        self._cli("app", "start", f"release:{rel}")
        self._gc(desired.dep_id, keep)

    def _gc(self, dep_id: str, keep: set):
        unit = self.units[dep_id]
        extra = [r for r in unit["installed"] if r not in keep]
        while len(unit["installed"]) > self.keep and extra:
            r = extra.pop(0)
            self._cli("app", "destroy", f"release:{r}", check=False)
            unit["installed"].remove(r)
        self._save()

    def remove(self, dep_id):
        unit = self.units.get(dep_id)
        if not unit:
            return
        unit["wantRunning"] = False
        self._save()
        for r in list(unit.get("installed", [])):
            self._cli("app", "destroy", f"release:{r}", check=False)
        del self.units[dep_id]
        self._save()

    def status(self, dep_id):
        unit = self.units.get(dep_id)
        if not unit or not unit.get("release"):
            return Observed(present=False)
        try:
            ps = self._apps(ps=True)
        except DriverError as e:
            return Observed(present=True, running=False, digest=unit["digest"], version=unit["version"], detail=str(e))
        a = ps.get(unit["release"])
        if a is None:
            return Observed(present=False, detail="release not installed")
        return Observed(present=True, running=a.get("status") == "running", digest=unit["digest"],
                        version=unit["version"], restarts=unit.get("restarts", 0), detail=a.get("status", "?"))

    def list(self):
        return {k: self.status(k) for k in self.units}

    def handshake_state(self, dep_id: str) -> dict:
        unit = self.units.get(dep_id) or {}
        want = unit.get("contractHash")
        out = {"want": want}
        try:
            with RawClient(f"unix://{self.router}", timeout=2) as c:
                out["mcu"] = c.call("flex.status.mcu", timeout=2)
                out["mpu"] = c.call("flex.status.mpu", timeout=2)
        except (OSError, RpcError, TimeoutError, ConnectionError) as e:
            out["error"] = str(e)
        return out

    def health(self, dep_id, probe):
        unit = self.units.get(dep_id)
        if not unit:
            return False
        obs = self.status(dep_id)
        if not obs.running:
            return False
        want = unit.get("contractHash")
        if self.health_via == "logs":
            logs = self._cli("app", "logs", f"release:{unit['release']}", "--tail", "200", parse=False, check=False)
            return isinstance(logs, str) and f"FLEX_CONTRACT_OK {want}" in logs
        hs = self.handshake_state(dep_id)
        mcu, mpu = hs.get("mcu"), hs.get("mpu")
        return bool(mcu and mpu and mcu[0] == want and mcu[1] and mcu[2] and mpu[0] == want and mpu[1])

    def failed(self, dep_id):
        """Fail fast (P4 amendment): the deployment's app is in `failed` (e.g. the Python side exited 3 on a
        FLEX01 handshake mismatch). arduino-app-cli does not restart it by itself, so waiting for the health
        timeout only delays the rollback."""
        unit = self.units.get(dep_id)
        if not unit or not unit.get("release"):
            return None
        obs = self.status(dep_id)
        if obs.detail == "failed":
            return f"app release:{unit['release']} failed"
        if not obs.present:
            return f"app release:{unit['release']} not installed"
        return None

    def supervise(self):
        events = []
        for dep_id, u in self.units.items():
            if not u.get("wantRunning"):
                continue
            obs = self.status(dep_id)
            if obs.detail != "failed" or time.time() - u.get("startedAt", 0) < 5:
                continue
            if u.get("restarts", 0) >= 3:
                if not u.get("startLimitHit"):
                    u["startLimitHit"] = True
                    events.append({"dep": dep_id, "event": "start-limit-hit"})
                continue
            u["restarts"] = u.get("restarts", 0) + 1
            u["startedAt"] = time.time()
            self._cli("app", "start", f"release:{u['release']}", check=False)
            events.append({"dep": dep_id, "event": "restarted", "restarts": u["restarts"]})
        if events:
            self._save()
        return events

    def capabilities(self):
        """Never raises: a CLI that cannot be run (missing, hung, failing) is reported as `available: false`."""
        avail = {}
        try:
            ver = self._cli("version")
        except (DriverError, ValueError) as e:
            ver = {"error": str(e)[:300]}
            avail = {"available": False, "detail": str(e)[:300]}
        return {"profile": self.profile, "driver": self.name, "board": self.board, "cli": ver,
                "maxRunningApps": 1, **self.placement(), **avail,
                "health": f"app running + FLEX01 handshake via {self.health_via}"}


def _alias(encoded: str) -> str:
    import base64
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


def _split_params(params: dict) -> dict:
    """Deployment parameters "<instance>.<name>": v -> {instance: {name: v}}; bare names go to "*"."""
    out: dict = {}
    for k, v in params.items():
        inst, _, name = k.partition(".")
        if name:
            out.setdefault(inst, {})[name] = v
        else:
            out.setdefault("*", {})[k] = v
    return out

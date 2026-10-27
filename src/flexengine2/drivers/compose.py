"""Container runtime `compose` (FE-4, x-flex-runtime `docker-compose`): a compose project per deployment.

Payload (CP-3, layer `application/vnd.flex.payload.compose.v1.tar+gzip`, dev.flex.profile container): a tar.gz with a
compose file at its root (compose.yaml, compose.yml, docker-compose.yaml or docker-compose.yml) and, optionally, the
images it runs as docker archives under `images/` (`docker save` / `podman save`). The driver loads those images and
runs the project with `up --pull never --no-build`, so only images that came with the signed payload (or an on-device
build of it) run: nothing is pulled from a registry the release signature does not cover.

    project   flex_<device>_<deployment> (lower case, [a-z0-9_-]; a hash is added when the id had to change)
    tree      <dataDir>/compose/<deployment>/<tree_key>/ (unpacked once, atomically; current + previous kept, so a
              roll back needs no second transfer)
    CLI       `<compose> -p <project> --project-directory <tree> -f <tree>/<file> <cmd>` where <compose> is
              `docker compose` (default) or `podman compose` (drivers.container.composeCommand); the deployment's
              parameters reach the compose file as ${FLEX_PARAM_<NAME>} (the CLI's environment)

SPI mapping: apply = `up -d --remove-orphans --pull never --no-build` (compose recreates what changed and removes
services the new file dropped); stage = `create` + `stop` (FE-4: installed, nothing runs); start / stop = `start` /
`stop`; remove = `down --volumes --remove-orphans` (named volumes of the project go with the deployment, as with the
CLI driver). status = `ps -a --format json` (JSON lines, or a JSON array from compose < 2.21): present when the project
has containers; running when one runs and none is dead, restarting, or exited with a non-zero code. health (the gate
per service) = running and every service container with a healthcheck reports healthy (the descriptor's probe is not
used: compose services declare their own healthcheck). failed = the first service whose container is dead or exited
for good (the CLI driver's rule, per container). A rollback re-applies the previous tree: the project goes back as
one unit. build (x-flex-build) = `compose build` of the source tree; the payload is that tree, the result digest
covers every image the project then names (`config --images`).

Policy (preflight): the compose file comes from a signed release, but deny by default still holds for host access:
privileged, cap_add, devices, security_opt, pid / ipc / network_mode / userns_mode host and bind mounts of host paths
are refused unless the device allows them (drivers.container.composeAllowHostAccess); a release's runOptions are
refused (the compose file says how its services run).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
from pathlib import Path

import yaml

from flexengine2.drivers.base import BuildRequest, BuildResult, DriverError, Observed, Workload, tree_key
from flexengine2.drivers.container import CliContainerDriver, _run, archive_image_ids
from flexengine2.util import atomic_write_bytes, atomic_write_json, fsync_dir, fsync_tree, read_json

__all__ = ["ComposeDriver", "MT_COMPOSE", "COMPOSE_FILES", "compose_policy", "project_name"]

MT_COMPOSE = "application/vnd.flex.payload.compose.v1.tar+gzip"
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_HOST_KEYS = ("privileged", "cap_add", "devices", "security_opt")
_HOST_MODES = ("pid", "ipc", "network_mode", "userns_mode", "uts")


def project_name(device: str, dep_id: str) -> str:
    """Compose project of a deployment: lower case letters, digits, '_' and '-', starting with a letter."""
    raw = f"flex_{device}_{dep_id}"
    name = re.sub(r"[^a-z0-9_-]", "-", raw.lower())
    if name != raw:
        name += "-" + hashlib.sha256(raw.encode()).hexdigest()[:8]
    return name


def compose_file(tree: Path) -> Path | None:
    return next((tree / n for n in COMPOSE_FILES if (tree / n).is_file()), None)


def compose_policy(doc, allow_host_access: bool = False) -> str | None:
    """Refusal reason for a compose document that asks for host access the device does not allow, else None."""
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        return "compose file has no services"
    if allow_host_access:
        return None
    for name, svc in sorted(doc["services"].items()):
        if not isinstance(svc, dict):
            return f"compose service {name} is not a mapping"
        for k in _HOST_KEYS:
            if svc.get(k):
                return f"compose service {name} asks for {k}, which this device does not allow"
        for k in _HOST_MODES:
            if svc.get(k) == "host":
                return f"compose service {name} asks for {k}: host, which this device does not allow"
        for v in svc.get("volumes") or []:
            src, typ = None, None
            if isinstance(v, str):
                src = v.split(":", 1)[0] if ":" in v else None
            elif isinstance(v, dict):
                typ, src = v.get("type"), v.get("source")
            if typ == "bind" or (isinstance(src, str) and src[:1] in ("/", ".", "~", "$")):
                return f"compose service {name} mounts a host path, which this device does not allow"
    return None


def _ps_rows(out: str) -> list[dict]:
    """`compose ps --format json`: one JSON object per line (compose >= 2.21) or one JSON array (older)."""
    s = out.strip()
    if not s:
        return []
    if s.startswith("["):
        rows = json.loads(s)
        return [r for r in rows if isinstance(r, dict)]
    return [json.loads(ln) for ln in s.splitlines() if ln.strip()]


class ComposeDriver(CliContainerDriver):
    profile = "container"
    name = "compose"
    runtime = "docker-compose"
    media_types = (MT_COMPOSE,)

    def __init__(self, device_id: str, data_dir: Path, cli: str = "docker", compose: list[str] | None = None,
                 allow_host_access: bool = False):
        super().__init__(device_id, runtime=cli)
        self.name, self.runtime = "compose", "docker-compose"
        self.compose = list(compose or [cli, "compose"])
        self.allow_host_access = bool(allow_host_access)
        self.root = Path(data_dir).absolute() / "compose"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"

    # ---- state, files ---------------------------------------------------------------------------------------
    def _state(self) -> dict:
        return read_json(self.state_path, {}) or {}

    def _save(self, st: dict) -> None:
        atomic_write_json(self.state_path, st)

    def project(self, dep_id: str) -> str:
        return project_name(self.device, dep_id)

    def dep_dir(self, dep_id: str) -> Path:
        return self.root / re.sub(r"[^A-Za-z0-9_.-]", "_", dep_id)

    def _unpack(self, wl: Workload) -> Path:
        d = self.dep_dir(wl.dep_id) / tree_key(wl)
        if (d / ".complete").exists():
            return d
        part = d.with_name(d.name + ".partial")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True)
        try:
            with tarfile.open(wl.payload_path, "r:*") as tf:
                tf.extractall(part, filter="data")
        except (tarfile.TarError, OSError) as e:
            shutil.rmtree(part, ignore_errors=True)
            raise DriverError(f"compose payload unusable: {e}") from None
        fsync_tree(part)
        atomic_write_bytes(part / ".complete", wl.digest.encode())
        shutil.rmtree(d, ignore_errors=True)
        os.replace(part, d)
        fsync_dir(d.parent)
        return d

    def _cli_env(self, wl: Workload) -> dict:
        """The compose CLI's environment: the deployment's parameters for ${FLEX_PARAM_<NAME>} in the file."""
        env = {"COMPOSE_STATUS_STDOUT": "1"}
        for k, v in sorted((wl.parameters or {}).items()):
            env[f"FLEX_PARAM_{k.upper()}"] = str(v)
        return env

    def _cmd(self, ent: dict, *args: str) -> list[str]:
        return [*self.compose, "-p", ent["project"], "--project-directory", ent["dir"], "-f", ent["file"], *args]

    def _compose(self, ent: dict, *args: str, timeout: float = 300, check: bool = True):
        return _run(self._cmd(ent, *args), timeout=timeout, check=check, env=ent.get("env") or None)

    def _load_images(self, tree: Path) -> None:
        """`load` the payload's images/*.tar unless the runtime has them: by the archive's image ids, or (an archive
        that names none) by the tree's marker of a load done before. No second transfer for an apply of the same
        release or a roll back."""
        marker = tree / ".images-loaded"
        for tar in sorted((tree / "images").glob("*.tar")) if (tree / "images").is_dir() else []:
            ids = archive_image_ids(tar)
            if ids and all(_run([self.rt, "image", "inspect", i], check=False).returncode == 0 for i in ids):
                continue                                  # already in the store
            if not ids and marker.exists():
                continue
            _run([self.rt, "load", "-q", "-i", str(tar)], timeout=300)
        atomic_write_bytes(marker, b"")

    def _entry(self, wl: Workload, tree: Path) -> dict:
        f = compose_file(tree)
        if f is None:
            raise DriverError(f"compose payload has no compose file ({', '.join(COMPOSE_FILES)})")
        return {"project": self.project(wl.dep_id), "dir": str(tree), "file": str(f), "digest": wl.digest,
                "version": wl.version, "env": self._cli_env(wl)}

    # ---- SPI --------------------------------------------------------------------------------------------------
    def plan(self, current, desired):
        steps = []
        if desired:
            steps.append(f"unpack {desired.module} {desired.version} -> compose/{desired.dep_id}/, load its images")
            steps.append(f"compose -p {self.project(desired.dep_id)} up -d --remove-orphans ({desired.version})")
        elif current:
            steps.append(f"compose -p {self.project(current.dep_id)} down")
        return steps

    def _install(self, desired: Workload, run: bool) -> None:
        if desired.run_options:
            raise DriverError("runOptions are not supported by the compose driver (the compose file says how its "
                              "services run)")
        tree = self._unpack(desired)
        ent = self._entry(desired, tree)
        self._load_images(tree)
        st = self._state()
        st[desired.dep_id] = {**ent, "run": run}
        self._save(st)
        if run:
            self._compose(ent, "up", "-d", "--remove-orphans", "--pull", "never", "--no-build")
        else:
            self._compose(ent, "create", "--remove-orphans", "--pull", "never", "--no-build")
            self._compose(ent, "stop")
        self._prune(desired.dep_id, {tree.name})

    def _prune(self, dep_id: str, keep: set[str]) -> None:
        """Keep the trees of the current and the previous release (roll back without re-transfer)."""
        d = self.dep_dir(dep_id)
        trees = sorted((p for p in d.iterdir() if p.is_dir() and p.name not in keep and not p.name.endswith(".partial")),
                       key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        for p in trees[1:]:
            shutil.rmtree(p, ignore_errors=True)

    def apply(self, current, desired):
        self._install(desired, run=True)

    def stage(self, current, desired):
        ent = self._state().get(desired.dep_id)
        o = self.status(desired.dep_id)
        if ent and o.present and not o.running and o.digest == desired.digest and \
                Path(ent["dir"]).name == tree_key(desired):
            return
        self._install(desired, run=False)

    def _want(self, dep_id: str, run: bool) -> dict | None:
        st = self._state()
        ent = st.get(dep_id)
        if ent and ent.get("run", True) != run:
            st[dep_id] = {**ent, "run": run}
            self._save(st)
        return ent

    def start(self, dep_id):
        ent = self._want(dep_id, True)
        if not ent:
            raise DriverError(f"compose project {self.project(dep_id)} is not installed")
        self._compose(ent, "start")

    def stop(self, dep_id):
        ent = self._want(dep_id, False)
        if ent:
            self._compose(ent, "stop", "-t", "10")

    def remove(self, dep_id):
        st = self._state()
        ent = st.get(dep_id)
        if ent and Path(ent["file"]).exists():
            self._compose(ent, "down", "--volumes", "--remove-orphans", check=False)
        else:   # compose v2 finds a project's containers by its label, also without the file
            _run([*self.compose, "-p", self.project(dep_id), "down", "--volumes", "--remove-orphans"], check=False,
                 timeout=300)
        if dep_id in st:
            del st[dep_id]
            self._save(st)
        shutil.rmtree(self.dep_dir(dep_id), ignore_errors=True)

    def _obs_filters(self, dep_id: str) -> list[str]:
        """UM-4 observation: the containers of the deployment's project (compose labels them)."""
        return [f"com.docker.compose.project={self.project(dep_id)}"]

    def _rows(self, dep_id: str, ent: dict) -> list[dict]:
        """Containers of the project (DriverError when compose cannot answer)."""
        cmd = (self._cmd(ent, "ps", "-a", "--format", "json") if Path(ent["file"]).exists()
               else [*self.compose, "-p", ent["project"], "ps", "-a", "--format", "json"])
        p = _run(cmd, check=False, timeout=60, env=ent.get("env") or None)
        if p.returncode != 0:
            raise DriverError(f"{' '.join(cmd[:4])}...: rc={p.returncode} {(p.stderr or '').strip()[:300]}")
        try:
            return _ps_rows(p.stdout)
        except ValueError as e:
            raise DriverError(f"{' '.join(cmd[:4])}...: unreadable output ({e})") from None

    @staticmethod
    def _up(rows: list[dict]) -> bool:
        states = [str(r.get("State", "")).lower() for r in rows]
        bad = any(s in ("restarting", "dead") for s in states) or any(
            str(r.get("State", "")).lower() == "exited" and int(r.get("ExitCode") or 0) != 0 for r in rows)
        return "running" in states and not bad

    def status(self, dep_id):
        ent = self._state().get(dep_id)
        if not ent:
            return Observed(present=False)
        try:
            rows = self._rows(dep_id, ent)
        except DriverError as e:
            return self._unreachable(e)
        if not rows:
            return Observed(present=False)
        up = self._up(rows)
        detail = ", ".join(sorted(f"{r.get('Service', '?')}={str(r.get('State', '?')).lower()}" for r in rows))
        return Observed(present=True, running=up, digest=ent.get("digest"), version=ent.get("version"),
                        detail=detail if up else (detail or "stopped"))

    def list(self):
        try:
            return {k: self.status(k) for k in self._state()}
        except Exception:  # noqa: BLE001 -- SPI: never raises
            return {}

    def health(self, dep_id, probe):
        ent = self._state().get(dep_id)
        if not ent:
            return False
        try:
            rows = self._rows(dep_id, ent)
        except DriverError:
            return False
        if not rows or not self._up(rows):
            return False
        return all(str(r.get("Health") or "").lower() in ("", "healthy") for r in rows
                   if str(r.get("State", "")).lower() == "running")

    def failed(self, dep_id, _info=None):
        """The first service whose container is dead or exited for good (the CLI driver's rule per container)."""
        ent = self._state().get(dep_id)
        if not ent or not ent.get("run", True):
            return None                                   # staged / stopped on request: not a failure
        try:
            rows = self._rows(dep_id, ent)
            for r in sorted(rows, key=lambda r: str(r.get("Service", ""))):
                if str(r.get("State", "")).lower() not in ("exited", "dead"):
                    continue
                info = self._inspect(str(r.get("ID") or r.get("Name")))
                why = CliContainerDriver.failed(self, dep_id, _info=info) if info else None
                if why:
                    return f"service {r.get('Service', '?')}: {why}"
        except DriverError:
            return None
        return None

    def supervise(self):
        return []                                         # the runtime's restart policies (per service) do it

    def preflight(self, release, dep):
        desc = getattr(release, "descriptor", None) or {}
        if desc.get("runOptions"):
            return "runOptions are not supported by the compose driver (the compose file says how its services run)"
        ann = getattr(release, "payload_annotations", None) or {}
        want = ann.get("dev.flex.arch")
        if want:
            have = (self.platform() or {}).get("dev.flex.arch")   # DriverError: runtime unreachable -> deferred
            if have and want != have:
                return f"container image is built for {want} (dev.flex.arch), this device is {have}"
        path = getattr(release, "payload_path", None)
        try:
            with tarfile.open(path, "r:*") as tf:
                names = {m.name.removeprefix("./") for m in tf.getmembers() if m.isfile()}
                name = next((n for n in COMPOSE_FILES if n in names), None)
                if name is None:
                    return f"compose payload has no compose file ({', '.join(COMPOSE_FILES)})"
                member = next(m for m in tf.getmembers() if m.name.removeprefix("./") == name)
                doc = yaml.safe_load(tf.extractfile(member).read())
        except (tarfile.TarError, OSError, yaml.YAMLError, AttributeError, TypeError) as e:
            return f"compose payload unusable: {e}"
        return compose_policy(doc, self.allow_host_access)

    def build(self, desired, request: BuildRequest) -> BuildResult:
        """`compose build` of the source tree; the payload is the tree (images stay in the runtime's store under the
        project's names), the result digest covers every image the project names."""
        from flexengine2.drivers.buildstep import pack_tree, run_build, unpack_source

        work = Path(request.work_dir)
        src = unpack_source(request, work / "src")
        f = compose_file(src)
        if f is None:
            raise DriverError(f"compose payload has no compose file ({', '.join(COMPOSE_FILES)})")
        try:
            why = compose_policy(yaml.safe_load(f.read_text()), self.allow_host_access)
        except (OSError, yaml.YAMLError) as e:
            why = f"compose payload unusable: {e}"
        if why:
            raise DriverError(why)
        ent = {"project": self.project(desired.dep_id), "dir": str(src), "file": str(f), "env": self._cli_env(desired)}
        request.progress(0.05)
        run_build(self._cmd(ent, "build"), src, {**os.environ, **ent["env"]}, request, what="compose build")
        p = self._compose(ent, "config", "--images", timeout=60)
        images = sorted({ln.strip() for ln in p.stdout.splitlines() if ln.strip()})
        if not images:
            raise DriverError("compose build: the project names no image")
        ids = []
        for img in images:
            q = _run([self.rt, "image", "inspect", "--format", "{{.Id}}", img], check=False, timeout=60)
            if q.returncode != 0:
                raise DriverError(f"compose build: image {img} is missing after the build")
            ids.append(f"{img}@{q.stdout.strip()}")
        request.progress(0.95)
        payload = work / "payload.tar.gz"
        pack_tree(src, payload)
        digest = "sha256:" + hashlib.sha256("\n".join(ids).encode()).hexdigest()
        return BuildResult(digest=digest, payload_path=str(payload),
                           payload_annotations={"dev.flex.profile": "container"})

    def capabilities(self):
        out = super().capabilities()
        out["driver"] = "compose"
        out.pop("runOptions", None)
        out.pop("healthTypes", None)
        if out.get("available") is False or out.get("reachable") is False:
            return out
        try:
            p = _run([*self.compose, "version", "--short"], check=False, timeout=30)
        except DriverError as e:
            return {k: v for k, v in out.items() if k in ("profile", "driver", "supportedRuntimes")} | {
                "available": False, "detail": str(e)[:300]}
        if p.returncode != 0 or not p.stdout.strip():
            return {k: v for k, v in out.items() if k in ("profile", "driver", "supportedRuntimes")} | {
                "available": False, "detail": ((p.stderr or p.stdout).strip()[:300] or f"rc={p.returncode}")}
        out["runtimeVersion"] = f"{out.get('runtimeVersion', '?')} compose {p.stdout.strip().splitlines()[-1]}"
        return out

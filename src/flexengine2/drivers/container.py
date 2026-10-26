"""Container drivers: Docker and Podman (same CLI surface), and Ankaios (Podman underneath).

The verified payload is a docker-archive tar (from the signed release). The driver loads it into the
runtime's image store, then runs it with labels that let it rebuild its view after an engine crash:
dev.flex.device / dev.flex.deployment / dev.flex.digest / dev.flex.version.
Restart policy is delegated to the runtime (docker/podman `--restart on-failure:N`, Ankaios
`restartPolicy: ON_FAILURE`). Health probes are executed by the engine: `http` (default; GET on the container IP;
where the host cannot reach a container IP -- rootless podman (slirp4netns / pasta: no IP; netavark: an IP inside
the rootless network namespace) or a container without one -- GET on 127.0.0.1 inside the container's network
namespace: `[podman unshare] nsenter --net=/proc/<pid>/ns/net <python>`),
`exec` (EV-39: a command run in the container with `docker|podman exec`, exit 0 = healthy, bounded by
`execTimeoutSeconds`), `process` (EV-39: the container runs, is not restarting and did not restart since the
previous probe). Ankaios runs podman underneath,
so the same three probes go to the workload's podman container.

platform() (EV-40) = {dev.flex.arch} of the runtime (`docker version` Server.Arch, `podman info` Host.Arch,
normalised to `uname -m` names): a release carries one image layer per CPU, the store picks the matching one,
preflight refuses an image built for another CPU.

runOptions (signed descriptor, S4; each value must also be on the device's allowedRunOptions, deny by default):
devices / gpus / runtime / groupAdd (run flags), env (EV-41: {NAME: value}; secret-looking names or values are
refused), envFrom (EV-41: {NAME: {file: <name>}}: the value is read at apply from the device-local 0600 file
<drivers.container.secretsDir>/<name> and handed to the CLI through its environment, never its argv), ports
(EV-41: "[ip:]hostPort:containerPort[/tcp|udp|sctp]" -> `-p`), volumes (EV-41: "name:/abs/target[:ro]" -> a
runtime named volume `flex_<device>_<deployment>_<name>` labelled dev.flex.device / dev.flex.deployment /
dev.flex.volume; never a bind mount). Volumes survive updates and rollbacks (apply replaces the container only) and
are deleted with the deployment (remove).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NoReturn

from flexengine2.drivers.base import (BuildRequest, BuildResult, Driver, DriverError, Observed, Workload, http_probe,
                                      wait_until)
from flexengine2.identity.secrets import SecretPermissionError, looks_secret, read_secret_file


def archive_image_ids(path: str | os.PathLike | None) -> list[str]:
    """Image IDs (config digests, `sha256:<hex>`) named by the manifest.json of a docker-archive tar (the OCI layout
    `docker save` writes carries one as well); [] when the file has none or cannot be read."""
    import tarfile

    try:
        with tarfile.open(path) as tf:
            f = tf.extractfile("manifest.json")
            entries = json.load(f) if f is not None else []
    except (OSError, KeyError, ValueError, TypeError, tarfile.TarError):
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        cfg = e.get("Config") if isinstance(e, dict) else None
        if not isinstance(cfg, str):
            continue
        hexd = cfg.rsplit("/", 1)[-1].removesuffix(".json")      # blobs/sha256/<hex> or legacy <hex>.json
        if re.fullmatch(r"[0-9a-f]{64}", hexd):
            out.append(f"sha256:{hexd}")
    return out


def _run(cmd: list[str], timeout: float = 120, check: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a runtime CLI command. A missing binary or a hung CLI is an executor problem (DriverError) even with
    check=False; check=False only tolerates a non-zero exit. `env`: extra variables for the CLI process (secret
    values for `-e NAME`, so they never appear in an argv)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **env} if env else None)
    except FileNotFoundError as e:
        raise DriverError(f"{cmd[0]}: executable not found ({e.strerror or e})") from e
    except subprocess.TimeoutExpired as e:
        raise DriverError(f"{' '.join(cmd[:4])}...: no answer within {timeout:.0f} s") from e
    except OSError as e:
        raise DriverError(f"{cmd[0]}: cannot run ({e})") from e
    if check and p.returncode != 0:
        _fail(cmd, p)
    return p


def _fail(cmd: list[str], p: subprocess.CompletedProcess) -> NoReturn:
    raise DriverError(f"{' '.join(cmd[:4])}...: rc={p.returncode} {(p.stderr or '').strip()[:300]}")


def _json(cmd: list[str], out: str):
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as e:
        raise DriverError(f"{' '.join(cmd[:4])}...: unreadable output ({e})") from e


# what `docker|podman container inspect` says for a container that does not exist (anything else -- "Cannot
# connect to the Docker daemon", a broken podman store -- is the executor being unreachable, never absence)
_NO_SUCH = re.compile(r"no such (container|object)|no container with (name|id)", re.I)


def _ip(info: dict) -> str | None:
    ns = info.get("NetworkSettings") or {}
    if ns.get("IPAddress"):
        return ns["IPAddress"]
    for n in (ns.get("Networks") or {}).values():
        if n.get("IPAddress"):
            return n["IPAddress"]
    return None


# the `http` probe inside a container's network namespace (argv: port path timeout); stdlib only, run with -I
_NETNS_HTTP = """import http.client, sys
try:
    c = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=float(sys.argv[3]))
    c.request("GET", sys.argv[2], headers={"Connection": "close"})
    r = c.getresponse()
    r.read()
    sys.exit(0 if 200 <= r.status < 300 else 1)
except Exception:
    sys.exit(1)
"""


# Extra `docker/podman run` options a release may request in its signed descriptor (`runOptions`), e.g.
# GPU or device access on the lab edge boxes. Only options named in the engine config's allow-list
# (`drivers.container.allowedRunOptions`) are used; anything else fails the deployment (S4).
RUN_OPTION_FLAGS = {"devices": "--device", "gpus": "--gpus", "runtime": "--runtime", "groupAdd": "--group-add"}
RUN_OPTION_KEYS = sorted([*RUN_OPTION_FLAGS, "env", "envFrom", "ports", "volumes"])   # EV-41: env/ports/volumes
HEALTH_TYPES = ["http", "exec", "process"]                                           # EV-39

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PORT = re.compile(r"^(?:(?P<ip>[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:]+\]):)?(?P<host>[0-9]{1,5}):"
                   r"(?P<ctr>[0-9]{1,5})(?:/(?P<proto>tcp|udp|sctp))?$")
_PORT_ALLOW = re.compile(r"^(?:(?P<ip>[0-9]{1,3}(?:\.[0-9]{1,3}){3}|\[[0-9A-Fa-f:]+\]):)?(?P<lo>[0-9]{1,5})"
                         r"(?:-(?P<hi>[0-9]{1,5}))?(?:/(?P<proto>tcp|udp|sctp))?$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")          # volume and secret-file names: never a path
_VOLUME = re.compile(r"^(?P<name>[^:/]+):(?P<target>/[^:,]*)(?::(?P<mode>ro|rw))?$")

# uname -m names (oci-profile dev.flex.arch) for the Go architectures docker/podman report
GO_ARCH = {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7l", "386": "i686", "ppc64le": "ppc64le",
           "s390x": "s390x", "riscv64": "riscv64", "x86_64": "x86_64", "aarch64": "aarch64"}


@dataclass
class RunSpec:
    """runOptions resolved for one `run`: extra CLI arguments, variables for the CLI's own environment (secret
    values behind `-e NAME`), and the runtime named volumes to create first."""
    args: list = field(default_factory=list)
    cli_env: dict = field(default_factory=dict)
    volumes: list = field(default_factory=list)


def _values(v) -> list:
    return v if isinstance(v, list) else [v]


def _port_allowed(m: re.Match, allowed: list) -> bool:
    host, proto, ip = int(m["host"]), m["proto"] or "tcp", m["ip"]
    for a in allowed or []:
        am = _PORT_ALLOW.match(a) if isinstance(a, str) else None
        if not am or (am["proto"] or "tcp") != proto:
            continue
        lo = int(am["lo"])
        hi = int(am["hi"] or lo)
        if lo <= host <= hi and (am["ip"] is None or am["ip"] == ip):
            return True
    return False


def resolve_run_options(opts: dict, allowed: dict, *, secrets_dir: str | None = None,
                        volume_name=lambda n: n, read_secrets: bool = True) -> RunSpec:
    """Check `opts` (descriptor runOptions) against `allowed` (allowedRunOptions: lists of permitted values;
    env / volumes entries are fnmatch patterns of names, ports entries "[ip:]port[-port][/proto]") and build the
    run arguments. Raises DriverError on anything not allowed (deny by default)."""
    spec = RunSpec()
    allowed = allowed or {}
    for key, value in sorted((opts or {}).items()):
        flag = RUN_OPTION_FLAGS.get(key)
        if flag is not None:
            permitted = set(allowed.get(key) or [])
            for v in _values(value):
                if not isinstance(v, str) or v not in permitted:
                    raise DriverError(f"runOptions.{key}={v!r} is not in this device's allow-list")
                spec.args += [flag, v]
        elif key in ("env", "envFrom"):
            if not isinstance(value, dict):
                raise DriverError(f"runOptions.{key} must be a map of variable names")
            for name, v in sorted(value.items()):
                if not _ENV_NAME.match(str(name)) or str(name).upper().startswith("FLEX_"):
                    raise DriverError(f"runOptions.{key}.{name}: invalid variable name (FLEX_* is the engine's)")
                if not any(fnmatchcase(name, pat) for pat in allowed.get("env") or []):
                    raise DriverError(f"runOptions.{key}.{name} is not in this device's allow-list")
                if key == "env":
                    if not isinstance(v, str):
                        raise DriverError(f"runOptions.env.{name} must be a string")
                    if looks_secret(name, v):
                        raise DriverError(f"runOptions.env.{name} looks like a secret: use runOptions.envFrom with a "
                                          "device-local secret file")
                    spec.args += ["-e", f"{name}={v}"]
                    continue
                fname = v.get("file") if isinstance(v, dict) else None
                if not isinstance(fname, str) or not _NAME.match(fname):
                    raise DriverError(f"runOptions.envFrom.{name}: expected an object naming a secret file "
                                      "(file: <name>)")
                if not secrets_dir:
                    raise DriverError(f"runOptions.envFrom.{name}: this device has no drivers.container.secretsDir")
                try:
                    secret = read_secret_file(Path(secrets_dir) / fname)
                except (SecretPermissionError, OSError, ValueError) as e:
                    raise DriverError(f"runOptions.envFrom.{name}: secret file {fname!r} is not usable ({e})") from e
                spec.args += ["-e", name]
                if read_secrets:
                    spec.cli_env[name] = secret
        elif key == "ports":
            for v in _values(value):
                m = _PORT.match(v) if isinstance(v, str) else None
                if not m or not (0 < int(m["host"]) < 65536 and 0 < int(m["ctr"]) < 65536):
                    raise DriverError(f"runOptions.ports={v!r}: expected [ip:]hostPort:containerPort[/tcp|udp|sctp]")
                if not _port_allowed(m, allowed.get("ports")):
                    raise DriverError(f"runOptions.ports={v!r} is not in this device's allow-list")
                spec.args += ["-p", v]
        elif key == "volumes":
            for v in _values(value):
                m = _VOLUME.match(v) if isinstance(v, str) else None
                if not m or not _NAME.match(m["name"]):
                    raise DriverError(f"runOptions.volumes={v!r}: expected name:/absolute/target[:ro] (named volumes "
                                      "only, never a host path)")
                if not any(fnmatchcase(m["name"], pat) for pat in allowed.get("volumes") or []):
                    raise DriverError(f"runOptions.volumes={v!r} is not in this device's allow-list")
                vol = volume_name(m["name"])
                spec.volumes.append(vol)
                spec.args += ["-v", f"{vol}:{m['target']}" + (":ro" if m["mode"] == "ro" else "")]
        else:
            raise DriverError(f"runOptions.{key} is not supported (supported: {RUN_OPTION_KEYS})")
    return spec


def run_option_args(opts: dict, allowed: dict) -> list[str]:
    """`{"devices": ["/dev/dri"], "gpus": "all"}` -> ["--device", "/dev/dri", "--gpus", "all"], checked
    against `allowed` (same shape, lists of permitted values). Raises DriverError on anything else."""
    return resolve_run_options(opts, allowed, read_secrets=False).args


def exec_probe_ok(probe: dict | None) -> bool:
    """False for an `exec` health spec without a usable command (the driver cannot run it)."""
    if ((probe or {}).get("type") or "http") != "exec":
        return True
    cmd = probe.get("command")
    return isinstance(cmd, list) and bool(cmd) and all(isinstance(c, str) and c for c in cmd)


def build_tag(device: str, dep_id: str, build_id: str) -> str:
    """Image name of an on-device build (FE-4): lower case, [a-z0-9._-] only."""
    def clean(s: str) -> str:
        return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip(".-_") or "x"
    return f"localhost/flexbuild/{clean(device)}-{clean(dep_id)}:{clean(build_id)[:120]}"


def norm_arch(a: str) -> str:
    return GO_ARCH.get(a.strip().lower(), a.strip().lower())


class CliContainerDriver(Driver):
    profile = "container"
    name = "docker"   # class default; an instance refines it to its runtime (docker | podman)

    def __init__(self, device_id: str, runtime: str = "docker", allowed_run_options: dict | None = None,
                 secrets_dir: str | None = None):
        self.device = device_id
        self.rt = runtime
        self.name = runtime
        self.runtime = runtime
        self._ver = None
        self._arch = None
        self._restarts: dict[str, int] = {}      # container id -> RestartCount at the last `process` probe
        self._rootless_v: bool | None = None
        self.allowed_run_options = allowed_run_options or {}
        self.secrets_dir = secrets_dir

    def cname(self, dep_id: str) -> str:
        return f"flex_{self.device}_{dep_id}"

    def vname(self, dep_id: str, name: str) -> str:
        """Runtime named volume of one deployment (EV-41)."""
        return f"flex_{self.device}_{dep_id}_{name}"

    def run_spec(self, wl: Workload, read_secrets: bool = True) -> RunSpec:
        return resolve_run_options(wl.run_options, self.allowed_run_options, secrets_dir=self.secrets_dir,
                                   volume_name=lambda n: self.vname(wl.dep_id, n), read_secrets=read_secrets)

    # ---- images ----------------------------------------------------------------------------
    def ensure_image(self, wl: Workload) -> str:
        """Load the verified archive unless present; always run by a content-addressed reference.

        The release pins the image by the manifest digest of the saved archive (dev.flex.imageRef). A containerd
        image store (Docker >= 29 default) resolves that reference after `docker load`; the classic graph-driver
        stores (Docker before 29, Podman) keep no repo digest for a loaded image and know it by its image ID, the
        digest of its config (which names every layer's diff id). Both identify exactly the verified content, so the
        driver runs by whichever the runtime resolves: the manifest digest first, else the archive's config digest."""
        ref = wl.payload_annotations["dev.flex.imageRef"]  # docker.io/flexnode/<app>@sha256:...
        refs = [ref, *archive_image_ids(wl.payload_path)]

        def resolved() -> str | None:
            return next((r for r in refs if _run([self.rt, "image", "inspect", r], check=False).returncode == 0), None)

        have = resolved()
        if have is None:
            _run([self.rt, "load", "-q", "-i", wl.payload_path], timeout=300)
            have = resolved()
            if have is None:
                raise DriverError(f"payload did not contain image {ref}")
        return have

    def _labels(self, wl: Workload) -> list[str]:
        out = []
        for k, v in {"dev.flex.device": self.device, "dev.flex.deployment": wl.dep_id, "dev.flex.digest": wl.digest,
                     "dev.flex.version": wl.version, "dev.flex.module": wl.module}.items():
            out += ["--label", f"{k}={v}"]
        return out

    def _env(self, wl: Workload) -> list[str]:
        out = []
        for k, v in sorted(wl.parameters.items()):
            out += ["-e", f"FLEX_PARAM_{k.upper()}={v}"]
        return out

    def _restart(self, wl: Workload) -> str:
        pol = wl.restart.get("policy", "on-failure")
        if pol == "always":
            return "unless-stopped"
        if pol == "never":
            return "no"
        return f"on-failure:{int(wl.restart.get('maxRetries', 5))}"

    # ---- SPI --------------------------------------------------------------------------------
    def plan(self, current, desired):
        steps = []
        if desired:
            steps.append(f"{self.rt} load {desired.payload_annotations.get('dev.flex.imageTag')} (if missing)")
        if current:
            steps.append(f"{self.rt} stop+rm {self.cname(current.dep_id)} ({current.version})")
        if desired:
            steps.append(f"{self.rt} run {self.cname(desired.dep_id)} ({desired.version}, restart={self._restart(desired)})")
        return steps

    def apply(self, current, desired):
        self._create(desired, run=True)

    def _create(self, desired: Workload, run: bool):
        """`run -d` (apply) or `create` (FE-4 stage) of a fresh container for the deployment."""
        spec = self.run_spec(desired)  # validate (and read envFrom secrets) before touching anything
        image_id = self.ensure_image(desired)
        self._remove_container(desired.dep_id)
        for vol in spec.volumes:
            self._ensure_volume(desired.dep_id, vol)
        _run([self.rt, *(["run", "-d"] if run else ["create"]), "--name", self.cname(desired.dep_id),
              "--restart", self._restart(desired), *self._labels(desired), *self._env(desired), *spec.args, image_id],
             timeout=120, env=spec.cli_env)

    # FE-4 run state: a created (staged) or stopped container keeps its image, volumes and configuration; a container
    # stopped on request is not brought back by its restart policy (docker/podman semantics of `stop`)
    def stage(self, current, desired):
        try:
            info = self._inspect(self.cname(desired.dep_id))
        except DriverError:
            info = None
        o = self._observed(info)
        if o.present and o.digest == desired.digest and not o.running:
            return                                        # idempotent: already staged / stopped at this release
        self._create(desired, run=False)

    def start(self, dep_id):
        n = self.cname(dep_id)
        if self._inspect(n) is None:
            raise DriverError(f"container {n} is not installed")
        _run([self.rt, "start", n], timeout=120)

    def stop(self, dep_id):
        n = self.cname(dep_id)
        if self._inspect(n) is None:
            return
        _run([self.rt, "stop", "-t", "10", n], timeout=60)

    def build(self, desired, request: BuildRequest) -> BuildResult:
        """`docker|podman build` of the source tree (descriptor build.dockerfile, default Dockerfile); the image id
        is the result and the image the workload runs (no archive to load)."""
        from flexengine2.drivers.buildstep import run_build, unpack_source

        spec = request.spec or {}
        src = unpack_source(request, Path(request.work_dir) / "src")
        dockerfile = src / (spec.get("dockerfile") or "Dockerfile")
        if not dockerfile.is_file() or src.resolve() not in dockerfile.resolve().parents:
            raise DriverError(f"the source payload has no {spec.get('dockerfile') or 'Dockerfile'}")
        tag = build_tag(self.device, desired.dep_id, request.id)
        request.progress(0.05)
        run_build([self.rt, "build", "-t", tag, "-f", str(dockerfile), str(src)], src, dict(os.environ), request,
                  what=f"{self.rt} build")
        p = _run([self.rt, "image", "inspect", "--format", "{{.Id}}", tag], timeout=60)
        image_id = p.stdout.strip()
        image_id = image_id if image_id.startswith("sha256:") else f"sha256:{image_id}"
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise DriverError(f"{self.rt} build gave no image id for {tag}")
        return BuildResult(digest=image_id, payload_annotations={"dev.flex.profile": "container",
                                                                 "dev.flex.imageRef": image_id,
                                                                 "dev.flex.imageTag": tag})

    def _remove_container(self, dep_id):
        n = self.cname(dep_id)
        if _run([self.rt, "container", "inspect", n], check=False).returncode == 0:
            _run([self.rt, "stop", "-t", "3", n], check=False, timeout=30)
            _run([self.rt, "rm", "-f", n], check=False, timeout=30)

    def _ensure_volume(self, dep_id: str, vol: str):
        """Named volumes outlive the container (updates, rollbacks); created once, labelled with the deployment."""
        if _run([self.rt, "volume", "inspect", vol], check=False).returncode == 0:
            return
        _run([self.rt, "volume", "create", "--label", f"dev.flex.device={self.device}",
              "--label", f"dev.flex.deployment={dep_id}", "--label", f"dev.flex.volume={vol}", vol], timeout=60)

    def _volumes(self, dep_id: str) -> list[str]:
        p = _run([self.rt, "volume", "ls", "-q", "--filter", f"label=dev.flex.device={self.device}",
                  "--filter", f"label=dev.flex.deployment={dep_id}"], check=False, timeout=60)
        return p.stdout.split() if p.returncode == 0 else []

    def remove(self, dep_id):
        """Removing the deployment also deletes its named volumes (EV-41); an update or rollback keeps them."""
        self._remove_container(dep_id)
        for vol in self._volumes(dep_id):
            _run([self.rt, "volume", "rm", "-f", vol], check=False, timeout=60)

    def preflight(self, release, dep):
        ann = getattr(release, "payload_annotations", None) or {}
        want = ann.get("dev.flex.arch")
        if want:
            have = (self.platform() or {}).get("dev.flex.arch")   # DriverError: runtime unreachable -> deferred
            if have and want != have:
                return f"container image is built for {want} (dev.flex.arch), this device is {have}"
        desc = getattr(release, "descriptor", None) or {}
        if not exec_probe_ok(desc.get("health")):
            return "health type exec needs a non-empty command list"
        try:
            resolve_run_options(desc.get("runOptions") or {}, self.allowed_run_options, secrets_dir=self.secrets_dir,
                                read_secrets=False)
        except DriverError as e:
            return str(e)
        return None

    def platform(self):
        """{dev.flex.arch} of the runtime (EV-40), cached per instance; DriverError when it cannot answer."""
        if self._arch is None:
            cmd = ([self.rt, "version", "--format", "{{.Server.Arch}}"] if self.rt != "podman"
                   else [self.rt, "info", "--format", "{{.Host.Arch}}"])
            p = _run(cmd, check=False, timeout=30)
            a = p.stdout.strip()
            if p.returncode != 0 or not a or "<no value>" in a or " " in a:
                raise DriverError(f"{' '.join(cmd[:2])}...: no architecture ({(p.stderr or a).strip()[:200]})")
            self._arch = norm_arch(a)
        return {"dev.flex.arch": self._arch}

    def _inspect(self, ref: str) -> dict | None:
        """Container info, None when the container does not exist; DriverError when the runtime cannot answer."""
        cmd = [self.rt, "container", "inspect", ref]
        p = _run(cmd, check=False)
        if p.returncode != 0:
            if _NO_SUCH.search(p.stderr or ""):
                return None
            _fail(cmd, p)
        return (_json(cmd, p.stdout) or [None])[0]

    def _ps(self, *filters: str) -> list[str]:
        """Container ids with these label filters; DriverError when the runtime cannot answer."""
        cmd = [self.rt, "ps", "-a", "-q"]
        for f in filters:
            cmd += ["--filter", f"label={f}"]
        p = _run(cmd, check=False)
        if p.returncode != 0:
            _fail(cmd, p)
        return p.stdout.split()

    @staticmethod
    def _unreachable(e: DriverError) -> Observed:
        # SPI: an unreachable executor is never reported as absent (that would make the engine repair = re-apply
        # a workload that may well still run); present + not running, the reason in detail.
        return Observed(present=True, running=False, detail=f"executor unreachable: {e}")

    def _observed(self, info: dict | None) -> Observed:
        if not info:
            return Observed(present=False)
        labels = (info.get("Config") or {}).get("Labels") or {}
        st = info.get("State") or {}
        return Observed(present=True, running=bool(st.get("Running")), digest=labels.get("dev.flex.digest"),
                        version=labels.get("dev.flex.version"), restarts=int(info.get("RestartCount") or 0),
                        detail=st.get("Status", ""))

    def status(self, dep_id):
        try:
            return self._observed(self._inspect(self.cname(dep_id)))
        except DriverError as e:
            return self._unreachable(e)

    def list(self):
        """Never raises; an unreachable runtime lists nothing (callers must not read that as "all gone":
        status() is the per-deployment truth)."""
        out = {}
        try:
            for cid in self._ps(f"dev.flex.device={self.device}"):
                info = self._inspect(cid)
                if info:
                    dep = ((info.get("Config") or {}).get("Labels") or {}).get("dev.flex.deployment")
                    if dep:
                        out[dep] = self._observed(info)
        except DriverError:
            return {}
        return out

    def _probe(self, info: dict | None, probe: dict) -> bool:
        """One probe of a container (http | exec | process, EV-39). Never raises."""
        st = (info or {}).get("State") or {}
        if not st.get("Running") or st.get("Restarting"):
            return False
        probe = probe or {}
        t = probe.get("type") or "http"
        if t == "process":
            # running, not restarting, and not restarted since the previous probe (a crash loop between two probes
            # is never healthy)
            rc, cid = int(info.get("RestartCount") or 0), str(info.get("Id") or "")
            last = self._restarts.get(cid)
            self._restarts[cid] = rc
            return last is None or rc == last
        if t == "exec":
            cmd = probe.get("command")
            if not exec_probe_ok(probe) or not info.get("Id"):
                return False
            try:
                p = _run([self.rt, "exec", info["Id"], *cmd], check=False,
                         timeout=float(probe.get("execTimeoutSeconds", 2)))
            except DriverError:
                return False
            return p.returncode == 0
        port, path = int(probe.get("port", 8080)), probe.get("path", "/healthz")
        ip = _ip(info)
        if ip and not self._rootless():
            return http_probe(ip, port, path)
        return self._netns_http_probe(info, port, path)

    def _rootless(self) -> bool:
        """Rootless podman: its container IPs (if any) live in a network namespace the host cannot route to.
        Asked once (`podman info`); no clear answer is "not rootless" (the container IP is probed) and asked again."""
        if self.rt != "podman":
            return False
        if self._rootless_v is None:
            try:
                p = _run([self.rt, "info", "--format", "{{.Host.Security.Rootless}}"], check=False, timeout=30)
            except DriverError:
                return False
            v = p.stdout.strip().lower()
            if p.returncode != 0 or v not in ("true", "false"):
                return False
            self._rootless_v = v == "true"
        return self._rootless_v

    def _netns_http_probe(self, info: dict, port: int, path: str) -> bool:
        """GET 127.0.0.1:<port><path> inside the container's network namespace. Rootless podman joins it through
        `podman unshare` (the user namespace that owns it); a root engine with nsenter directly. Never raises."""
        pid = int(((info.get("State") or {}).get("Pid")) or 0)
        if pid <= 0:
            return False
        if self._rootless():
            prefix = [self.rt, "unshare"]
        elif os.geteuid() == 0:
            prefix = []
        else:
            return False             # an unprivileged engine cannot enter a rootful runtime's namespaces
        argv = [*prefix, "nsenter", f"--net=/proc/{pid}/ns/net", "--", sys.executable, "-I", "-c", _NETNS_HTTP,
                str(port), path, "1.0"]
        try:
            return _run(argv, check=False, timeout=10).returncode == 0
        except DriverError:
            return False

    def health(self, dep_id, probe):
        try:
            info = self._inspect(self.cname(dep_id))
        except DriverError:
            return False
        return bool(info) and self._probe(info, probe)

    def failed(self, dep_id, _info: dict | None = None):
        """Terminal failure: the container exited and the runtime's restart policy will not bring it back
        (on-failure:N exhausted, or policy `no`), or it is dead. A crash loop thus fails the gate at once.
        `_info`: an inspected container instead of the deployment's (the compose driver, per service)."""
        try:
            info = _info if _info is not None else self._inspect(self.cname(dep_id))
        except DriverError:
            return None   # unreachable is "not (yet) failed"
        if not info:
            return None
        st = info.get("State") or {}
        status = st.get("Status", "")
        if status == "dead":
            return "container dead"
        if status != "exited" or st.get("Restarting"):
            return None
        pol = ((info.get("HostConfig") or {}).get("RestartPolicy") or {})
        name, maxr = pol.get("Name", ""), int(pol.get("MaximumRetryCount") or 0)
        restarts = int(info.get("RestartCount") or 0)
        code = int(st.get("ExitCode") or 0)
        if name in ("", "no") or (name == "on-failure" and (code == 0 or (maxr and restarts >= maxr))):
            return f"container exited (code {code}, {restarts} restarts, restart policy {name or 'no'}:{maxr})"
        return None

    def capabilities(self):
        """Never raises: a missing CLI is `available: false`, a runtime that does not answer `reachable: false`."""
        out = {"profile": self.profile, "driver": self.name, "supportedRuntimes": ["oci"]}
        fmt = "{{.Server.Version}}" if self.rt == "docker" else "{{.Client.Version}}"
        try:
            p = _run([self.rt, "version", "--format", fmt], check=False, timeout=30)
        except DriverError as e:
            return {**out, "available": False, "detail": str(e)[:300]}
        v = p.stdout.strip()
        if p.returncode != 0 or not v or "<no value>" in v:
            return {**out, "reachable": False, "detail": (p.stderr or p.stdout).strip()[:300] or f"rc={p.returncode}"}
        out = {**out, "runtimeVersion": v, "healthTypes": list(HEALTH_TYPES), "runOptions": list(RUN_OPTION_KEYS)}
        try:
            out["platform"] = self.platform()
        except DriverError:
            pass                                    # no architecture answer: no platform facts (never guessed)
        return out


class AnkaiosDriver(CliContainerDriver):
    """Delegates container lifecycle to an Ankaios agent (runtime: podman) through the `ank` CLI.

    flexEngine stays the Margo-facing reconciler (pull, verify, safety gate, health gate, rollback);
    Ankaios is the local executor (create/delete workloads, restart policy, workload states).
    """

    name = "ankaios"
    runtime = "ankaios"
    # FE-4 run state and build are not mapped onto Ankaios workloads yet (the engine refuses staged/stopped for it)
    stage, start, stop, build = Driver.stage, Driver.start, Driver.stop, Driver.build

    def __init__(self, device_id: str, ank_bin: str, server_url: str, agent: str):
        super().__init__(device_id, runtime="podman")
        self.name = "ankaios"
        self.runtime = "ankaios"
        self.ank = [ank_bin, "-k", "--server-url", server_url]
        self.agent = agent

    def wname(self, dep_id: str) -> str:
        return f"flex_{self.device}_{dep_id}".replace(".", "_")

    def _manifest(self, wl: Workload, image_id: str) -> str:
        opts = self._labels(wl) + self._env(wl)
        rc = {"image": image_id, "commandOptions": opts}
        pol = {"always": "ALWAYS", "never": "NEVER"}.get(wl.restart.get("policy"), "ON_FAILURE")
        return json.dumps({"apiVersion": "v1", "workloads": {self.wname(wl.dep_id): {
            "runtime": "podman", "agent": self.agent, "restartPolicy": pol,
            "runtimeConfig": json.dumps(rc)}}})  # JSON is valid YAML

    def _containers(self, dep_id: str) -> list[dict]:
        """DriverError when podman cannot answer (never an empty list for "unreachable")."""
        ids = self._ps(f"dev.flex.device={self.device}", f"dev.flex.deployment={dep_id}")
        return [i for i in (self._inspect(c) for c in ids) if i]

    def _ank_state(self) -> dict:
        cmd = self.ank + ["get", "state", "-o", "json"]
        p = _run(cmd, check=False, timeout=30)
        if p.returncode != 0:
            _fail(cmd, p)
        return _json(cmd, p.stdout) or {}

    def plan(self, current, desired):
        steps = [f"podman load (if missing)"] if desired else []
        if desired:
            steps.append(f"ank apply workload {self.wname(desired.dep_id)} ({desired.version})")
        elif current:
            steps.append(f"ank delete workload {self.wname(current.dep_id)}")
        return steps

    def preflight(self, release, dep):
        if (getattr(release, "descriptor", None) or {}).get("runOptions"):
            return "runOptions are not supported by the Ankaios driver (use docker/podman)"
        return super().preflight(release, dep)

    def apply(self, current, desired):
        if desired.run_options:
            raise DriverError("runOptions are not supported by the Ankaios driver (use docker/podman)")
        image_id = self.ensure_image(desired)
        p = subprocess.run(self.ank + ["--no-wait", "apply", "-"], input=self._manifest(desired, image_id),
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise DriverError(f"ank apply failed: {p.stderr.strip()[:300]}")
        # wait until the container with the new digest exists (Ankaios replaces the old one)
        ok = wait_until(lambda: any(self._observed(i).digest == desired.digest and self._observed(i).running
                                    for i in self._containers(desired.dep_id)), timeout=60, period=0.1)
        if not ok:
            raise DriverError("ankaios did not start the workload within 60 s")

    def remove(self, dep_id):
        _run(self.ank + ["--no-wait", "delete", "workload", self.wname(dep_id)], check=False, timeout=30)
        wait_until(lambda: not self._containers(dep_id), timeout=60, period=0.1)

    def status(self, dep_id):
        try:
            cs = [self._observed(i) for i in self._containers(dep_id)]
        except DriverError as e:
            return self._unreachable(e)
        running = [c for c in cs if c.running]
        return running[0] if running else (cs[0] if cs else Observed(present=False))

    def health(self, dep_id, probe):
        try:
            infos = self._containers(dep_id)
        except DriverError:
            return False
        for info in infos:  # the podman container under the Ankaios workload (http | exec | process, EV-39)
            if (info.get("State") or {}).get("Running"):
                return self._probe(info, probe)
        return False

    def failed(self, dep_id):
        return None  # Ankaios replaces/restarts workloads itself; its workload states are not mapped yet

    def capabilities(self):
        """Never raises: a missing `ank` is `available: false`, a server that does not answer `reachable: false`."""
        out = {"profile": self.profile, "driver": self.name, "executor": "ankaios+podman", "supportedRuntimes": ["oci"]}
        try:
            st = self._ank_state()
        except DriverError as e:
            missing = "executable not found" in str(e)
            return {**out, ("available" if missing else "reachable"): False, "detail": str(e)[:300]}
        out = {**out, "ankaiosAgents": sorted((st.get("agents") or {}).keys()), "healthTypes": list(HEALTH_TYPES)}
        try:
            out["platform"] = self.platform()
        except DriverError:
            pass
        return out

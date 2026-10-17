"""Container drivers: Docker and Podman (same CLI surface), and Ankaios (Podman underneath).

The verified payload is a docker-archive tar (from the signed release). The driver loads it into the
runtime's image store, then runs it with labels that let it rebuild its view after an engine crash:
dev.flex.device / dev.flex.deployment / dev.flex.digest / dev.flex.version.
Restart policy is delegated to the runtime (docker/podman `--restart on-failure:N`, Ankaios
`restartPolicy: ON_FAILURE`). Health probes are executed by the engine (HTTP to the container IP).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import NoReturn

from flexengine2.drivers.base import Driver, DriverError, Observed, Workload, http_probe, wait_until


def _run(cmd: list[str], timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
    """Run a runtime CLI command. A missing binary or a hung CLI is an executor problem (DriverError) even with
    check=False; check=False only tolerates a non-zero exit."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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


# Extra `docker/podman run` options a release may request in its signed descriptor (`runOptions`), e.g.
# GPU or device access on the lab edge boxes. Only options named in the engine config's allow-list
# (`drivers.container.allowedRunOptions`) are used; anything else fails the deployment (S4).
RUN_OPTION_FLAGS = {"devices": "--device", "gpus": "--gpus", "runtime": "--runtime", "groupAdd": "--group-add"}


def run_option_args(opts: dict, allowed: dict) -> list[str]:
    """`{"devices": ["/dev/dri"], "gpus": "all"}` -> ["--device", "/dev/dri", "--gpus", "all"], checked
    against `allowed` (same shape, lists of permitted values). Raises DriverError on anything else."""
    args: list[str] = []
    for key, value in sorted((opts or {}).items()):
        flag = RUN_OPTION_FLAGS.get(key)
        if flag is None:
            raise DriverError(f"runOptions.{key} is not supported (supported: {sorted(RUN_OPTION_FLAGS)})")
        values = value if isinstance(value, list) else [value]
        permitted = set((allowed or {}).get(key) or [])
        for v in values:
            if not isinstance(v, str) or v not in permitted:
                raise DriverError(f"runOptions.{key}={v!r} is not in this device's allow-list")
            args += [flag, v]
    return args


class CliContainerDriver(Driver):
    profile = "container"
    name = "docker"   # class default; an instance refines it to its runtime (docker | podman)

    def __init__(self, device_id: str, runtime: str = "docker", allowed_run_options: dict | None = None):
        self.device = device_id
        self.rt = runtime
        self.name = runtime
        self._ver = None
        self.allowed_run_options = allowed_run_options or {}

    def cname(self, dep_id: str) -> str:
        return f"flex_{self.device}_{dep_id}"

    # ---- images ----------------------------------------------------------------------------
    def ensure_image(self, wl: Workload) -> str:
        """Load the verified archive unless present; always run by digest-pinned reference."""
        ref = wl.payload_annotations["dev.flex.imageRef"]  # docker.io/flexnode/<app>@sha256:...
        if _run([self.rt, "image", "inspect", ref], check=False).returncode != 0:
            _run([self.rt, "load", "-q", "-i", wl.payload_path], timeout=300)
            if _run([self.rt, "image", "inspect", ref], check=False).returncode != 0:
                raise DriverError(f"payload did not contain image {ref}")
        return ref

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
        extra = run_option_args(desired.run_options, self.allowed_run_options)  # validate before touching anything
        image_id = self.ensure_image(desired)
        self.remove(desired.dep_id)
        _run([self.rt, "run", "-d", "--name", self.cname(desired.dep_id), "--restart", self._restart(desired),
              *self._labels(desired), *self._env(desired), *extra, image_id], timeout=120)

    def remove(self, dep_id):
        n = self.cname(dep_id)
        if _run([self.rt, "container", "inspect", n], check=False).returncode == 0:
            _run([self.rt, "stop", "-t", "3", n], check=False, timeout=30)
            _run([self.rt, "rm", "-f", n], check=False, timeout=30)

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

    def health(self, dep_id, probe):
        try:
            info = self._inspect(self.cname(dep_id))
        except DriverError:
            return False
        if not info or not (info.get("State") or {}).get("Running"):
            return False
        ip = _ip(info)
        if not ip:
            return False
        return http_probe(ip, int(probe.get("port", 8080)), probe.get("path", "/healthz"))

    def failed(self, dep_id):
        """Terminal failure: the container exited and the runtime's restart policy will not bring it back
        (on-failure:N exhausted, or policy `no`), or it is dead. A crash loop thus fails the gate at once."""
        try:
            info = self._inspect(self.cname(dep_id))
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
        return {**out, "runtimeVersion": v}


class AnkaiosDriver(CliContainerDriver):
    """Delegates container lifecycle to an Ankaios agent (runtime: podman) through the `ank` CLI.

    flexEngine stays the Margo-facing reconciler (pull, verify, safety gate, health gate, rollback);
    Ankaios is the local executor (create/delete workloads, restart policy, workload states).
    """

    name = "ankaios"

    def __init__(self, device_id: str, ank_bin: str, server_url: str, agent: str):
        super().__init__(device_id, runtime="podman")
        self.name = "ankaios"
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
        for info in infos:
            if (info.get("State") or {}).get("Running") and _ip(info):
                return http_probe(_ip(info), int(probe.get("port", 8080)), probe.get("path", "/healthz"))
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
        return {**out, "ankaiosAgents": sorted((st.get("agents") or {}).keys())}

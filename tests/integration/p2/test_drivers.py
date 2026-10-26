"""Same lifecycle (install -> update -> failed update + rollback -> remove) on three container executors:
Docker (direct CLI), Podman (direct CLI) and Ankaios 1.0 (ank-server + ank-agent, runtime podman).
Input for the Ankaios-vs-direct decision; the lab run on a Jetson repeats it (see report §9)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .p2lab import ankaios_binaries, item, metric, running_version

PG = "perception_guard"


def _rss_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS"):
            return int(line.split()[1])
    return 0


@pytest.fixture(scope="module")
def ankaios(tmp_path_factory):
    d = ankaios_binaries()
    if not d or not shutil.which("podman"):
        pytest.skip("Ankaios binaries or podman not available")
    run = tmp_path_factory.mktemp("ank")
    (run / "agent").mkdir(mode=0o700)
    port = 25561
    procs = {}

    def start_server():
        procs["srv"] = subprocess.Popen([str(d / "ank-server"), "-k", "-a", f"127.0.0.1:{port}"],
                                        stdout=open(run / "server.log", "a"), stderr=subprocess.STDOUT)
        time.sleep(0.5)
        return procs["srv"]

    def restart_server():
        procs["srv"].kill()
        procs["srv"].wait(10)
        start_server()

    srv = start_server()
    agent = subprocess.Popen([str(d / "ank-agent"), "-k", "-n", "flexagent", "-s", f"http://127.0.0.1:{port}",
                              "-r", str(run / "agent")], stdout=open(run / "agent.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(1.0)
    yield {"ankBin": str(d / "ank"), "serverUrl": f"http://127.0.0.1:{port}", "agent": "flexagent",
           "pids": (srv.pid, agent.pid), "restart_server": restart_server, "bin_sizes": {b: (d / b).stat().st_size for b in ("ank", "ank-server", "ank-agent")}}
    agent.terminate()
    procs["srv"].terminate()
    agent.wait(10)
    procs["srv"].wait(10)


def _podman_version(dev, dep):
    p = subprocess.run(["podman", "ps", "--filter", f"label=dev.flex.device={dev}", "--filter",
                        f"label=dev.flex.deployment={dep}", "--format", '{{index .Labels "dev.flex.version"}}'],
                       capture_output=True, text=True)
    vs = p.stdout.split()
    return vs[0] if len(vs) == 1 else (None if not vs else vs)


@pytest.mark.slow
@pytest.mark.docker
@pytest.mark.parametrize("runtime", ["docker", pytest.param("podman", marks=pytest.mark.podman),
                                     pytest.param("ankaios", marks=pytest.mark.podman)])
def test_driver_lifecycle(env, runtime, request):
    if runtime == "podman" and not shutil.which("podman"):
        pytest.skip("podman not installed")
    if runtime == "ankaios":
        ank = request.getfixturevalue("ankaios")
        cont = {"runtime": "ankaios", "ankBin": ank["ankBin"], "serverUrl": ank["serverUrl"], "agent": ank["agent"]}
    else:
        cont = {"runtime": runtime}
    e = env.engine(f"drv{runtime[:3]}", container=cont).start()
    ver = (lambda: running_version(e.dev, "perception-guard")) if runtime == "docker" else \
        (lambda: _podman_version(e.dev, "perception-guard"))
    t = {}
    p = env.cloud.deploy(e.dev, [item(PG, "1.0.0", "container")])
    t["install"] = env.cloud.wait_converged(e.dev, {"perception-guard": "1.0.0"}) - p["publishedAt"]
    assert ver() == "1.0.0"
    p = env.cloud.deploy(e.dev, [item(PG, "1.1.0", "container")])
    t["update"] = env.cloud.wait_converged(e.dev, {"perception-guard": "1.1.0"}) - p["publishedAt"]
    assert ver() == "1.1.0"
    p = env.cloud.deploy(e.dev, [item(PG, "1.2.0", "container")])
    st = env.cloud.wait(lambda: (r := env.cloud.status(e.dev)["perception-guard"])["state"] == "Failed" and r, 60)
    t["failed_update_and_rollback"] = st["receivedAt"] - p["publishedAt"]
    assert st["rolledBackTo"] == "1.1.0" and st["rollbackHealthy"]
    env.cloud.wait(lambda: ver() == "1.1.0", 30, what="rolled back")
    p = env.cloud.deploy(e.dev, [])
    t["remove"] = env.cloud.wait_converged(e.dev, {}) - p["publishedAt"]
    env.cloud.wait(lambda: ver() is None, 30, what="removed")
    metric(f"driver_{runtime}", {k: round(v, 3) for k, v in t.items()})
    if runtime == "ankaios":
        metric("ankaios_rss_kb", {"server": _rss_kb(ank["pids"][0]), "agent": _rss_kb(ank["pids"][1])})
        metric("ankaios_binary_bytes", ank["bin_sizes"])


@pytest.mark.slow
@pytest.mark.podman
def test_ankaios_server_restart_loses_state(env, ankaios):
    """Ankaios keeps its desired state in memory: after an ank-server restart the agent deletes every
    workload it does not see in the (now empty) state. The flexEngine reconciler notices the drift and
    re-applies from its own persisted state; we measure that outage."""
    cont = {"runtime": "ankaios", "ankBin": ankaios["ankBin"], "serverUrl": ankaios["serverUrl"], "agent": ankaios["agent"]}
    e = env.engine("ankrs", container=cont).start()
    env.cloud.deploy(e.dev, [item(PG, "1.1.0", "container")])
    env.cloud.wait_converged(e.dev, {"perception-guard": "1.1.0"})
    t0 = time.time()
    ankaios["restart_server"]()
    env.cloud.wait(lambda: _podman_version(e.dev, "perception-guard") is None, 30, period=0.05, what="agent deleted workload")
    t_gone = time.time()
    env.cloud.wait(lambda: _podman_version(e.dev, "perception-guard") == "1.1.0", 60, period=0.05, what="re-applied")
    t_back = time.time()
    assert any(a["action"] == "repair" for a in e.actions())
    metric("ankaios_server_restart", {"workload_deleted_after_s": round(t_gone - t0, 3),
                                      "outage_s": round(t_back - t_gone, 3)})

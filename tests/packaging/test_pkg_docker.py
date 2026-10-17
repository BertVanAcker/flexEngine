"""Packaging layer in a clean Ubuntu 24.04 container (plan §7.2 layer 8): install from the built wheel + offline
wheelhouse, `systemd-analyze verify`, idempotent reinstall, the service under systemd (sd_notify READY/WATCHDOG and
the real engine), dry-run, upgrade keeping engine.yaml + dataDir, uninstall keeping state, purge.

The tests share one container and run in file order (each step builds on the previous one)."""

from __future__ import annotations

import base64
import json
import os

import pytest

from fe_pkg import PACKAGING, VERSION, Container, changes, image, make_bumped

pytestmark = [pytest.mark.docker, pytest.mark.slow]

WH = "/src/dist/wheelhouse"
EXTRAS = "mcu,plc,applab,otel"
SNAP = ("find /opt/flexengine /etc/flexengine /etc/systemd/system /var/lib/flexengine "
        "-printf '%p %m %u:%g %s %T@\\n' 2>/dev/null | sort; id flex 2>&1; "
        "systemctl is-enabled flexengine 2>&1; systemctl is-active flexengine 2>&1; true")


@pytest.fixture(scope="module")
def box(dist, wheel, tmp_path_factory):
    try:
        image()
    except RuntimeError as e:   # registry/apt unreachable: test_pkg_install.py's --root lifecycle is the fallback
        pytest.skip(f"no Ubuntu 24.04 test image: {e}")
    src = make_bumped(tmp_path_factory.mktemp("next"))      # upgrade source: this checkout, version <ver>.post1
    c = Container(f"fe-pkg-{os.getpid()}", {str(PACKAGING): "/src/packaging", str(dist): "/src/dist",
                                              str(src): "/src/next"})
    c.wheel = f"/src/dist/{wheel.name}"
    yield c
    c.close()


def inst(box, *args, check=True):
    r = box.sh("/src/packaging/install.sh " + " ".join(args), timeout=900)
    if check:
        assert r.returncode == 0, f"install.sh {' '.join(args)}\n{r.stdout}\n{r.stderr}"
    return r


def base(box):
    return f"--wheel {box.wheel} --wheelhouse {WH} --extras {EXTRAS} --device-id panda"


def test_fresh_install(box):
    assert box.ok(". /etc/os-release; echo $VERSION_ID").strip() == "24.04"
    r = inst(box, base(box))
    assert changes(r.stdout) > 8
    assert box.ok("/opt/flexengine/bin/flexengine version").strip() == VERSION
    assert box.ok("/opt/flexengine/bin/python -c 'import sys; print(sys.version[:4])'").strip() == "3.12"
    box.ok("/opt/flexengine/bin/python -c 'import smpclient, pyads, pymodbus, msgpack, opentelemetry.sdk'")
    assert "linux_x86_64-cp312" in r.stdout                          # picked the target's wheelhouse
    pw = box.ok("getent passwd flex").strip().split(":")
    assert pw[5] == "/var/lib/flexengine" and pw[6] == "/usr/sbin/nologin"
    assert box.ok("stat -c '%a %U:%G' /etc/flexengine /etc/flexengine/tokens /var/lib/flexengine "
                  "/etc/flexengine/engine.yaml").split("\n")[:4] == \
        ["750 root:flex", "700 flex:flex", "700 flex:flex", "640 root:flex"]
    unit = box.ok("cat /etc/systemd/system/flexengine.service")
    assert "ExecStart=/opt/flexengine/bin/flexengine run --config /etc/flexengine/engine.yaml" in unit
    assert "SupplementaryGroups=dialout" in box.ok("cat /etc/systemd/system/flexengine.service.d/10-groups.conf")


def test_systemd_analyze_verify(box):
    r = box.sh("systemd-analyze verify --man=no flexengine.service flexengine@test.service")
    assert r.returncode == 0, r.stdout + r.stderr
    if box.systemd:
        assert "flexengine.service" in box.ok("systemctl cat flexengine")
        exp = box.ok("systemd-analyze security flexengine --no-pager | tail -1")
        assert "Overall exposure level" in exp


def test_reinstall_is_idempotent(box):
    before = box.ok(SNAP)
    r = inst(box, base(box))
    assert changes(r.stdout) == 0, r.stdout
    assert "kept (exists, never overwritten)" in r.stdout
    assert box.ok(SNAP) == before


def test_service_notify_and_watchdog(box):
    """The unit's Type=notify + WatchdogSec=30 + sandbox with flexengine2.local.sdnotify (READY/STATUS/WATCHDOG),
    run as flex. (host.run does not send READY=1 yet: the real engine is started as Type=simple below.)"""
    if not box.systemd:
        pytest.skip("systemd does not run as PID 1 in this container (needs --privileged + cgroup v2)")
    box.ok("""cat > /usr/local/lib/fe-notify-test.py <<'EOF'
import os, time
from flexengine2.local import sdnotify as n
open("/var/lib/flexengine/notify-test", "w").write(f"{n.watchdog_interval()} {os.getuid()}")
n.ready("ready from sdnotify")
while True:
    n.watchdog()
    time.sleep(n.watchdog_interval() or 1)
EOF
mkdir -p /etc/systemd/system/flexengine.service.d
printf '[Service]\\nExecStart=\\nExecStart=/opt/flexengine/bin/python /usr/local/lib/fe-notify-test.py\\n' \
  > /etc/systemd/system/flexengine.service.d/50-test.conf
systemctl daemon-reload""")
    box.ok("timeout 60 systemctl start flexengine")
    props = box.ok("systemctl show flexengine -p ActiveState -p StatusText -p WatchdogUSec -p User -p NRestarts")
    assert "ActiveState=active" in props and "StatusText=ready from sdnotify" in props
    assert "WatchdogUSec=30s" in props and "User=flex" in props
    iv, uid = box.ok("cat /var/lib/flexengine/notify-test").split()
    assert float(iv) == 15.0 and uid == box.ok("id -u flex").strip()
    box.ok("systemctl stop flexengine && rm /etc/systemd/system/flexengine.service.d/50-test.conf "
           "/var/lib/flexengine/notify-test && systemctl daemon-reload")


def test_service_runs_real_engine(box):
    """The real `flexengine run` under the hardened unit (Type=simple drop-in until host.run sends READY=1): reads
    the root:flex config, writes its dataDir, stays up while flexCloud is unreachable."""
    if not box.systemd:
        pytest.skip("systemd does not run as PID 1 in this container")
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    # the operator's edit of the installed engine.yaml: the key and a reachable-later cloud URL
    box.ok(f"sed -i 's|  # cloudPublicKey: <b64>.*|  cloudPublicKey: {key}|; "
           "s|apiUrl: https://lattepanda.lab:8700|apiUrl: http://127.0.0.1:9\\n  allowPlainHttp: true|; /container: {runtime: docker}/d; "
           "/^    drivers:$/d' /etc/flexengine/engine.yaml")
    box.ok("echo one-time-token > /tmp/token && install -m 0600 -o flex -g flex /tmp/token /etc/flexengine/tokens/panda")
    chk = box.sh("/opt/flexengine/bin/flexengine config check /etc/flexengine/engine.yaml --resolve")
    if "invalid choice" not in chk.stderr:
        assert chk.returncode == 0, chk.stdout + chk.stderr
    box.ok("printf '[Service]\\nType=simple\\nWatchdogSec=0\\n' > /etc/systemd/system/flexengine.service.d/50-test.conf"
           " && systemctl daemon-reload && systemctl start flexengine")
    box.ok("for i in $(seq 40); do [ -f /var/lib/flexengine/panda/device.key ] && exit 0; sleep 0.25; done; exit 1")
    props = box.ok("systemctl show flexengine -p ActiveState -p NRestarts -p MainPID")
    assert "ActiveState=active" in props and "NRestarts=0" in props, props + box.ok("journalctl -u flexengine -n 30")
    assert box.ok("stat -c %U /var/lib/flexengine/panda/device.key").strip() == "flex"
    j = box.ok("journalctl -u flexengine --no-pager -n 200")
    assert "offline" in j, j
    # plan §6.3 item 4: `flexengine status --json` answers from the running service (local API socket, CP-7)
    from flexengine2 import contracts

    st = box.sh("for i in $(seq 40); do [ -S /run/flexengine/api.sock ] && break; sleep 0.25; done; "
                "runuser -u flex -- /opt/flexengine/bin/flexengine status --json")
    assert st.returncode == 0, st.stdout + st.stderr
    doc = json.loads(st.stdout)
    contracts.validate("cli-status", doc)
    assert doc["engine"]["mode"] == "running" and [d["deviceId"] for d in doc["devices"]] == ["panda"], doc
    box.pid = props.split("MainPID=")[1].split()[0]


def test_dry_run_changes_nothing(box):
    box.ok("cp -r /src/next /tmp/next")            # setuptools writes build/ + egg-info next to the sources
    before = box.ok(SNAP)
    r = inst(box, f"--from-source /tmp/next --wheelhouse {WH} --extras {EXTRAS} --device-id panda --dry-run")
    assert "[dry-run]" in r.stdout and "nothing changed" in r.stdout and changes(r.stdout) >= 1
    assert box.ok(SNAP) == before


def test_upgrade_keeps_config_and_data(box):
    cfg = box.ok("sha256sum /etc/flexengine/engine.yaml /etc/flexengine/tokens/panda")
    data = box.ok("find /var/lib/flexengine/panda -type f | sort | xargs sha256sum")
    r = inst(box, f"--from-source /tmp/next --wheelhouse {WH} --extras {EXTRAS} --device-id panda")
    assert box.ok("/opt/flexengine/bin/flexengine version").strip() == f"{VERSION}.post1"
    assert f"upgraded from {VERSION}" in r.stdout
    assert box.ok("sha256sum /etc/flexengine/engine.yaml /etc/flexengine/tokens/panda") == cfg
    after = box.ok("find /var/lib/flexengine/panda -type f | sort | xargs sha256sum")
    assert all(line in after for line in data.splitlines() if "device.key" in line or "state.json" in line)
    if box.systemd and getattr(box, "pid", None):
        assert "systemctl restart flexengine" in r.stdout          # a running service picks up the new venv
        box.ok("for i in $(seq 40); do systemctl is-active -q flexengine && exit 0; sleep 0.25; done; exit 1")
        assert box.ok("systemctl show flexengine -p MainPID").strip() != f"MainPID={box.pid}"
    # and the same upgrade again: nothing to do
    assert changes(inst(box, f"--from-source /tmp/next --wheelhouse {WH} --extras {EXTRAS} "
                             "--device-id panda").stdout) == 0


def test_uninstall_keeps_state(box):
    r = inst(box, "--uninstall")
    assert "kept (/etc/flexengine)" in r.stdout and "kept (/var/lib/flexengine)" in r.stdout
    assert box.sh("test -e /opt/flexengine").returncode != 0
    assert box.sh("test -e /etc/systemd/system/flexengine.service").returncode != 0
    assert box.sh("test -e /etc/systemd/system/flexengine.service.d/10-groups.conf").returncode != 0
    box.ok("test -f /etc/flexengine/engine.yaml && test -f /var/lib/flexengine/panda/device.key && id flex")
    if box.systemd:
        assert box.sh("systemctl is-active flexengine").stdout.strip() in ("inactive", "unknown")
    assert changes(inst(box, "--uninstall").stdout) == 0


def test_reinstall_after_uninstall_reuses_state(box):
    cfg = box.ok("sha256sum /etc/flexengine/engine.yaml")
    inst(box, base(box))
    assert box.ok("/opt/flexengine/bin/flexengine version").strip() == VERSION
    assert box.ok("sha256sum /etc/flexengine/engine.yaml") == cfg


def test_purge_removes_everything(box):
    inst(box, "--uninstall --purge")
    for p in ("/opt/flexengine", "/etc/flexengine", "/var/lib/flexengine", "/etc/systemd/system/flexengine.service",
              "/etc/systemd/system/flexengine@.service"):
        assert box.sh(f"test -e {p}").returncode != 0, p
    assert box.sh("getent passwd flex").returncode != 0 and box.sh("getent group flex").returncode != 0
    assert changes(inst(box, "--uninstall --purge").stdout) == 0

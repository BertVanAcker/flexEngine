"""flexengine2.local.sdnotify: datagrams on $NOTIFY_SOCKET, no-op without it, watchdog interval from $WATCHDOG_USEC."""

from __future__ import annotations

import os
import socket
import sys

import pytest

from flexengine2.local import sdnotify

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX")


@pytest.fixture
def sock(tmp_path):
    path = str(tmp_path / "notify.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(2)
    yield path, s
    s.close()


def recv(s) -> list[str]:
    return s.recv(4096).decode().split("\n")


def test_noop_without_socket():
    env = {}
    assert not sdnotify.enabled(env)
    assert sdnotify.ready("x", env=env) is False
    assert sdnotify.watchdog(env=env) is False
    assert sdnotify.status("x", env=env) is False
    assert sdnotify.stopping(env=env) is False


def test_noop_uses_process_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.ready() is False


def test_ready_status(sock):
    path, s = sock
    env = {"NOTIFY_SOCKET": path}
    assert sdnotify.enabled(env)
    assert sdnotify.ready("3 devices", env=env)
    assert recv(s) == ["READY=1", "STATUS=3 devices"]
    assert sdnotify.ready(env=env)
    assert recv(s) == ["READY=1"]


def test_watchdog_status_stopping(sock):
    path, s = sock
    env = {"NOTIFY_SOCKET": path}
    assert sdnotify.watchdog(env=env)
    assert recv(s) == ["WATCHDOG=1"]
    assert sdnotify.status("tick 7\nsecond line", env=env)
    assert recv(s) == ["STATUS=tick 7 second line"]  # newlines would start a new field
    assert sdnotify.stopping("draining", env=env)
    assert recv(s) == ["STOPPING=1", "STATUS=draining"]


def test_process_env(sock, monkeypatch):
    path, s = sock
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sdnotify.watchdog()
    assert recv(s) == ["WATCHDOG=1"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract sockets are Linux only")
def test_abstract_socket():
    name = f"flexengine-test-{os.getpid()}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(b"\0" + name.encode())
    s.settimeout(2)
    try:
        assert sdnotify.ready(env={"NOTIFY_SOCKET": "@" + name})
        assert recv(s) == ["READY=1"]
    finally:
        s.close()


def test_send_errors_are_swallowed(tmp_path):
    assert sdnotify.ready(env={"NOTIFY_SOCKET": str(tmp_path / "missing.sock")}) is False
    assert sdnotify.ready(env={"NOTIFY_SOCKET": "vsock:2:1234"}) is False


@pytest.mark.parametrize("env,expected", [
    ({}, None),
    ({"WATCHDOG_USEC": "30000000"}, 15.0),
    ({"WATCHDOG_USEC": "0"}, None),
    ({"WATCHDOG_USEC": "-5"}, None),
    ({"WATCHDOG_USEC": "abc"}, None),
    ({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "1"}, None),   # meant for another process
    ({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "x"}, None),
    ({"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": "<self>"}, 15.0),
])
def test_watchdog_interval(env, expected):
    env = {k: (str(os.getpid()) if v == "<self>" else v) for k, v in env.items()}
    if os.getpid() == 1 and env.get("WATCHDOG_PID") == "1":
        pytest.skip("running as PID 1")
    assert sdnotify.watchdog_interval(env) == expected


def test_watchdog_interval_process_env(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert sdnotify.watchdog_interval() == 1.0

"""Minimal, dependency-free sd_notify(3) client for `Type=notify` + `WatchdogSec=` (EV-08).

    from flexengine2.local import sdnotify
    sdnotify.ready("3 devices")            # READY=1 + STATUS=...
    iv = sdnotify.watchdog_interval()      # seconds between pings (half of WatchdogSec), or None
    sdnotify.watchdog()                    # WATCHDOG=1
    sdnotify.stopping()                    # STOPPING=1

Every call is a no-op (returns False) when $NOTIFY_SOCKET is unset, so the engine behaves the same outside systemd
(Mac dev, tests, containers). Abstract sockets (`@name`) and filesystem sockets are both supported. Errors while
sending are swallowed: a missing watchdog ping must never take the engine down by itself (systemd will do that).
"""

from __future__ import annotations

import os
import socket

__all__ = ["notify", "ready", "watchdog", "status", "stopping", "watchdog_interval", "enabled"]


def _address(raw: str) -> str | bytes:
    if raw.startswith("@"):  # abstract namespace (Linux)
        return b"\0" + raw[1:].encode()
    return raw


def enabled(env=None) -> bool:
    """True when systemd (or a test) gave us a notification socket."""
    env = os.environ if env is None else env
    return bool(env.get("NOTIFY_SOCKET"))


def notify(*fields: str, env=None) -> bool:
    """Send `KEY=VALUE` lines in one datagram; True if a datagram was sent."""
    env = os.environ if env is None else env
    raw = env.get("NOTIFY_SOCKET")
    if not raw or not fields:
        return False
    if not (raw.startswith("/") or raw.startswith("@")):  # vsock: etc. are not supported here
        return False
    msg = "\n".join(f.replace("\n", " ") for f in fields).encode()  # one field per line
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0)) as s:
            s.connect(_address(raw))
            s.sendall(msg)
        return True
    except OSError:
        return False


def ready(status_text: str | None = None, env=None) -> bool:
    fields = ["READY=1"] + ([f"STATUS={status_text}"] if status_text else [])
    return notify(*fields, env=env)


def watchdog(env=None) -> bool:
    return notify("WATCHDOG=1", env=env)


def status(text: str, env=None) -> bool:
    return notify(f"STATUS={text}", env=env)


def stopping(status_text: str | None = None, env=None) -> bool:
    fields = ["STOPPING=1"] + ([f"STATUS={status_text}"] if status_text else [])
    return notify(*fields, env=env)


def watchdog_interval(env=None) -> float | None:
    """Seconds between WATCHDOG=1 pings: half of $WATCHDOG_USEC (systemd's recommendation), or None when the
    watchdog is off, malformed, or meant for another process ($WATCHDOG_PID set and not ours)."""
    env = os.environ if env is None else env
    usec = env.get("WATCHDOG_USEC")
    if not usec:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid:
        try:
            if int(pid) != os.getpid():
                return None
        except ValueError:
            return None
    try:
        v = int(usec)
    except ValueError:
        return None
    if v <= 0:
        return None
    return v / 1e6 / 2.0

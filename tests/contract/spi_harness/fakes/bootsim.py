"""Boot simulator for the os-rauc tests: two A/B slots, a GRUB-like boot counter and a bootloader fallback.

State: one JSON file (flock-protected, atomic writes), shared by
  * the fake `rauc` (fakerauc.py, push tier): it imports this module,
  * the REAL rauc 1.11 (nightly tier): `bootloader=custom` calls `bootsim.py <state> backend <op> ...` inside the
    container (the state file is bind-mounted at the same path),
  * the reboot hook (`rebootCommand` of the driver): `bootsim.py <state> reboot`,
  * the tests: `boot()` after killing the engine ("the machine reboots"), `health` as a device-side health check.

Bootloader model (RAUC's GRUB example, with `maxTries` attempts instead of one): every boot of a slot increments its
`tries`; a slot is bootable while `ok` and `tries < maxTries`; `mark-good` (set-state good) resets `tries` to 0;
`set-primary X` (RAUC after an install, `mark-active`) puts X first with `ok`, `tries = 0`; `set-state X bad` clears
`ok`. The boot picks the primary if bootable, else the other slot (fallback). A slot whose image header says
`bootable=0` "does not come up" (no engine, kernel panic): the watchdog resets the machine and the next attempt
starts, until the counter is exhausted and the bootloader falls back.

Image header (first line of the slot device / the bundle's image): `FLEXSIM bootable=<0|1> healthy=<0|1> ...`.

    bootsim.py STATE init A=<dev> B=<dev> [--booted A] [--max-tries 3] [--auto-boot] [--compatible flex-test]
    bootsim.py STATE backend get-primary | set-primary X | get-state X | set-state X good|bad | get-current
    bootsim.py STATE reboot          # the driver's reboot hook: boots at once (autoBoot) or records the request
    bootsim.py STATE boot            # the machine boots (tests, after killing the engine)
    bootsim.py STATE health          # exit 0 when the booted slot's image says healthy=1
    bootsim.py STATE show
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def _lock_path(state: Path) -> Path:
    return state.with_name(state.name + ".lock")


@contextlib.contextmanager
def locked(state: str | Path):
    """Read-modify-write of the state under an exclusive flock; yields the dict, writes it back atomically."""
    state = Path(state)
    with open(_lock_path(state), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            st = json.loads(state.read_text())
            yield st
            tmp = state.with_name(f".{state.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
            os.chmod(tmp, 0o666)
            os.replace(tmp, state)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load(state: str | Path) -> dict:
    with open(_lock_path(Path(state)), "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)
        try:
            return json.loads(Path(state).read_text())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def init(state: str | Path, slots: dict[str, str], booted: str = "A", max_tries: int = 3, auto_boot: bool = True,
         compatible: str = "flex-test", slot_names: dict[str, str] | None = None, on_boot: list[str] | None = None,
         image: str = "FLEXSIM bootable=1 healthy=1 factory\n") -> dict:
    """slots: bootname -> device file. Every slot starts with a bootable factory image; `booted` is up."""
    state = Path(state)
    state.parent.mkdir(parents=True, exist_ok=True)
    names = slot_names or {b: f"rootfs.{i}" for i, b in enumerate(sorted(slots))}
    for dev in slots.values():
        p = Path(dev)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(image)
            os.chmod(p, 0o666)
    st = {"compatible": compatible, "maxTries": int(max_tries), "autoBoot": bool(auto_boot),
          "primary": booted, "booted": booted, "bootId": str(uuid.uuid4()), "boots": 1, "bootAttempts": 1,
          "rebootRequested": 0, "installs": 0, "installDelay": 0.0, "onBoot": on_boot or [],
          "slots": {b: {"name": names[b], "device": str(dev), "ok": True, "tries": 0, "bundle": None}
                    for b, dev in slots.items()},
          "log": [f"init booted={booted}"]}
    state.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.chmod(state, 0o666)
    _write_boot_id(state, st)
    return st


def boot_id_file(state: str | Path) -> Path:
    return Path(state).with_name("boot_id")


def _write_boot_id(state: Path, st: dict) -> None:
    p = boot_id_file(state)
    p.write_text(st["bootId"] + "\n")
    os.chmod(p, 0o666)


def header(dev: str) -> dict:
    """Flags of a slot image's first line ({} for an unreadable / foreign image)."""
    try:
        with open(dev, "rb") as f:
            line = f.readline(512).decode("utf-8", "replace")
    except OSError:
        return {}
    if not line.startswith("FLEXSIM"):
        return {}
    out = {}
    for tok in line.split()[1:]:
        k, _, v = tok.partition("=")
        out[k] = v
    return out


def other(st: dict, bootname: str) -> str:
    return next(b for b in st["slots"] if b != bootname)


def bootable(st: dict, b: str) -> bool:
    s = st["slots"][b]
    return bool(s["ok"]) and int(s["tries"]) < int(st["maxTries"])


def primary_of(st: dict) -> str:
    """The slot the next boot takes (the primary while bootable, else the fallback)."""
    p = st["primary"]
    if not bootable(st, p):
        o = other(st, p)
        p = o if bootable(st, o) else p
    return p


# ---- RAUC custom bootloader backend ------------------------------------------------------------------------------
def backend(state: str | Path, op: str, *args: str) -> str:
    with locked(state) as st:
        st["log"].append(f"backend {op} {' '.join(args)}".strip())
        if op == "get-primary":
            return primary_of(st)
        if op == "set-primary":
            b = args[0]
            st["primary"] = b
            st["slots"][b].update(ok=True, tries=0)
            return ""
        if op == "get-state":
            return "good" if st["slots"][args[0]]["ok"] else "bad"
        if op == "set-state":
            b, val = args[0], args[1]
            if val == "good":
                st["slots"][b].update(ok=True, tries=0)
            else:
                st["slots"][b]["ok"] = False
            return ""
        if op == "get-current":
            return st["booted"]
        raise SystemExit(f"bootsim backend: unknown operation {op!r}")


# ---- reboot / boot -----------------------------------------------------------------------------------------------
def reboot(state: str | Path) -> dict:
    """The driver's reboot hook. autoBoot: the machine 'reboots' at once (the calling engine survives, as if it had
    been restarted on the new slot); else the request is recorded and the test reboots (kill engine, boot())."""
    with locked(state) as st:
        st["rebootRequested"] = int(st.get("rebootRequested", 0)) + 1
        st["log"].append("reboot requested")
        auto = st.get("autoBoot", True)
    return boot(state) if auto else load(state)


def boot(state: str | Path) -> dict:
    """Power on: the bootloader picks the slot (counting attempts); unbootable images reset until it falls back."""
    with locked(state) as st:
        for _ in range(4 * int(st["maxTries"]) + 4):
            order = [st["primary"], other(st, st["primary"])]
            cand = next((b for b in order if bootable(st, b)), None)
            if cand is None:
                st["booted"] = None
                st["log"].append("boot: no bootable slot (bricked)")
                break
            s = st["slots"][cand]
            s["tries"] = int(s["tries"]) + 1
            st["bootAttempts"] = int(st.get("bootAttempts", 0)) + 1
            if header(s["device"]).get("bootable", "1") == "1":
                st["booted"] = cand
                st["bootId"] = str(uuid.uuid4())
                st["boots"] = int(st.get("boots", 0)) + 1
                st["rebootRequested"] = 0
                st["log"].append(f"boot: {cand} up (try {s['tries']})")
                break
            if int(s["tries"]) >= int(st["maxTries"]):
                s["ok"] = False   # counter exhausted: the bootloader script clears the slot's OK flag
            st["log"].append(f"boot: {cand} did not come up (try {s['tries']}), watchdog reset")
        hooks = list(st.get("onBoot") or [])
        booted = st["booted"]
        _write_boot_id(Path(state), st)
    for cmd in hooks:   # e.g. restart `rauc service --override-boot-slot=<booted>` in the nightly container
        subprocess.run([c.replace("{booted}", str(booted)) for c in cmd], check=True, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return load(state)


def healthy(state: str | Path) -> bool:
    st = load(state)
    b = st.get("booted")
    return bool(b) and header(st["slots"][b]["device"]).get("healthy", "1") == "1"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    state, cmd, args = argv[0], argv[1], argv[2:]
    if cmd == "init":
        slots = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("--"))
        opts = {a.split("=", 1)[0]: (a.split("=", 1) + [""])[1] for a in args if a.startswith("--")}
        init(state, slots, booted=opts.get("--booted", "A"), max_tries=int(opts.get("--max-tries", 3)),
             auto_boot="--auto-boot" in opts, compatible=opts.get("--compatible", "flex-test"))
        return 0
    if cmd == "backend":
        out = backend(state, *args)
        if out:
            print(out)
        return 0
    if cmd == "reboot":
        reboot(state)
        return 0
    if cmd == "boot":
        st = boot(state)
        print(st["booted"])
        return 0
    if cmd == "health":
        return 0 if healthy(state) else 1
    if cmd == "show":
        print(json.dumps(load(state), indent=1))
        return 0
    print(f"bootsim: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

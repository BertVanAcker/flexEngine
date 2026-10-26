"""Fake `rauc` 1.11 CLI for the os-rauc push tier: the subset the driver uses, over bootsim's JSON state.

    rauc [--conf=F] [--keyring=PEM] info --output-format=json BUNDLE
    rauc [--conf=F] install BUNDLE
    rauc [--conf=F] status [--detailed] --output-format=json
    rauc [--conf=F] status mark-good|mark-bad|mark-active [booted|other|<slot>]

Environment: FAKE_RAUC_STATE (bootsim state file, required), FAKE_RAUC_KEYRING (the "system.conf" keyring: one
trusted signer name per line), FAKE_RAUC_CALLS (JSONL log of every call, for the harness' transfer count).

Fake bundle file (testing/rauc.py build_fake_bundle): line 1 `FAKERAUCB1`, line 2 a JSON header {compatible,
version, signer}, then the image bytes (whose first line is bootsim's `FLEXSIM bootable=.. healthy=..` header).
The bundle hash is sha256 over the whole file, as RAUC reports a bundle hash per slot after an install. Output
shapes follow rauc 1.11.3 (`--output-format=json`), recorded in the nightly tier.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootsim  # noqa: E402

MAGIC = b"FAKERAUCB1\n"


def die(msg: str, rc: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(rc)


def read_bundle(path: str, keyring: str | None) -> tuple[dict, bytes, str]:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        die(f"Failed to open bundle {path}: {e.strerror}")
    if not data.startswith(MAGIC):
        die(f"{path}: Invalid bundle format: not a RAUC bundle")
    rest = data[len(MAGIC):]
    head, _, image = rest.partition(b"\n")
    try:
        meta = json.loads(head)
    except ValueError:
        die(f"{path}: Invalid bundle format: broken manifest")
    trusted = []
    if keyring:
        try:
            trusted = [l.strip() for l in Path(keyring).read_text().splitlines() if l.strip()]
        except OSError as e:
            die(f"failed to load keyring {keyring}: {e.strerror}")
    if not keyring:
        die("signature verification failed: no keyring configured")
    if meta.get("signer") not in trusted:
        die(f"signature verification failed: Verify error: certificate of {meta.get('signer')!r} not trusted")
    return meta, image, hashlib.sha256(data).hexdigest()


def slot_json(st: dict, b: str, detailed: bool) -> dict:
    s = st["slots"][b]
    d = {"class": "rootfs", "device": s["device"], "type": "raw", "bootname": b,
         "state": "booted" if st["booted"] == b else "inactive", "parent": None, "mountpoint": None,
         "boot_status": "good" if s["ok"] else "bad"}
    if detailed:
        bun = s.get("bundle")
        d["slot_status"] = ({"bundle": {"compatible": bun["compatible"], "version": bun["version"], "hash": bun["hash"]},
                             "status": "ok"} if bun else {"bundle": {"compatible": None}})
    return d


def status_json(st: dict, detailed: bool) -> dict:
    prim = bootsim.primary_of(st)
    return {"compatible": st["compatible"], "variant": "", "booted": st["booted"],
            "boot_primary": st["slots"][prim]["name"],
            "slots": [{st["slots"][b]["name"]: slot_json(st, b, detailed)} for b in sorted(st["slots"],
                                                                                         key=lambda x: st["slots"][x]["name"],
                                                                                         reverse=True)]}


def resolve(st: dict, which: str) -> str:
    if which == "booted":
        return st["booted"]
    if which == "other":
        return bootsim.other(st, st["booted"])
    for b, s in st["slots"].items():
        if s["name"] == which or b == which:
            return b
    die(f"rauc status: No slot with name '{which}' found")


def main(argv: list[str]) -> int:
    state = os.environ.get("FAKE_RAUC_STATE")
    if not state:
        die("fake rauc: FAKE_RAUC_STATE not set", 2)
    opts, args = {}, []
    for a in argv:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
        elif a.startswith("--"):
            opts[a[2:]] = True
        else:
            args.append(a)
    if os.environ.get("FAKE_RAUC_CALLS"):
        with open(os.environ["FAKE_RAUC_CALLS"], "a") as f:
            f.write(json.dumps({"t": time.time(), "args": args}) + "\n")
    if not args:
        die("usage: rauc <COMMAND>", 2)
    cmd = args[0]
    if cmd == "info":
        meta, image, h = read_bundle(args[1], opts.get("keyring") or os.environ.get("FAKE_RAUC_KEYRING"))
        print(json.dumps({"compatible": meta.get("compatible"), "version": meta.get("version"), "description": None,
                          "build": None, "hooks": [], "hash": h,
                          "images": [{"rootfs": {"variant": None, "filename": "rootfs.img",
                                                 "checksum": hashlib.sha256(image).hexdigest(), "size": len(image),
                                                 "hooks": [], "adaptive": []}}]}))
        return 0
    if cmd == "status":
        if len(args) == 1:
            st = bootsim.load(state)
            print(json.dumps(status_json(st, bool(opts.get("detailed")))))
            return 0
        op, which = args[1], (args[2] if len(args) > 2 else "booted")
        with bootsim.locked(state) as st:
            b = resolve(st, which)
            name = st["slots"][b]["name"]
            st["log"].append(f"rauc status {op} {name}")
            if op == "mark-good":
                if st.get("refuseMarkGood"):
                    die(f"rauc status: failed to mark slot {name} as good (injected)")
                st["slots"][b].update(ok=True, tries=0)
                msg = f"marked slot {name} as good"
            elif op == "mark-bad":
                st["slots"][b]["ok"] = False
                msg = f"marked slot {name} as bad"
            elif op == "mark-active":
                st["primary"] = b
                st["slots"][b].update(ok=True, tries=0)
                msg = f"activated slot {name}"
            else:
                die(f"rauc status: unknown subcommand {op}", 2)
        print(f"rauc status: {msg}")
        return 0
    if cmd == "install":
        meta, image, h = read_bundle(args[1], os.environ.get("FAKE_RAUC_KEYRING"))
        st = bootsim.load(state)
        if meta.get("compatible") != st["compatible"]:
            die(f"Compatible mismatch: Expected '{st['compatible']}' but bundle manifest has '{meta.get('compatible')}'")
        with bootsim.locked(state) as st:
            target = bootsim.other(st, st["booted"])
            st["slots"][target]["ok"] = False       # "Marking target slot ... as non-bootable"
            st["slots"][target]["bundle"] = None
            st["log"].append(f"install {meta.get('version')} -> {st['slots'][target]['name']}")
            delay = float(st.get("installDelay") or 0)
            dev = st["slots"][target]["device"]
            st["installPid"] = os.getpid()             # the simulated power cut kills the installer too
        with open(dev, "wb") as f:                   # a power cut during this leaves a torn, non-bootable slot
            f.write(b"PARTIAL\n")
            f.flush()
        if delay:
            time.sleep(delay)
        tmp = dev + ".tmp"
        with open(tmp, "wb") as f:
            f.write(image)
        shutil.move(tmp, dev)
        with bootsim.locked(state) as st:
            st["slots"][target]["bundle"] = {"compatible": meta.get("compatible"), "version": meta.get("version"),
                                             "hash": h}
            st["primary"] = target                   # "Marking target slot ... as bootable" (set-primary)
            st["slots"][target].update(ok=True, tries=0)
            st["installs"] = int(st.get("installs", 0)) + 1
            st["installPid"] = None
        print(f"Installing `{args[1]}` succeeded")
        return 0
    die(f"fake rauc: unsupported command {cmd!r}", 2)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

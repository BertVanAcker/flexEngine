"""flexengine CLI (EV-07). Operator commands are clients of the local API (CP-7, local/api.py): over the Unix socket
when the engine runs, else answered in-process from the data directories ("offline mode").

    flexengine run --config engine.yaml [--ticks N]
    flexengine status [--json]                      engine + per-device status (--json = GET /v1/status body)
    flexengine deployments [--device D] [--json]
    flexengine logs [-f] [--device D] [-n N] [--json]
    flexengine history [--since 1h|<unix s>] [--device D] [--json]
    flexengine bundle import <file> [--device D]    import a signed offline bundle
    flexengine config check <file> [--json] [--resolve]
    flexengine enroll --token-file <f> [--device D] write the onboarding token where the config expects it (0600)
    flexengine doctor [--repair [--yes]] [--json]
    flexengine plc-read --via ads|modbus|shm --conn '{...}' [--spec flex-plc.json]
    flexengine relay-inject|relay-status --smp host:port --model M --work W ...
    flexengine version

Common options of the API commands: --config (default $FLEXENGINE_CONFIG, else /etc/flexengine/engine.yaml),
--socket (default local.socket of the config, else /run/flexengine/api.sock), --offline (do not use the socket).
Exit codes: 0 ok, 1 the operation failed / check failed, 2 usage or config error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

from flexengine2 import __version__

DEFAULT_CONFIG = "/etc/flexengine/engine.yaml"


class CliError(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


# ---- formatting (pure; golden-tested) ----------------------------------------------------------------------------
def _age(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    if s < 86400:
        return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"
    return f"{int(s // 86400)}d{int(s % 86400 // 3600):02d}h"


def _iso(t) -> str:
    if not isinstance(t, (int, float)):
        return "-"
    from flexengine2.util import iso

    return iso(float(t))


def _table(rows: list[list[str]]) -> list[str]:
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows]


def _summary(deps: list[dict]) -> str:
    if not deps:
        return "none"
    order = ["Installed", "Staged", "Stopped", "Installing", "Pending", "Failed", "Removing", "Removed"]
    n = {s: sum(d["state"] == s for d in deps) for s in order}
    out = [f"{n[s]} {s.lower()}" for s in order if n[s]]
    sick = sum(d.get("health") == "unhealthy" for d in deps)
    return ", ".join(out) + (f" ({sick} unhealthy)" if sick else "")


def format_status(doc: dict) -> str:
    e = doc["engine"]
    mode = e.get("mode", "running")
    head = f"flexengine {e['version']} (spi {e['spi']}) {mode}"
    if mode != "offline":
        head += f", up {_age(e['uptimeSeconds'])}"
    if e.get("otel"):
        head += ", otel on"
    lines = [head]
    if e.get("configFile"):
        lines.append(f"config: {e['configFile']}")
    if not doc["devices"]:
        return "\n".join(lines + ["no devices"])
    rows = [["DEVICE", "ROLE", "ONBOARDED", "MANIFEST", "ONLINE", "ETAG AGE", "OUTBOX", "DEPLOYMENTS"]]
    for d in doc["devices"]:
        rows.append([d["deviceId"], d["role"], "yes" if d["onboarded"] else "no", str(d["manifestVersion"]),
                     "yes" if d["online"] else "no", _age(d.get("etagAgeSeconds")), str(d["outboxLength"]),
                     _summary(d["deployments"])])
    lines += [""] + _table(rows)
    notes = []
    for d in doc["devices"]:
        rej = d.get("rejected")
        if rej:
            notes.append(f"{d['deviceId']}: manifestVersion {rej.get('manifestVersion')} rejected"
                         + (f" ({rej['deployment']})" if rej.get("deployment") else "") + f": {rej.get('reason')}")
        j = d.get("journal")
        if j:
            notes.append(f"{d['deviceId']}: install in flight: {j.get('dep')} {j.get('phase', '')}".rstrip())
        if d.get("downgradesRefused"):
            notes.append(f"{d['deviceId']}: {d['downgradesRefused']} downgrade(s) refused")
        lr = d.get("lastRefusal")
        if lr:
            code = f"{lr['reasonCode']}: " if lr.get("reasonCode") else ""
            notes.append(f"{d['deviceId']}: desired state refused ({code}{lr['reason']}; {lr.get('count', 1)}x)")
        for dep in d["deployments"]:
            if dep["state"] in ("Failed", "Pending") and dep.get("reason"):
                code = f" [{dep['reasonCode']}]" if dep.get("reasonCode") else ""
                notes.append(f"{d['deviceId']}/{dep['id']}: {dep['state']}: {dep['reason']}{code}")
    if notes:
        lines += [""] + notes
    return "\n".join(lines)


def format_deployments(doc: dict, device: str | None = None) -> str:
    rows = [["DEVICE", "DEPLOYMENT", "DRIVER", "STATE", "VERSION", "HEALTH", "PREVIOUS", "REASON"]]
    for d in doc["devices"]:
        if device and d["deviceId"] != device:
            continue
        for dep in d["deployments"]:
            reason = dep.get("reason", "")
            if dep.get("reasonCode"):
                reason = f"{reason} [{dep['reasonCode']}]"
            rows.append([d["deviceId"], dep["id"], dep["driver"], dep["state"], dep.get("version") or "-",
                         dep.get("health") or "-", dep.get("previousVersion") or "-", reason])
    if len(rows) == 1:
        return "no deployments"
    return "\n".join(_table(rows))


def format_history(entries: list[dict]) -> str:
    if not entries:
        return "no status reports in range"
    rows = [["TIME", "DEVICE", "DEPLOYMENT", "SEQ", "STATE", "VERSION", "DETAIL"]]
    for e in entries:
        r = e["record"]
        detail = []
        if r.get("health"):
            detail.append(r["health"])
        if r.get("reason"):
            detail.append(r["reason"])
        if "rolledBackTo" in r:
            detail.append(f"rolled back to {r['rolledBackTo']}")
        if e.get("delivered") is False:
            detail.append("(not yet delivered)")
        rows.append([_iso(r.get("deviceTime")), e["deviceId"], r["dep"], str(r["seq"]), r["state"],
                     r.get("version") or "-", "; ".join(detail)])
    return "\n".join(_table(rows))


def format_log(rec: dict) -> str:
    if rec.get("source") == "actions" and "action" in rec:
        extra = " ".join(f"{k}={rec[k]}" for k in ("version", "digest", "reason", "failed", "why") if rec.get(k))
        msg = f"{rec['action']} {rec.get('dep', '')} {extra}".rstrip()
    elif rec.get("source") == "workload":
        stream = " (stderr)" if rec.get("stream") == "stderr" else ""
        msg = f"{rec.get('dep', '?')}{stream}: {rec.get('msg', '')}"
    else:
        msg = rec.get("msg") or rec.get("message") or json.dumps({k: v for k, v in rec.items()
                                                                   if k not in ("t", "device", "source")})
    return f"{_iso(rec.get('t'))} [{rec.get('device', '?')}] {rec.get('source', '?'):<7} {msg}"


def parse_since(v: str | None, now: float | None = None) -> float | None:
    """'90s', '30m', '2h', '1d' (ago) or unix seconds."""
    if v is None:
        return None
    now = time.time() if now is None else now
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([smhd])\s*", v)
    if m:
        return now - float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    try:
        return float(v)
    except ValueError:
        raise CliError(f"--since {v!r}: use e.g. 30m, 2h, 1d or unix seconds") from None


# ---- backend: socket or offline ----------------------------------------------------------------------------------
def _config_path(a) -> str | None:
    p = getattr(a, "config", None) or os.environ.get("FLEXENGINE_CONFIG")
    if p:
        return p
    return DEFAULT_CONFIG if Path(DEFAULT_CONFIG).exists() else None


def load_offline_config(path: str) -> dict:
    """Parse + validate; resolve stackInfo/releaseKeyFiles; a consumed onboarding token file is not an error here."""
    from flexengine2.config.load import ConfigError, parse, resolve, validate

    cfg = parse(path)
    errors = validate(cfg)
    if errors:
        raise ConfigError(errors)
    for dev in cfg.get("devices", []):
        if dev.get("onboardingTokenFile") and not os.path.exists(dev["onboardingTokenFile"]):
            dev.pop("onboardingTokenFile")
    return resolve(cfg)


class Backend:
    def __init__(self, a):
        from flexengine2.local.api import DEFAULT_SOCKET, ApiClient, LocalApi, OfflineProvider, local_settings

        self.cfg, self.cfg_error = None, None
        cpath = _config_path(a)
        if cpath:
            try:
                self.cfg = load_offline_config(cpath)
            except Exception as e:  # noqa: BLE001  (reported only if the socket is not there either)
                self.cfg_error = e
        sock = getattr(a, "socket", None) or (local_settings(self.cfg)["socket_path"] if self.cfg else DEFAULT_SOCKET)
        self.client = None if getattr(a, "offline", False) else ApiClient(sock)
        if self.client is not None and not (os.path.exists(sock) and self.client.reachable()):
            self.client = None
        self.api = None
        if self.client is None:
            if self.cfg is None:
                why = f"{cpath}: {self.cfg_error}" if self.cfg_error else "no config (--config or $FLEXENGINE_CONFIG)"
                raise CliError(f"engine not reachable at unix:{sock} and cannot read offline: {why}")
            self.api = LocalApi(OfflineProvider(self.cfg, cpath), socket_path=None, log=lambda m: None)
        self.mode = "socket" if self.client else "offline"

    def request(self, method: str, target: str, body: bytes = b"", ctype: str | None = None, stream=False):
        if self.client is not None:
            r = self.client.request(method, target, body, ctype, timeout=None if stream else -1)
        else:
            r = self.api.handle(method, target, body)
        return r

    def json(self, method: str, target: str, body: bytes = b"", ctype: str | None = None):
        r = self.request(method, target, body, ctype)
        data = r.body if not r.streaming else b"".join(r.body)
        try:
            doc = json.loads(data) if data else None
        except ValueError:
            doc = None
        if r.status >= 400:
            detail = (doc or {}).get("detail") or (doc or {}).get("title") or data.decode(errors="replace")
            raise CliError(f"{method} {target}: {r.status} {detail}", 1)
        return doc


# ---- commands ----------------------------------------------------------------------------------------------------
def _api_opts(p):
    p.add_argument("--config", help="engine config (default $FLEXENGINE_CONFIG, else /etc/flexengine/engine.yaml)")
    p.add_argument("--socket", help="local API socket (default: local.socket of the config)")
    p.add_argument("--offline", action="store_true", help="read the data directories even if the engine runs")


def cmd_status(a) -> int:
    doc = Backend(a).json("GET", "/v1/status")
    print(json.dumps(doc, indent=1, sort_keys=True) if a.json else format_status(doc))
    return 0


def cmd_deployments(a) -> int:
    from flexengine2.local.api import quote_id

    b = Backend(a)
    if a.device and a.deployment:
        doc = b.json("GET", f"/v1/devices/{quote_id(a.device)}/deployments/{quote_id(a.deployment)}")
        print(json.dumps(doc, indent=1, sort_keys=True) if a.json else
              format_deployments({"devices": [{"deviceId": a.device, "deployments": [doc]}]}))
        return 0
    doc = b.json("GET", "/v1/status")
    if a.device and not any(d["deviceId"] == a.device for d in doc["devices"]):
        raise CliError(f"unknown device {a.device!r}", 1)
    if a.json:
        print(json.dumps([{"deviceId": d["deviceId"], **dep} for d in doc["devices"]
                          if not a.device or d["deviceId"] == a.device for dep in d["deployments"]], indent=1,
                         sort_keys=True))
    else:
        print(format_deployments(doc, a.device))
    return 0


def cmd_history(a) -> int:
    from urllib.parse import urlencode

    q = {}
    since = parse_since(a.since)
    if since is not None:
        q["since"] = repr(since)
    if a.device:
        q["device"] = a.device
    doc = Backend(a).json("GET", "/v1/history" + (f"?{urlencode(q)}" if q else ""))
    print(json.dumps(doc, indent=1, sort_keys=True) if a.json else format_history(doc))
    return 0


def cmd_logs(a) -> int:
    from urllib.parse import urlencode

    q = {"tail": str(a.lines)}
    if a.device:
        q["device"] = a.device
    if a.follow:
        q["follow"] = "1"
    b = Backend(a)
    r = b.request("GET", f"/v1/logs?{urlencode(q)}", stream=a.follow)
    if r.status >= 400:
        data = r.body if not r.streaming else b"".join(r.body)
        try:
            detail = json.loads(data).get("detail")
        except ValueError:
            detail = data.decode(errors="replace")
        raise CliError(f"logs: {r.status} {detail}", 1)
    chunks = r.body if r.streaming else [r.body]
    try:
        for chunk in chunks:
            for line in chunk.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                if a.json:
                    print(line, flush=True)
                else:
                    try:
                        print(format_log(json.loads(line)), flush=True)
                    except ValueError:
                        print(line, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
    return 0


def cmd_bundle(a) -> int:
    from urllib.parse import urlencode

    try:
        data = Path(a.file).read_bytes()
    except OSError as e:
        raise CliError(f"{a.file}: {e.strerror or e}") from None
    b = Backend(a)
    q = f"?{urlencode({'device': a.device})}" if a.device else ""
    doc = b.json("POST", f"/v1/bundle{q}", data, "application/x-tar")
    if a.json:
        print(json.dumps(doc, sort_keys=True))
    else:
        what = "imported" if doc.get("changed", True) else "already accepted"
        where = "the running engine" if b.mode == "socket" else "state.json (installed at the next engine start)"
        print(f"{doc['deviceId']}: manifestVersion {doc['manifestVersion']} {what} into {where}")
    return 0


def cmd_enroll(a) -> int:
    from flexengine2.config.load import ConfigError, parse, validate
    from flexengine2.util import atomic_write_bytes

    cpath = _config_path(a)
    if not cpath:
        raise CliError("no config (--config or $FLEXENGINE_CONFIG)")
    try:
        cfg = parse(cpath)
    except ConfigError as e:
        raise CliError(str(e)) from None
    errs = validate(cfg)
    if errs:
        raise CliError(str(ConfigError(errs)))
    devs = cfg["devices"]
    if a.device:
        devs = [d for d in devs if d["deviceId"] == a.device]
        if not devs:
            raise CliError(f"unknown device {a.device!r} in {cpath}")
    else:
        with_file = [d for d in devs if d.get("onboardingTokenFile")]
        devs = with_file if len(with_file) == 1 else devs
        if len(devs) != 1:
            raise CliError("several devices: choose one with --device")
    dev = devs[0]
    target = dev.get("onboardingTokenFile")
    if not target:
        i = cfg["devices"].index(dev)
        raise CliError(f"{dev['deviceId']}: the config does not name a token file; set devices[{i}].onboardingTokenFile")
    try:
        token = (sys.stdin.read() if a.token_file == "-" else Path(a.token_file).read_text()).strip()
    except OSError as e:
        raise CliError(f"{a.token_file}: {e.strerror or e}") from None
    if not token or "\n" in token or len(token) > 4096:
        raise CliError(f"{a.token_file}: expected one line with the onboarding token")
    t = Path(target)
    if not t.parent.exists():
        t.parent.mkdir(mode=0o700, parents=True)
    atomic_write_bytes(t, token.encode() + b"\n", mode=0o600)
    if a.owner:
        import grp
        import pwd

        user, _, group = a.owner.partition(":")
        try:
            os.chown(t, pwd.getpwnam(user).pw_uid if user else -1, grp.getgrnam(group).gr_gid if group else -1)
        except (KeyError, PermissionError) as e:
            raise CliError(f"chown {a.owner} {t}: {e}", 1) from None
    from flexengine2.local.api import config_devices

    dd = next(d["dataDir"] for d in config_devices(cfg) if d["deviceId"] == dev["deviceId"])
    st = Path(dd) / "state.json"
    onboarded = False
    try:
        onboarded = bool(json.loads(st.read_text()).get("onboarded"))
    except (OSError, ValueError, AttributeError):
        pass
    print(f"{dev['deviceId']}: onboarding token written to {t} (0600)"
          + ("; note: state.json says the device is already onboarded" if onboarded else
             "; the engine onboards at its next start"))
    return 0


def cmd_doctor(a) -> int:
    from flexengine2.local import doctor

    cpath = _config_path(a)
    if not cpath:
        raise CliError("no config (--config or $FLEXENGINE_CONFIG)")
    checks = doctor.run(cpath, socket_path=a.socket, repair=a.repair, yes=a.yes, timeout=a.timeout)
    if a.json:
        print(json.dumps({"ok": doctor.ok(checks), "checks": checks}, indent=1))
    else:
        print(doctor.format_checks(checks))
    return 0 if doctor.ok(checks) else 1


def _relay(a):
    from flexengine2.drivers.mcu.driver import LinkPool
    from flexengine2.gateway.relay import McuRelay

    host, port = a.smp.rsplit(":", 1)
    return McuRelay(LinkPool(host, int(port)), Path(a.model), a.deployment, a.device, Path(a.work),
                    zenoh_enabled=False)


def cmd_relay(a) -> int:
    r = _relay(a)
    if a.cmd == "relay-inject":
        from flexengine2.gateway.relay import find_port

        p = find_port(r.ports, a.instance, a.port)
        n = r.inject_raw(p.topic, r.codec.from_json(p.qname, json.loads(a.json)), f"{a.instance}.{a.port}")
        print(json.dumps({"instance": a.instance, "port": a.port, "type": p.qname, "topic": f"0x{p.topic:08x}",
                          "delivered": n}))
        return 0 if n else 1
    msgs = r.poll()
    for m in msgs:
        m["xcdr1"] = m["xcdr1"].hex()
    print(json.dumps(msgs, indent=1))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="flexengine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run every device engine of this host")
    p.add_argument("--config", required=True)
    p.add_argument("--ticks", type=int, default=0, help="stop after N scheduler rounds (0 = forever)")
    p.add_argument("--no-api", action="store_true", help="do not serve the local API socket")
    p = sub.add_parser("status", help="engine and per-device status")
    _api_opts(p)
    p.add_argument("--json", action="store_true", help="print the GET /v1/status body (cli-status.v1)")
    p = sub.add_parser("deployments", help="deployments of every device")
    _api_opts(p)
    p.add_argument("--device")
    p.add_argument("deployment", nargs="?", help="one deployment (needs --device)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("logs", help="action log and engine log lines")
    _api_opts(p)
    p.add_argument("-f", "--follow", action="store_true", help="keep streaming new lines")
    p.add_argument("--device")
    p.add_argument("-n", "--lines", type=int, default=200, help="last N lines (default 200)")
    p.add_argument("--json", action="store_true", help="raw JSON Lines")
    p = sub.add_parser("history", help="status reports (local ring buffer + outbox)")
    _api_opts(p)
    p.add_argument("--since", help="30m, 2h, 1d or unix seconds (default: last hour)")
    p.add_argument("--device")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("bundle", help="offline bundles")
    bsub = p.add_subparsers(dest="bundle_cmd", required=True)
    p = bsub.add_parser("import", help="import a signed offline bundle (tar)")
    _api_opts(p)
    p.add_argument("file")
    p.add_argument("--device", help="cross-check: the bundle must be for this device")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("enroll", help="store the one-time onboarding token where the config expects it")
    p.add_argument("--config")
    p.add_argument("--token-file", required=True, help="file with the token from flexCloud ('-' = stdin)")
    p.add_argument("--device")
    p.add_argument("--owner", help="user[:group] to give the token file to (when run as root)")
    p = sub.add_parser("doctor", help="check config, files, socket, cloud and executors")
    p.add_argument("--config")
    p.add_argument("--socket")
    p.add_argument("--repair", action="store_true", help="move a corrupt state.json aside (with --yes)")
    p.add_argument("--yes", action="store_true", help="confirm --repair")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p = sub.add_parser("plc-read", help="read the interface hash of a PLC once")
    p.add_argument("--via", required=True, choices=["ads", "modbus", "shm"])
    p.add_argument("--conn", required=True)
    p.add_argument("--spec")
    p = sub.add_parser("config", help="engine configuration tools")
    csub = p.add_subparsers(dest="config_cmd", required=True)
    p = csub.add_parser("check", help="validate an engine config (exit 0 valid, 1 invalid)")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help='print {"valid": bool, "errors": [{path, message}]}')
    p.add_argument("--resolve", action="store_true", help="also read stackInfo, releaseKeyFiles, onboardingTokenFile")
    for name in ("relay-inject", "relay-status"):
        p = sub.add_parser(name, help="MCU relay test tool (inject a message / poll the MCU outbox)")
        p.add_argument("--smp", required=True, help="host:port of the SMP link")
        p.add_argument("--model", required=True)
        p.add_argument("--deployment", default="d_pick_lab")
        p.add_argument("--device", default="portenta")
        p.add_argument("--work", required=True)
        if name == "relay-inject":
            p.add_argument("--instance", required=True)
            p.add_argument("--port", required=True)
            p.add_argument("--json", required=True)
    sub.add_parser("version")
    return ap


COMMANDS = {"status": cmd_status, "deployments": cmd_deployments, "history": cmd_history, "logs": cmd_logs,
            "bundle": cmd_bundle, "enroll": cmd_enroll, "doctor": cmd_doctor, "relay-inject": cmd_relay,
            "relay-status": cmd_relay}


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "version":
        print(__version__)
        return 0
    if a.cmd == "run":
        from flexengine2.host import load_config, run

        cfg = load_config(a.config)
        hook = None
        if not getattr(a, "no_api", False) and ((cfg.get("local") or {}).get("enabled", True)):
            from flexengine2.local.api import serve_host

            def hook(host):
                serve_host(host, config_file=a.config)
        run(cfg, ticks=a.ticks, on_ready=hook)
        return 0
    if a.cmd == "config":
        from flexengine2.config.load import check, format_error

        r = check(a.file, resolve_files=a.resolve)
        for w in r["warnings"]:
            print(f"warning: {w}", file=sys.stderr)
        if a.json:
            print(json.dumps({"valid": r["valid"], "errors": r["errors"]}))
        else:
            for e in r["errors"]:
                print(format_error(e))
            if r["valid"]:
                print(f"{a.file}: valid")
        return 0 if r["valid"] else 1
    if a.cmd == "plc-read":
        from flexengine2.drivers.plc.verify import READERS, Unreachable

        spec = json.loads(Path(a.spec).read_text()) if a.spec else {}
        try:
            h, detail = READERS[a.via](json.loads(a.conn), spec).read_hash()
        except Unreachable as e:
            print(f"unreachable: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"interfaceHash": h, "detail": detail}))
        return 0
    if a.cmd == "deployments" and a.deployment and not a.device:
        print("flexengine deployments: a deployment id needs --device", file=sys.stderr)
        return 2
    with _logs_to_stderr():
        try:
            return COMMANDS[a.cmd](a)
        except CliError as e:
            print(f"flexengine {a.cmd}: {e}", file=sys.stderr)
            return e.code
        except BrokenPipeError:
            return 0


@contextlib.contextmanager
def _logs_to_stderr():
    """Engine/library log records go to stderr while an operator command runs: stdout is its answer (--json)."""
    try:
        from flexengine2.obs import logging as flog
    except ImportError:  # pragma: no cover
        yield
        return
    cfg = getattr(flog, "_cfg", None)
    old = cfg.get("stream") if isinstance(cfg, dict) else None
    flog.configure(stream=sys.stderr)
    try:
        yield
    finally:
        if isinstance(cfg, dict):
            cfg["stream"] = old


if __name__ == "__main__":
    raise SystemExit(main())

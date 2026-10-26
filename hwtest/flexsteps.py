"""flexEngineV2 step types for the flexNode-arduino desk-runbook runner and GUI (plan 8.2, EV-14).

`register(runbook_module)` adds them to the runner's `ACTIONS` table (the GUI looks step kinds up in the same
table), so the pinned runner and GUI run them unmodified. Every step gets `(ctx, params)` like the built-in ones and
returns `(status, detail)`; console output and questions go through the runner's `say`/`ask`/`wait_until`, which
the GUI redirects to the browser.

  http_expect:      {url, method?, headers?, body?, token_file?, ca?, status?: 200, json_match?, body_contains?,
                     save?, timeout?: 10, every?: 0.5}
  cmd_json_expect:  {cmd, host?, cwd?, match?, expect_rc?: 0, json_lines?, save?, timeout?: 0, every?: 1}
  fe_status_expect: {device, deployment?, state?, version?, health?, fields?, device_fields?, absent?,
                     via?: api|cmd, timeout?: 60, every?: 1}
  fe_history_mark:  {name, device?, via?, timeout?: 60}
  fe_history_expect:{since, match: [{dep, state, version?, fields?}, ...], device?, max_duration?, min_duration?,
                     save_duration?, timeout?: 60}
  zenoh_expect:     {connect, key, match?, decode?: json|text|raw|cdr:<type>, model?, mode?: client, timeout?: 10}
  smp_probe:        {tty+baud? | host+port, cmd: echo|image_list|flexmod_status|flexmod_info, match?, timeout?: 10}
  ads_probe:        {net_id, ip, port?: 851, local_net_id?, instance?, hello_hash?, symbol?, plc_type?, equals?,
                     timeout?: 10}
  modbus_expect:    {host, port?: 502, unit?: 1, holding?: [start, count] | coils?: [start, count] |
                     input?: [start, count], equals, timeout?: 10}
  ssh_run:          {host, cmd, expect_rc?: 0, match?, save?, timeout?: 120}
  journal_mark:     {name, unit?: flexengine, host?, source?: journal | file:<path>}
  journal_expect:   {since, pattern?, fields?, count?: 1, absent?, unit?, host?, source?, timeout?: 30}
  write_file:       {path, content | json, mode?, host?, sudo?}    (the runner's own write_file plus mode/host/json/sudo)
  power_cycle:      {instruction, wait_up?: {host, port}, wait_down?: true, simulate?, timeout?: 300}

Matchers (json_match / match / fields): {"/json/pointer": expected}. A pointer segment `name[key=value]` picks the
first list element whose `key` equals `value` (e.g. `/devices[deviceId=panda]/deployments[id=app]/state`); a
segment `*` flattens one level (the values of a mapping or of the list's elements: `/slots/*[state=booted]`). The
expected value is a literal (strings compare with the JSON text of scalars: "true", "3", "null") or an operator
mapping: {regex}, {eq}, {ne}, {gte}, {lte}, {gt}, {lt}, {in: [...]}, {contains}, {exists: bool}, {len},
{where: {pointer: value}, count: n | {gte: n}} (filter a list, then count).

Conventions shared with the runbooks (variables, see runbooks/*.yaml):
  fe_api / fe_api_token_file   the engine's read-only TCP API (local.tcp) and its bearer token file
  fe / fe_host                 the flexengine CLI command and the host it runs on ("local" = this PC)
  dry_run=1                    simulated targets: `manual` steps run their `sim:` command and are answered with
                               `sim_answer` (default y); `power_cycle` runs `simulate:` instead of asking a person
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIPPED"
R = None                  # the runner module (tools/hwtest/runbook.py), set by register()
_ORIG = {}                # the runner's own actions we extend (manual, write_file)


# ---- runner glue -------------------------------------------------------------------------------------------------
def _say(text: str) -> None:
    (R.say if R is not None else print)(text)


def _wait(timeout: float, probe, interval: float = 0.3):
    if R is not None:
        return R.wait_until(timeout, probe, interval)
    deadline = time.time() + timeout
    while True:
        v = probe()
        if v or time.time() >= deadline:
            return v
        time.sleep(interval)


def subkeys(ctx, v):
    """The runner substitutes ${var} in values only; matcher keys are JSON pointers that may name a variable too."""
    if isinstance(v, dict):
        return {(ctx.sub(k) if isinstance(k, str) and "${" in k and ctx is not None else k): subkeys(ctx, x)
                for k, x in v.items()}
    if isinstance(v, list):
        return [subkeys(ctx, x) for x in v]
    return v


def _keyed(fn):
    def wrapper(ctx, p):
        return fn(ctx, subkeys(ctx, p) if isinstance(p, dict) else p)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def dry_run(ctx) -> bool:
    return truthy(ctx.vars.get("dry_run", ""))


def _num(v, default: float) -> float:
    if v is None or v == "":
        return float(default)
    return float(v)


# ---- JSON pointer matching ----------------------------------------------------------------------------------------
_SEG = re.compile(r"^(?P<name>[^\[]*)(?:\[(?P<key>[^=\]]+)=(?P<val>[^\]]*)\])?$")
_MISSING = object()


def _unescape(s: str) -> str:
    return s.replace("~1", "/").replace("~0", "~")


def resolve(doc, pointer: str):
    """JSON pointer with the `[key=value]` list selector; returns _MISSING when a segment does not resolve."""
    if pointer in ("", "/"):
        return doc
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/': {pointer!r}")
    cur = doc
    for raw in pointer[1:].split("/"):
        m = _SEG.match(raw)
        name, key, val = m.group("name"), m.group("key"), m.group("val")
        if name == "*":   # every value of a mapping / of the list's elements, flattened one level
            if isinstance(cur, dict):
                cur = list(cur.values())
            elif isinstance(cur, list):
                flat = []
                for x in cur:
                    flat.extend(x.values() if isinstance(x, dict) else x if isinstance(x, list) else [x])
                cur = flat
            else:
                return _MISSING
        elif name != "":
            name = _unescape(name)
            if isinstance(cur, dict):
                if name not in cur:
                    return _MISSING
                cur = cur[name]
            elif isinstance(cur, list) and name.lstrip("-").isdigit():
                i = int(name)
                if not -len(cur) <= i < len(cur):
                    return _MISSING
                cur = cur[i]
            else:
                return _MISSING
        if key is not None:
            if not isinstance(cur, list):
                return _MISSING
            cur = next((x for x in cur if isinstance(x, dict) and _eq(x.get(_unescape(key), _MISSING), val)),
                       _MISSING)
            if cur is _MISSING:
                return _MISSING
    return cur


def _text(v) -> str:
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    return str(v)


def _eq(actual, expected) -> bool:
    if actual is _MISSING:
        return False
    if actual == expected and type(actual) is not bool and type(expected) is not bool:
        return True
    if isinstance(actual, bool) or isinstance(expected, bool):
        return _text(actual) == _text(expected)
    if isinstance(expected, str) or isinstance(actual, str):
        return _text(actual) == _text(expected)
    return actual == expected


OPS = ("regex", "eq", "ne", "gte", "lte", "gt", "lt", "in", "contains", "exists", "len", "where", "count")


def _is_op(expected) -> bool:
    return isinstance(expected, dict) and expected and all(k in OPS for k in expected)


def check_value(actual, expected) -> str | None:
    """None when `actual` satisfies `expected`, else a short reason."""
    if not _is_op(expected):
        return None if _eq(actual, expected) else f"{_show(actual)} != {_text(expected)}"
    ops = dict(expected)
    if "exists" in ops:
        want = truthy(ops.pop("exists"))
        if (actual is not _MISSING) != want:
            return "missing" if want else f"present ({_show(actual)})"
        if not ops:
            return None
    if actual is _MISSING:
        return "missing"
    if "where" in ops:
        if not isinstance(actual, list):
            return f"not a list ({_show(actual)})"
        where = ops.pop("where")
        actual = [x for x in actual if all(check_value(resolve(x, p), v) is None for p, v in where.items())]
        if "count" not in ops and "len" not in ops:
            ops["count"] = {"gte": 1}
    for op, want in ops.items():
        if op in ("count", "len"):
            n = len(actual) if isinstance(actual, (list, dict, str)) else _MISSING
            why = check_value(n, want)
            if why:
                return f"{op} {why}"
        elif op == "regex":
            if re.search(str(want), _text(actual)) is None:
                return f"{_show(actual)} !~ /{want}/"
        elif op == "eq":
            if not _eq(actual, want):
                return f"{_show(actual)} != {_text(want)}"
        elif op == "ne":
            if _eq(actual, want):
                return f"{_show(actual)} == {_text(want)}"
        elif op in ("gte", "lte", "gt", "lt"):
            try:
                a, w = float(actual), float(want)
            except (TypeError, ValueError):
                return f"{_show(actual)} is not a number"
            ok = {"gte": a >= w, "lte": a <= w, "gt": a > w, "lt": a < w}[op]
            if not ok:
                return f"{_show(actual)} not {op} {_text(want)}"
        elif op == "in":
            if not any(_eq(actual, w) for w in want):
                return f"{_show(actual)} not in {want}"
        elif op == "contains":
            if isinstance(actual, list):
                if not any(_eq(x, want) or (isinstance(want, dict) and isinstance(x, dict)
                                            and all(check_value(resolve(x, p), v) is None for p, v in want.items()))
                           for x in actual):
                    return f"no element {_text(want)}"
            elif str(want) not in _text(actual):
                return f"{_show(actual)} does not contain {want!r}"
    return None


def _show(v) -> str:
    if v is _MISSING:
        return "missing"
    s = _text(v)
    return s if len(s) <= 120 else s[:117] + "..."


def match_doc(doc, match: dict | None) -> list[str]:
    """Reasons why `doc` does not satisfy `match` (empty = matches)."""
    out = []
    for ptr, want in (match or {}).items():
        why = check_value(resolve(doc, ptr), want)
        if why:
            out.append(f"{ptr}: {why}")
    return out


def save_values(ctx, doc, save: dict | None) -> list[str]:
    done = []
    for name, ptr in (save or {}).items():
        v = resolve(doc, ptr)
        if v is not _MISSING:
            ctx.vars[name] = _text(v)
            done.append(f"{name}={_show(v)}")
    return done


# ---- shell / ssh ----------------------------------------------------------------------------------------------------
def is_local(host) -> bool:
    return host in (None, "", "local", "localhost-local")


def shell(ctx, cmd: str, host=None, timeout: float = 120, cwd=None, stdin: str | None = None,
          echo: bool = True) -> subprocess.CompletedProcess:
    """Run `cmd` with sh locally, or on `host` over ssh (BatchMode; `${ssh}` overrides the ssh command)."""
    if is_local(host):
        argv, shell_ = cmd, True
        where = ""
    else:
        ssh = shlex.split(str(ctx.vars.get("ssh") or "ssh -o BatchMode=yes -o ConnectTimeout=10"))
        argv, shell_ = [*ssh, str(host), cmd], False
        where = f"{host}: "
    if echo:
        _say(f"    $ {where}{cmd}")
    return subprocess.run(argv, shell=shell_, cwd=cwd or None, capture_output=True, text=True, errors="replace",
                          timeout=timeout, input=stdin)


def _tail(text: str, n: int = 6) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:])[-600:]


def parse_json_output(out: str, json_lines: bool = False):
    out = (out or "").strip()
    if json_lines:
        docs = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    docs.append(json.loads(line))
                except ValueError:
                    pass
        return docs
    try:
        return json.loads(out)
    except ValueError:
        pass
    for start in ("{", "["):   # tolerate a banner before the document
        i = out.find(start)
        if i >= 0:
            try:
                return json.loads(out[i:])
            except ValueError:
                continue
    lines = out.splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError(f"no JSON in the output: {_tail(out, 3)!r}")


# ---- HTTP -----------------------------------------------------------------------------------------------------------
def read_token(path) -> str:
    return Path(os.path.expanduser(str(path))).read_text().strip()


def tls_context(ca=None) -> ssl.SSLContext:
    """The engine's trust rule (cloud/client.py): only the pinned lab CA, hostname checked; no X509_STRICT, which
    Python 3.13's default context adds and which lab CAs made without key identifiers do not pass."""
    if not ca:
        return ssl.create_default_context()
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.minimum_version = ssl.TLSVersion.TLSv1_2
    c.load_verify_locations(cafile=os.path.expanduser(str(ca)))
    return c


def http(url: str, method: str = "GET", headers: dict | None = None, body=None, ca=None, token_file=None,
         timeout: float = 5.0) -> tuple[int, dict, bytes]:
    hdrs = {str(k): str(v) for k, v in (headers or {}).items()}
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode()
    if token_file:
        hdrs["Authorization"] = f"Bearer {read_token(token_file)}"
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
    ctx = tls_context(ca) if url.startswith("https:") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _status_ok(code: int, want) -> bool:
    if isinstance(want, list):
        return any(int(w) == code for w in want)
    return int(want) == code


def act_http_expect(ctx, p):
    want = p.get("status", 200)
    timeout, every = _num(p.get("timeout"), 10), _num(p.get("every"), 0.5)
    last = {"why": "no request"}

    def probe():
        try:
            code, _h, raw = http(p["url"], p.get("method", "GET"), p.get("headers"), p.get("body"), p.get("ca"),
                                 p.get("token_file"), _num(p.get("request_timeout"), 5))
        except (OSError, ValueError, urllib.error.URLError) as e:
            last["why"] = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            return None
        text = raw.decode(errors="replace")
        if not _status_ok(code, want):
            last["why"] = f"HTTP {code} (want {want}): {text[:200]}"
            return None
        doc = None
        if p.get("json_match") or p.get("save"):
            try:
                doc = json.loads(text)
            except ValueError:
                last["why"] = f"HTTP {code}, body is not JSON: {text[:120]!r}"
                return None
            why = match_doc(doc, p.get("json_match"))
            if why:
                last["why"] = f"HTTP {code}: " + "; ".join(why)
                return None
        if p.get("body_contains") and str(p["body_contains"]) not in text:
            last["why"] = f"HTTP {code}: body does not contain {p['body_contains']!r}"
            return None
        return {"code": code, "doc": doc}

    r = _wait(timeout, probe, every)
    if not r:
        return FAIL, f"{p.get('method', 'GET')} {p['url']}: {last['why']}"
    saved = save_values(ctx, r["doc"], p.get("save"))
    extra = f"; saved {', '.join(saved)}" if saved else ""
    return PASS, f"{p.get('method', 'GET')} {p['url']} -> {r['code']}" + (
        f", {len(p['json_match'])} field(s) match" if p.get("json_match") else "") + extra


# ---- command with JSON output ---------------------------------------------------------------------------------------
def act_cmd_json_expect(ctx, p):
    timeout, every = _num(p.get("timeout"), 0), _num(p.get("every"), 1)
    want_rc = p.get("expect_rc", 0)
    last = {"why": "not run"}
    first = {"echo": True}

    def probe():
        try:
            r = shell(ctx, p["cmd"], p.get("host"), _num(p.get("cmd_timeout"), 120), p.get("cwd"), echo=first["echo"])
        except subprocess.TimeoutExpired:
            last["why"] = "command timed out"
            return None
        first["echo"] = False
        if want_rc is not None and want_rc != "" and r.returncode != int(want_rc):
            last["why"] = f"exit {r.returncode} (want {want_rc}): {_tail(r.stderr or r.stdout)}"
            return None
        try:
            doc = parse_json_output(r.stdout, truthy(p.get("json_lines", False)))
        except ValueError as e:
            last["why"] = str(e)
            return None
        why = match_doc(doc, p.get("match"))
        if why:
            last["why"] = "; ".join(why)
            return None
        return {"doc": doc, "rc": r.returncode}

    r = _wait(timeout, probe, every) if timeout > 0 else probe()
    if not r:
        return FAIL, last["why"]
    saved = save_values(ctx, r["doc"], p.get("save"))
    n = len(p.get("match") or {})
    return PASS, f"exit {r['rc']}, {n} field(s) match" + (f"; saved {', '.join(saved)}" if saved else "")


# ---- flexengine status / history ------------------------------------------------------------------------------------
def _fe_get(ctx, p, path: str, cli: str):
    """GET a local-API document over the TCP bind (via api) or with the CLI (via cmd)."""
    via = p.get("via") or ("api" if (p.get("api") or ctx.vars.get("fe_api")) else "cmd")
    if via == "api":
        base = (p.get("api") or ctx.vars.get("fe_api") or "").rstrip("/")
        if not base:
            raise RuntimeError("no engine API: set fe_api (local.tcp bind, e.g. http://lattepanda.lab:8781)")
        code, _h, raw = http(base + path, token_file=p.get("token_file") or ctx.vars.get("fe_api_token_file"),
                             ca=p.get("ca"), timeout=_num(p.get("request_timeout"), 5))
        if code != 200:
            raise RuntimeError(f"GET {path}: HTTP {code} {raw[:200].decode(errors='replace')}")
        return json.loads(raw)
    if via in ("cmd", "ssh"):
        fe = p.get("cmd") or ctx.vars.get("fe")
        if not fe:
            raise RuntimeError("no engine CLI: set fe (e.g. 'sudo -u flex /opt/flexengine/bin/flexengine')")
        host = p.get("host", ctx.vars.get("fe_host"))
        r = shell(ctx, f"{fe} {cli}", host, _num(p.get("request_timeout"), 60), echo=False)
        if r.returncode != 0:
            raise RuntimeError(f"{fe} {cli}: exit {r.returncode}: {_tail(r.stderr or r.stdout)}")
        return parse_json_output(r.stdout)
    raise ValueError(f"via must be api or cmd, got {via!r}")


def _device(doc: dict, device: str):
    return next((d for d in doc.get("devices", []) if d.get("deviceId") == device), None)


def act_fe_status_expect(ctx, p):
    timeout, every = _num(p.get("timeout"), 60), _num(p.get("every"), 1)
    dev_id, dep_id = p["device"], p.get("deployment")
    last = {"why": "no answer"}

    def probe():
        try:
            doc = _fe_get(ctx, p, "/v1/status", "status --json")
        except Exception as e:  # noqa: BLE001  network, CLI, JSON: retry until the timeout
            last["why"] = f"{type(e).__name__}: {e}"
            return None
        dev = _device(doc, dev_id)
        if dev is None:
            last["why"] = f"device {dev_id} not in the status (devices: {[d.get('deviceId') for d in doc.get('devices', [])]})"
            return None
        why = match_doc(dev, {k if k.startswith("/") else "/" + k: v for k, v in (p.get("device_fields") or {}).items()})
        if not dep_id:
            why += match_doc(dev, {k if k.startswith("/") else "/" + k: v for k, v in (p.get("fields") or {}).items()})
            if why:
                last["why"] = f"{dev_id}: " + "; ".join(why)
                return None
            return {"dev": dev, "dep": None}
        dep = next((d for d in dev.get("deployments", []) if d.get("id") == dep_id), None)
        if truthy(p.get("absent", False)):
            if dep is None or dep.get("state") == "Removed":
                return {"dev": dev, "dep": dep or {"state": "absent"}}
            last["why"] = f"{dev_id}/{dep_id} still {dep.get('state')} {dep.get('version', '')}"
            return None
        if dep is None:
            last["why"] = f"{dev_id}/{dep_id} not deployed (deployments: {[d.get('id') for d in dev.get('deployments', [])]})"
            return None
        want = {"/" + k: p[k] for k in ("state", "version", "health") if p.get(k) not in (None, "")}
        want.update({k if k.startswith("/") else "/" + k: v for k, v in (p.get("fields") or {}).items()})
        why += match_doc(dep, want)
        if why:
            last["why"] = (f"{dev_id}/{dep_id} is {dep.get('state')} {dep.get('version', '')}"
                           f"{' (' + dep['reasonCode'] + ')' if dep.get('reasonCode') else ''}: " + "; ".join(why))
            return None
        return {"dev": dev, "dep": dep}

    r = _wait(timeout, probe, every)
    if not r:
        return FAIL, last["why"]
    dep = r["dep"]
    saved = save_values(ctx, dep if dep is not None else r["dev"], p.get("save"))
    if dep is None:
        return PASS, f"{dev_id}: manifestVersion {r['dev'].get('manifestVersion')}, onboarded " \
                     f"{_text(r['dev'].get('onboarded'))}" + (f"; saved {', '.join(saved)}" if saved else "")
    extra = "".join(f", {k} {_show(dep[k])}" for k in ("health", "reasonCode", "rolledBackTo", "failFast") if k in dep)
    return PASS, f"{dev_id}/{dep_id}: {dep.get('state')} {dep.get('version', '')}{extra}" + (
        f"; saved {', '.join(saved)}" if saved else "")


def _history(ctx, p, since: float):
    dev = p.get("device")
    q = {"since": f"{since:.6f}"}
    if dev:
        q["device"] = dev
    cli = f"history --json --since {since:.6f}" + (f" --device {shlex.quote(dev)}" if dev else "")
    doc = _fe_get(ctx, p, "/v1/history?" + urllib.parse.urlencode(q), cli)
    recs = []
    for e in doc if isinstance(doc, list) else []:
        rec = e.get("record", e) if isinstance(e, dict) else None
        if isinstance(rec, dict):
            recs.append({**rec, "deviceId": e.get("deviceId")})
    return sorted(recs, key=lambda r: (float(r.get("deviceTime") or 0), r.get("seq") or 0))


def act_fe_history_mark(ctx, p):
    last = {"why": ""}

    def probe():
        try:
            return {"recs": _history(ctx, p, 0.0)}
        except Exception as e:  # noqa: BLE001  the engine may still be starting: retry until the timeout
            last["why"] = f"{type(e).__name__}: {e}"
            return None
    r = _wait(_num(p.get("timeout"), 60), probe, 1.0)
    if r is None:
        return FAIL, f"history not readable: {last['why']}"
    recs = r["recs"]
    mark = max((float(r.get("deviceTime") or 0) for r in recs), default=0.0)
    ctx.marks["fe:" + p["name"]] = mark
    return PASS, f"mark {p['name']} at deviceTime {mark:.3f} ({len(recs)} record(s) before it)"


def _seq_match(recs: list[dict], pattern: list[dict]):
    found, i = [], 0
    for want in pattern:
        w = {"/" + k: v for k, v in want.items() if k not in ("fields",)}
        w.update({k if k.startswith("/") else "/" + k: v for k, v in (want.get("fields") or {}).items()})
        while i < len(recs) and match_doc(recs[i], {("/dep" if k == "/deployment" else k): v for k, v in w.items()}):
            i += 1
        if i >= len(recs):
            return found, False
        found.append(recs[i])
        i += 1
    return found, True


def act_fe_history_expect(ctx, p):
    key = "fe:" + str(p["since"])
    if key not in ctx.marks:
        return FAIL, f"no history mark {p['since']!r} (add a fe_history_mark step before)"
    since = ctx.marks[key]
    pattern = p["match"] if isinstance(p["match"], list) else [p["match"]]
    timeout = _num(p.get("timeout"), 60)
    last = {"why": "no answer", "found": []}

    def probe():
        try:
            recs = [r for r in _history(ctx, p, since) if float(r.get("deviceTime") or 0) > since]
        except Exception as e:  # noqa: BLE001
            last["why"] = f"{type(e).__name__}: {e}"
            return None
        found, ok = _seq_match(recs, pattern)
        if not ok:
            seen = ", ".join(f"{r.get('dep')} {r.get('state')} {r.get('version', '')}".strip() for r in recs[-6:])
            last["why"] = f"matched {len(found)}/{len(pattern)} of the sequence; last records: {seen or 'none'}"
            return None
        return found

    found = _wait(timeout, probe, _num(p.get("every"), 1))
    if not found:
        return FAIL, last["why"]
    dur = float(found[-1].get("deviceTime") or 0) - float(found[0].get("deviceTime") or 0)
    if p.get("save_duration"):
        ctx.vars[str(p["save_duration"])] = f"{dur:.3f}"
    steps = " -> ".join(f"{r.get('dep')} {r.get('state')} {r.get('version', '')}".strip() for r in found)
    detail = f"{steps}; {dur:.2f} s from the first to the last record"
    if p.get("max_duration") not in (None, "") and dur > float(p["max_duration"]):
        return FAIL, f"{detail} > max {p['max_duration']} s"
    if p.get("min_duration") not in (None, "") and dur < float(p["min_duration"]):
        return FAIL, f"{detail} < min {p['min_duration']} s"
    return PASS, detail


# ---- Zenoh ----------------------------------------------------------------------------------------------------------
def _decode_sample(payload: bytes, decode: str, model=None, work=None):
    decode = decode or "json"
    if decode == "raw":
        return payload.hex()
    if decode == "text":
        return payload.decode(errors="replace")
    if decode == "json":
        return json.loads(payload.decode())
    if decode.startswith("cdr:"):
        import dataclasses

        from flexengine2.gateway.relay import Codec
        from flexmodel.runtime import cdr

        if not model:
            raise ValueError("decode cdr:<type> needs `model` (the flexModel workspace with the type)")
        import hashlib
        import tempfile

        key = hashlib.sha256(str(Path(model).resolve()).encode()).hexdigest()[:12]   # never write into the model
        codec = Codec(Path(model), Path(work or Path(tempfile.gettempdir()) / f"flexhwtest-codec-{key}"))
        obj = cdr.decode(codec.cls(decode[4:]), payload)
        return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj
    raise ValueError(f"decode must be json, text, raw or cdr:<type>, got {decode!r}")


def act_zenoh_expect(ctx, p):
    import threading

    import zenoh

    conf = {"mode": p.get("mode", "client"), "connect": {"endpoints": [p["connect"]]},
            "scouting": {"multicast": {"enabled": False}}}
    if conf["mode"] != "client":
        conf["listen"] = {"endpoints": ["tcp/127.0.0.1:0"]}
    got, seen, lock = [], [], threading.Lock()
    match, decode = p.get("match"), p.get("decode", "json")

    def on_sample(sample):
        try:
            raw = bytes(sample.payload.to_bytes())
            val = _decode_sample(raw, decode, p.get("model"), p.get("work"))
        except Exception as e:  # noqa: BLE001
            val = f"<undecodable: {e}>"
        with lock:
            seen.append((str(sample.key_expr), val))
            if isinstance(match, dict):
                ok = isinstance(val, (dict, list)) and not match_doc(val, match)
            elif match:
                ok = re.search(str(match), _text(val)) is not None
            else:
                ok = True
            if ok:
                got.append((str(sample.key_expr), val))

    session = zenoh.open(zenoh.Config.from_json5(json.dumps(conf)))
    try:
        sub = session.declare_subscriber(p["key"], on_sample)
        r = _wait(_num(p.get("timeout"), 10), lambda: got[0] if got else None, 0.1)
        sub.undeclare()
    finally:
        session.close()
    if r:
        return PASS, f"{r[0]}: {_show(r[1])} ({len(seen)} sample(s))"
    last = f"; last: {seen[-1][0]} {_show(seen[-1][1])}" if seen else ""
    return FAIL, f"no sample on {p['key']} matching {match or 'anything'} within {p.get('timeout', 10)} s " \
                 f"({len(seen)} sample(s){last})"


# ---- SMP (mcumgr) ---------------------------------------------------------------------------------------------------
class _SerialPipe:
    """pyserial port with the socket calls SmpLink uses."""

    def __init__(self, port: str, baud: int):
        import serial

        self.s = serial.serial_for_url(port, baudrate=baud, timeout=0.2)

    def sendall(self, data: bytes):
        self.s.write(data)
        self.s.flush()

    def settimeout(self, t):
        self.s.timeout = max(0.01, min(float(t), 1.0))

    def recv(self, n: int) -> bytes:
        import socket as _s

        data = self.s.read(n)
        if not data:
            raise _s.timeout()
        return data

    def close(self):
        self.s.close()


def smp_link(p):
    from flexengine2.drivers.mcu.link import SmpLink

    t = _num(p.get("request_timeout"), 5)
    if p.get("tty"):
        link = SmpLink("serial", 0, timeout=t)
        link.sock = _SerialPipe(p["tty"], int(p.get("baud", 115200)))
        return link
    link = SmpLink(p["host"], int(p["port"]), timeout=t)
    link.connect(wait=_num(p.get("connect_wait"), 5))
    return link


def _hexify(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, dict):
        return {str(k): _hexify(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_hexify(x) for x in v]
    if hasattr(v, "name") and hasattr(v, "value") and not isinstance(v, (int, str)):
        return v.value
    return v


def smp_query(p):
    cmd = p.get("cmd", "echo")
    link = smp_link(p)
    try:
        if cmd == "echo":
            return {"echo": link.echo(str(p.get("text", "flexhwtest")))}
        if cmd == "image_list":
            return {"images": _hexify(link.image_state())}
        if cmd in ("flexmod_status", "flexmod_info"):
            return _hexify(link.flexmod(cmd.split("_", 1)[1]))
        raise ValueError(f"cmd must be echo, image_list, flexmod_status or flexmod_info, got {cmd!r}")
    finally:
        link.close()


def act_smp_probe(ctx, p):
    last = {"why": "no answer"}
    where = p.get("tty") or f"{p.get('host')}:{p.get('port')}"

    def probe():
        try:
            doc = smp_query(p)
        except Exception as e:  # noqa: BLE001  link errors: retry until the timeout
            last["why"] = f"{type(e).__name__}: {e}"
            return None
        why = match_doc(doc, p.get("match"))
        if why:
            last["why"] = "; ".join(why) + f" (answer: {_show(doc)})"
            return None
        return doc

    doc = _wait(_num(p.get("timeout"), 10), probe, _num(p.get("every"), 0.5))
    if doc is None:
        return FAIL, f"SMP {p.get('cmd', 'echo')} on {where}: {last['why']}"
    saved = save_values(ctx, doc, p.get("save"))
    return PASS, f"SMP {p.get('cmd', 'echo')} on {where}: {_show(doc)}" + (f"; saved {', '.join(saved)}" if saved else "")


# ---- PLCs -----------------------------------------------------------------------------------------------------------
def ads_read(p) -> dict:
    out = {}
    if p.get("hello_hash") not in (None, "") or not p.get("symbol"):
        from flexengine2.drivers.plc.verify import AdsReader

        cfg = {"amsNetId": p["net_id"], "ip": p["ip"], "amsPort": int(p.get("port", 851)),
               "localNetId": p.get("local_net_id") or None, "timeout": _num(p.get("request_timeout"), 2),
               "instance": p.get("instance", "MAIN.fbBridge")}
        h, how = AdsReader(cfg, {}).read_hash()
        out["hello"] = {"hash": h, "status": how}
    if p.get("symbol"):
        import pyads

        from flexengine2.drivers.plc import verify as v

        with v._ADS_LOCK:
            if not v._ADS_ROUTES:
                pyads.open_port()
                if p.get("local_net_id"):
                    pyads.set_local_address(p["local_net_id"])
            if (p["net_id"], p["ip"]) not in v._ADS_ROUTES:
                pyads.add_route(p["net_id"], p["ip"])
                v._ADS_ROUTES.add((p["net_id"], p["ip"]))
            conn = pyads.Connection(p["net_id"], int(p.get("port", 851)), p["ip"])
            conn.open()
            try:
                plc_type = getattr(pyads, str(p.get("plc_type", "PLCTYPE_BOOL")))
                out["symbol"] = {"name": p["symbol"], "value": conn.read_by_name(p["symbol"], plc_type)}
            finally:
                conn.close()
    return out


def act_ads_probe(ctx, p):
    last = {"why": "no answer"}

    def probe():
        try:
            doc = ads_read(p)
        except Exception as e:  # noqa: BLE001
            last["why"] = f"{type(e).__name__}: {e}"
            return None
        why = []
        if p.get("hello_hash") not in (None, "") and doc["hello"]["hash"] != p["hello_hash"]:
            why.append(f"Hello hash {doc['hello']['hash']} != {p['hello_hash']}")
        if p.get("symbol") and "equals" in p and not _eq(doc["symbol"]["value"], p["equals"]):
            why.append(f"{p['symbol']} = {_text(doc['symbol']['value'])} != {_text(p['equals'])}")
        if why:
            last["why"] = "; ".join(why)
            return None
        return doc

    doc = _wait(_num(p.get("timeout"), 10), probe, _num(p.get("every"), 0.5))
    if doc is None:
        return FAIL, f"ADS {p['net_id']}:{p.get('port', 851)}: {last['why']}"
    parts = []
    if "hello" in doc:
        parts.append(f"Hello {doc['hello']['hash'][:23]}... ({doc['hello']['status']})")
    if "symbol" in doc:
        parts.append(f"{doc['symbol']['name']} = {_text(doc['symbol']['value'])}")
    return PASS, f"ADS {p['net_id']}:{p.get('port', 851)}: " + ", ".join(parts)


def _hash_registers(iface: str) -> list[int]:
    raw = bytes.fromhex(iface.split("_", 1)[1])
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def modbus_read(p) -> tuple[str, list]:
    from pymodbus.client import ModbusTcpClient

    kind = next((k for k in ("holding", "coils", "input") if p.get(k) not in (None, "")), None)
    if kind is None:
        raise ValueError("modbus_expect needs holding, coils or input: [start, count]")
    start, count = (int(x) for x in p[kind])
    c = ModbusTcpClient(p["host"], port=int(p.get("port", 502)), timeout=_num(p.get("request_timeout"), 2), retries=0)
    try:
        if not c.connect():
            raise ConnectionError(f"Modbus {p['host']}:{p.get('port', 502)}: no connection")
        unit = int(p.get("unit", 1))
        if kind == "holding":
            r = c.read_holding_registers(start, count=count, device_id=unit)
        elif kind == "input":
            r = c.read_input_registers(start, count=count, device_id=unit)
        else:
            r = c.read_coils(start, count=count, device_id=unit)
        if r.isError():
            raise ConnectionError(f"Modbus {p['host']}:{p.get('port', 502)}: {r}")
        vals = list(r.registers) if kind != "coils" else [bool(b) for b in r.bits[:count]]
    finally:
        c.close()
    return kind, vals


def act_modbus_expect(ctx, p):
    want = p["equals"]
    if isinstance(want, str) and want.startswith("FLEX01_"):
        want = _hash_registers(want)
    last = {"why": "no answer"}

    def probe():
        try:
            kind, vals = modbus_read(p)
        except Exception as e:  # noqa: BLE001
            last["why"] = f"{type(e).__name__}: {e}"
            return None
        if not isinstance(want, list):
            ok = len(vals) == 1 and _eq(vals[0], want)
        else:
            ok = len(want) == len(vals) and all(_eq(a, b) for a, b in zip(vals, want))
        if not ok:
            last["why"] = f"{kind} {p[kind]} = {vals} != {want}"
            return None
        return kind, vals

    r = _wait(_num(p.get("timeout"), 10), probe, _num(p.get("every"), 0.5))
    if not r:
        return FAIL, f"Modbus {p['host']}:{p.get('port', 502)}: {last['why']}"
    kind, vals = r
    shown = p["equals"] if isinstance(p["equals"], str) else vals
    return PASS, f"Modbus {p['host']}:{p.get('port', 502)} {kind} {p[kind]} = {_show(shown)}"


# ---- ssh / journal --------------------------------------------------------------------------------------------------
def act_ssh_run(ctx, p):
    try:
        r = shell(ctx, p["cmd"], p["host"], _num(p.get("timeout"), 120), p.get("cwd"))
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {p.get('timeout', 120)} s"
    for line in (r.stdout or "").strip().splitlines()[-15:]:
        _say("    | " + line)
    want = p.get("expect_rc", 0)
    if want is not None and want != "" and r.returncode != int(want):
        return FAIL, f"exit {r.returncode} (want {want}): {_tail(r.stderr or r.stdout)}"
    detail = f"exit {r.returncode}"
    if p.get("match"):
        m = re.search(str(p["match"]), r.stdout or "", re.M)
        if not m:
            return FAIL, f"{detail}, no line matching /{p['match']}/: {_tail(r.stdout)}"
        detail += f", matched {m.group(0)[:120]!r}"
        for name, grp in (p.get("save") or {}).items():
            ctx.vars[name] = m.group(grp if not str(grp).isdigit() else int(grp))
    return PASS, detail


def _source(p, ctx):
    return str(p.get("source") or ctx.vars.get("journal_source") or "journal")


def act_journal_mark(ctx, p):
    src = _source(p, ctx)
    if src.startswith("file:"):
        path = Path(src[5:])
        pos = path.stat().st_size if path.exists() else 0
        ctx.marks["journal:" + p["name"]] = {"source": src, "pos": pos}
        return PASS, f"mark {p['name']} at byte {pos} of {path}"
    r = shell(ctx, "date +%s.%N", p.get("host", ctx.vars.get("fe_host")), 30, echo=False)
    if r.returncode != 0:
        return FAIL, f"date on the target: exit {r.returncode}: {_tail(r.stderr)}"
    t = float(r.stdout.strip())
    ctx.marks["journal:" + p["name"]] = {"source": src, "since": t, "unit": p.get("unit", "flexengine"),
                                          "host": p.get("host", ctx.vars.get("fe_host"))}
    return PASS, f"mark {p['name']} at {t:.3f} (target clock)"


def _journal_lines(ctx, mark: dict, p) -> list[str]:
    src = mark["source"]
    if src.startswith("file:"):
        path = Path(src[5:])
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(mark["pos"])
            return f.read().decode(errors="replace").splitlines()
    unit = p.get("unit") or mark.get("unit") or "flexengine"
    host = p.get("host", mark.get("host"))
    cmd = f"journalctl -u {shlex.quote(unit)} -o cat --no-pager --since @{mark['since']:.6f}"
    r = shell(ctx, cmd, host, 60, echo=False)
    if r.returncode != 0:
        raise RuntimeError(f"journalctl: exit {r.returncode}: {_tail(r.stderr)}")
    return r.stdout.splitlines()


def _line_matches(line: str, pattern, fields) -> bool:
    if pattern and not re.search(str(pattern), line):
        return False
    if fields:
        try:
            doc = json.loads(line)
        except ValueError:
            return False
        if not isinstance(doc, dict) or match_doc(doc, {k if k.startswith("/") else "/" + k: v
                                                        for k, v in fields.items()}):
            return False
    return True


def act_journal_expect(ctx, p):
    mark = ctx.marks.get("journal:" + str(p["since"]))
    if mark is None:
        return FAIL, f"no journal mark {p['since']!r} (add a journal_mark step before)"
    if not (p.get("pattern") or p.get("fields")):
        return FAIL, "journal_expect needs pattern or fields"
    want = int(p.get("count", 1))
    absent = truthy(p.get("absent", False))
    last = {"n": 0, "lines": 0, "hit": None}

    def probe():
        try:
            lines = _journal_lines(ctx, mark, p)
        except Exception as e:  # noqa: BLE001
            last["why"] = str(e)
            return None
        hits = [l for l in lines if _line_matches(l, p.get("pattern"), p.get("fields"))]
        last.update(n=len(hits), lines=len(lines), hit=hits[0] if hits else None)
        if absent:
            return None if not hits else "hit"
        return hits[0] if len(hits) >= want else None

    what = p.get("pattern") or p.get("fields")
    r = _wait(_num(p.get("timeout"), 30), probe, _num(p.get("every"), 0.5))
    if absent:
        if r:
            return FAIL, f"unexpected log line for {what}: {last['hit'][:200]}"
        return PASS, f"no line matching {what} in {last['lines']} line(s) for {p.get('timeout', 30)} s"
    if not r:
        return FAIL, f"{last['n']}/{want} line(s) matching {what} in {last['lines']} line(s) since the mark" + (
            f" ({last['why']})" if last.get("why") else "")
    return PASS, f"{last['n']} line(s) matching {what}: {r.strip()[:200]}"


# ---- write_file (superset of the runner's) / manual (dry run) / power_cycle ----------------------------------------------
def act_write_file(ctx, p):
    content = p.get("content")
    if "json" in p:
        content = json.dumps(p["json"], indent=1) + "\n"
    if content is None:
        return FAIL, "write_file needs content or json"
    sudo = truthy(p.get("sudo", False)) and os.geteuid() != 0 if is_local(p.get("host")) else truthy(p.get("sudo", False))
    plain = not p.get("mode") and "json" not in p and not truthy(p.get("sudo", False))
    if is_local(p.get("host")) and plain and "write_file" in _ORIG:
        return _ORIG["write_file"](ctx, {"path": p["path"], "content": content})
    if is_local(p.get("host")) and not sudo:
        path = Path(os.path.abspath(os.path.expanduser(str(p["path"]))))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if p.get("mode"):
            os.chmod(path, int(str(p["mode"]), 8))
        return PASS, f"wrote {path}" + (f" (mode {p['mode']})" if p.get("mode") else "")
    q = shlex.quote(str(p["path"]))
    pre = "sudo " if sudo else ""
    cmd = f"{pre}mkdir -p \"$(dirname {q})\" && {pre}tee {q} >/dev/null" + (
        f" && {pre}chmod {shlex.quote(str(p['mode']))} {q}" if p.get("mode") else "")
    r = shell(ctx, cmd, p.get("host"), 60, stdin=content)
    where = f"{p['host']}:" if not is_local(p.get("host")) else ""
    if r.returncode != 0:
        return FAIL, f"{where}{p['path']}: exit {r.returncode}: {_tail(r.stderr)}"
    return PASS, f"wrote {where}{p['path']}" + (" (sudo)" if sudo else "")


def act_manual(ctx, p):
    if not dry_run(ctx):
        return _ORIG["manual"](ctx, {k: v for k, v in p.items() if k in ("instruction", "confirm")})
    _say("    >> " + str(p["instruction"]).strip().replace("\n", "\n       "))
    did = ""
    if p.get("sim"):
        r = shell(ctx, str(p["sim"]), None, _num(p.get("sim_timeout"), 300))
        if r.returncode != 0:
            return FAIL, f"dry run: simulation `{p['sim']}` exit {r.returncode}: {_tail(r.stderr or r.stdout)}"
        did = f"simulated ({_tail(r.stdout, 1) or 'exit 0'}), "
    answer = str(p.get("sim_answer", "y")).lower()
    if p.get("confirm"):
        if answer == "y":
            return PASS, f"dry run: {did}answered yes"
        return FAIL, f"dry run: {did}answered no"
    return PASS, f"dry run: {did}done"


def _tcp_up(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return True
    except OSError:
        return False


def act_power_cycle(ctx, p):
    timeout = _num(p.get("timeout"), 300)
    up = p.get("wait_up") or {}
    _say("    >> " + str(p["instruction"]).strip().replace("\n", "\n       "))
    t0 = time.time()
    saw_down = {"v": False}
    if dry_run(ctx):
        if not p.get("simulate"):
            return FAIL, "dry run: no `simulate` command for this power cycle"
        r = shell(ctx, str(p["simulate"]), None, timeout)
        if r.returncode != 0:
            return FAIL, f"dry run: simulation exit {r.returncode}: {_tail(r.stderr or r.stdout)}"
        saw_down["v"] = True     # the simulation took the target down (it returns when it is powered again)
        how = "simulated"
    else:
        if up and truthy(p.get("wait_down", True)):
            if _wait(min(timeout, 120), lambda: not _tcp_up(up["host"], up["port"]), 0.5):
                saw_down["v"] = True
                _say(f"    i  {up['host']}:{up['port']} went down")
        a = R.ask("power restored? press Enter (s = skip)", default="") if R is not None else ""
        if str(a).strip().lower() == "s":
            return SKIP, "skipped by operator"
        how = "by hand"
    if up:
        if not saw_down["v"] and truthy(p.get("wait_down", True)):
            return FAIL, f"{up['host']}:{up['port']} never went down: was the power really cut?"
        if not _wait(max(1.0, timeout - (time.time() - t0)), lambda: _tcp_up(up["host"], up["port"]), 0.5):
            return FAIL, f"{how}; {up['host']}:{up['port']} not back within {timeout:.0f} s"
        return PASS, f"power cycled ({how}); {up['host']}:{up['port']} back after {time.time() - t0:.1f} s"
    return PASS, f"power cycled ({how})"


ACTIONS = {name: _keyed(fn) for name, fn in {
    "http_expect": act_http_expect, "cmd_json_expect": act_cmd_json_expect,
    "fe_status_expect": act_fe_status_expect, "fe_history_mark": act_fe_history_mark,
    "fe_history_expect": act_fe_history_expect, "zenoh_expect": act_zenoh_expect, "smp_probe": act_smp_probe,
    "ads_probe": act_ads_probe, "modbus_expect": act_modbus_expect, "ssh_run": act_ssh_run,
    "journal_mark": act_journal_mark, "journal_expect": act_journal_expect, "write_file": act_write_file,
    "manual": act_manual, "power_cycle": act_power_cycle,
}.items()}
NEW = sorted(k for k in ACTIONS if k not in ("write_file", "manual"))

# ---- parameter schemas (for `--list` validation): required, optional -------------------------------------------------
_SAVE = {"save"}
PARAMS = {
    # the runner's own types (tools/hwtest/runbook.py docstring)
    "check_tcp": ({"host", "port"}, set()),
    "run": ({"cmd"}, {"cwd", "timeout"}),
    "upload": (set(), {"platformio", "env", "arduino_cli", "fqbn", "libraries", "timeout"}),
    "serial_open": (set(), {"baudrate"}),
    "serial_expect": ({"pattern"}, {"timeout"}),
    "redis_expect": ({"key"}, {"equals", "matches", "contains", "db", "since_mark", "timeout"}),
    "redis_mark": ({"name", "key"}, {"db"}),
    "mqtt_subscribe": ({"name", "topic"}, set()),
    "mqtt_publish": ({"topic", "payload"}, set()),
    "mqtt_expect": ({"capture"}, {"match", "timeout"}),
    "mqtt_expect_none": ({"capture"}, {"match", "duration"}),
    "wait": ({"seconds"}, {"message"}),
    "serve": ({"dir"}, {"port"}),
    "manual": ({"instruction"}, {"confirm", "sim", "sim_answer", "sim_timeout"}),
    "write_file": ({"path"}, {"content", "json", "mode", "host", "sudo"}),
    # flex step types
    "http_expect": ({"url"}, {"method", "headers", "body", "token_file", "ca", "status", "json_match", "body_contains",
                              "timeout", "every", "request_timeout"} | _SAVE),
    "cmd_json_expect": ({"cmd"}, {"host", "cwd", "match", "expect_rc", "json_lines", "timeout", "every",
                                  "cmd_timeout"} | _SAVE),
    "fe_status_expect": ({"device"}, {"deployment", "state", "version", "health", "fields", "device_fields", "absent",
                                      "via", "api", "token_file", "ca", "cmd", "host", "timeout", "every",
                                      "request_timeout"} | _SAVE),
    "fe_history_mark": ({"name"}, {"device", "via", "api", "token_file", "ca", "cmd", "host", "request_timeout",
                                   "timeout"}),
    "fe_history_expect": ({"since", "match"}, {"device", "max_duration", "min_duration", "save_duration", "timeout",
                                               "every", "via", "api", "token_file", "ca", "cmd", "host",
                                               "request_timeout"}),
    "zenoh_expect": ({"connect", "key"}, {"match", "decode", "model", "work", "mode", "timeout"}),
    "smp_probe": ({"cmd"}, {"tty", "baud", "host", "port", "match", "text", "timeout", "every", "request_timeout",
                            "connect_wait"} | _SAVE),
    "ads_probe": ({"net_id", "ip"}, {"port", "local_net_id", "instance", "hello_hash", "symbol", "plc_type", "equals",
                                     "timeout", "every", "request_timeout"}),
    "modbus_expect": ({"host", "equals"}, {"port", "unit", "holding", "coils", "input", "timeout", "every",
                                           "request_timeout"}),
    "ssh_run": ({"host", "cmd"}, {"expect_rc", "match", "timeout", "cwd"} | _SAVE),
    "journal_mark": ({"name"}, {"unit", "host", "source"}),
    "journal_expect": ({"since"}, {"pattern", "fields", "count", "absent", "unit", "host", "source", "timeout",
                                   "every"}),
    "power_cycle": ({"instruction"}, {"wait_up", "wait_down", "simulate", "timeout"}),
}
STEP_KEYS = {"id", "title", "note", "skippable"}
BUILTIN_VARS = {"local_ip", "python", "serial_port", "hwtest", "flexengine", "repo", "project_dir", "results_dir"}
_VAR = re.compile(r"\$\{(\w+)(\|commas)?\}")


def _refs(value) -> set[str]:
    if isinstance(value, str):
        return {m.group(1) for m in _VAR.finditer(value)}
    if isinstance(value, list):
        return set().union(*(_refs(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_refs(v) | _refs(k) for k, v in value.items())) if value else set()
    return set()


def validate(rb: dict, actions=None) -> list[str]:
    """Format problems of a loaded runbook beyond the runner's own check (one step type per step): parameters of
    every step type, `${var}` references, marks used before they are set."""
    actions = actions or (R.ACTIONS if R is not None else ACTIONS)
    errs = []
    for k in ("id", "title", "steps"):
        if k not in rb:
            errs.append(f"runbook: missing `{k}`")
    known = set(BUILTIN_VARS) | set((rb.get("vars") or {}).keys())
    for name, spec in (rb.get("vars") or {}).items():
        if isinstance(spec, dict):
            bad = set(spec) - {"default", "prompt"}
            if bad:
                errs.append(f"var {name}: unknown keys {sorted(bad)}")
    for ref in _refs(rb.get("setup") or []) | _refs(rb.get("description") or "") | _refs(
            [s.get("default") if isinstance(s, dict) else s for s in (rb.get("vars") or {}).values()]):
        if ref not in known:
            errs.append(f"setup/vars: ${{{ref}}} is not a variable")
    marks = {"fe": set(), "journal": set(), "redis": set(), "mqtt": set()}
    for step in rb.get("steps") or []:
        sid = step.get("id", "?")
        kinds = [k for k in step if k in actions]
        if len(kinds) != 1:
            errs.append(f"{sid}: needs exactly one step type, has {kinds or 'none'}")
            continue
        kind = kinds[0]
        extra = set(step) - STEP_KEYS - {kind}
        if extra:
            errs.append(f"{sid}: unknown step keys {sorted(extra)}")
        params = step[kind]
        if kind == "wait" and not isinstance(params, dict):
            params = {"seconds": params}
        if kind in PARAMS:
            req, opt = PARAMS[kind]
            if not isinstance(params, dict):
                errs.append(f"{sid}: {kind} needs a mapping")
                continue
            miss, unknown = req - set(params), set(params) - req - opt
            if miss:
                errs.append(f"{sid}: {kind} misses {sorted(miss)}")
            if unknown:
                errs.append(f"{sid}: {kind} has unknown parameters {sorted(unknown)}")
            if kind == "smp_probe" and not (params.get("tty") or (params.get("host") and params.get("port"))):
                errs.append(f"{sid}: smp_probe needs tty or host + port")
            if kind == "modbus_expect" and not any(params.get(k) is not None for k in ("holding", "coils", "input")):
                errs.append(f"{sid}: modbus_expect needs holding, coils or input")
            if kind == "fe_status_expect" and params.get("via") not in (None, "", "api", "cmd", "ssh") \
                    and "${" not in str(params.get("via")):
                errs.append(f"{sid}: via must be api or cmd")
            if kind == "fe_history_expect":
                for m in params.get("match") if isinstance(params.get("match"), list) else [params.get("match")]:
                    if not isinstance(m, dict) or not ({"dep", "deployment"} & set(m)) or "state" not in m:
                        errs.append(f"{sid}: fe_history_expect match items need dep and state")
                if params.get("since") not in marks["fe"]:
                    errs.append(f"{sid}: history mark {params.get('since')!r} is not set by an earlier fe_history_mark")
            if kind == "journal_expect" and params.get("since") not in marks["journal"]:
                errs.append(f"{sid}: journal mark {params.get('since')!r} is not set by an earlier journal_mark")
            if kind == "journal_expect" and not (params.get("pattern") or params.get("fields")):
                errs.append(f"{sid}: journal_expect needs pattern or fields")
            if kind == "redis_expect" and params.get("since_mark") and params["since_mark"] not in marks["redis"]:
                errs.append(f"{sid}: redis mark {params['since_mark']!r} is not set by an earlier redis_mark")
            if kind in ("mqtt_expect", "mqtt_expect_none") and params.get("capture") not in marks["mqtt"]:
                errs.append(f"{sid}: capture {params.get('capture')!r} is not started by an earlier mqtt_subscribe")
            for pat_key in ("pattern",) if kind in ("journal_expect", "serial_expect") else ():
                if params.get(pat_key):
                    try:
                        re.compile(str(params[pat_key]))
                    except re.error as e:
                        errs.append(f"{sid}: bad regex {params[pat_key]!r}: {e}")
            if kind == "write_file" and params.get("content") is None and "json" not in params:
                errs.append(f"{sid}: write_file needs content or json")
        for ref in _refs(params) | _refs(step.get("title", "")) | _refs(step.get("note", "")):
            if ref not in known:
                errs.append(f"{sid}: ${{{ref}}} is not a variable (vars, built-ins or an earlier save)")
        if isinstance(params, dict):
            known |= set((params.get("save") or {}).keys())
            if params.get("save_duration"):
                known.add(str(params["save_duration"]))
            if kind == "fe_history_mark":
                marks["fe"].add(params.get("name"))
            if kind == "journal_mark":
                marks["journal"].add(params.get("name"))
            if kind == "redis_mark":
                marks["redis"].add(params.get("name"))
            if kind == "mqtt_subscribe":
                marks["mqtt"].add(params.get("name"))
    return errs


def register(runbook_module) -> None:
    """Add the flex step types to the runner's ACTIONS (idempotent)."""
    global R
    R = runbook_module
    acts = runbook_module.ACTIONS
    for name in ("manual", "write_file"):
        cur = acts.get(name)
        if cur is not None and cur is not ACTIONS[name]:
            _ORIG[name] = cur
    acts.update(ACTIONS)

"""Engine configuration: load, convert, validate (EV-04).

    cfg = load("engine.yaml")          # parse + ${VAR} expansion + plan-form conversion + file resolution
    errors = validate(cfg)             # [{"path": "/devices/1/mcu/smp", "message": "..."}], [] = valid
    report = check("engine.yaml")      # what `flexengine config check` prints

The canonical form is the one of demo/edge/edge.example.yaml; see the `$comment` of schema.json for the decision and
`from_plan_v2()` for the converter of the docs/flexengine-v2-plan.md §3.6 form.

Order: ${VAR} references are expanded in the file text (os.path.expandvars semantics: unset variables stay as they
are and are then reported by validate()), the text is parsed (JSON for *.json, else YAML), a plan-§3.6 document is
converted, and only then validated. load() validates before it reads the referenced files (cloud.stackInfo,
cloud.releaseKeyFiles), so a bad config fails with the schema error, not a traceback. devices[].onboardingTokenFile is
consumed by the engine at onboarding (EV-06), never copied into the config.
"""

from __future__ import annotations

import copy
import json
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

SCHEMA_PATH = Path(__file__).with_name("schema.json")
PLAN_API_VERSION = "flexengine/v2alpha1"
PLAN_ONLY_TOP = ("apiVersion", "identity", "observability", "safeState")  # `local` is in both forms (EV-07)
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Invalid or unreadable config; `.errors` = [{"path": <JSON pointer>, "message": str}]."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("invalid engine config:\n" + "\n".join(format_error(e) for e in errors))


def format_error(e: dict) -> str:
    return f"{e['path'] or '/'}: {e['message']}"


def _ptr(parts) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema())
    return Draft202012Validator(schema())


# ---- parse ------------------------------------------------------------------------------------------------------------
def _comment_start(line: str) -> int:
    """Index of a YAML comment (`#` at line start or after whitespace, outside quotes), else -1."""
    q = None
    for i, c in enumerate(line):
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return i
    return -1


def expand(text: str, yaml_comments: bool = False) -> str:
    """${VAR} / $VAR from the environment; unset ones stay (validate() reports them). With yaml_comments, text in
    YAML comments is left as is, so a variable mentioned in a comment never breaks the config."""
    if not yaml_comments:
        return os.path.expandvars(text)
    out = []
    for line in text.splitlines(keepends=True):
        i = _comment_start(line)
        out.append(os.path.expandvars(line) if i < 0 else os.path.expandvars(line[:i]) + line[i:])
    return "".join(out)


def parse(path: str | Path, notes: list[str] | None = None) -> dict:
    """Read, expand, parse and (for a plan-§3.6 document) convert; no validation, no file resolution."""
    p = Path(path)
    try:
        text = expand(p.read_text(), yaml_comments=p.suffix != ".json")
    except OSError as e:
        raise ConfigError([{"path": "", "message": f"cannot read {p}: {e.strerror or e}"}]) from e
    try:
        cfg = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as e:
        unset = [{"path": "", "message": f"environment variable {m.group(1)} is not set (line {text.count(chr(10), 0, m.start()) + 1})"}
                 for m in _VAR.finditer(text)]
        raise ConfigError(unset + [{"path": "", "message": f"{p} is not valid {'JSON' if p.suffix == '.json' else 'YAML'}"
                                                           f"{' after ${VAR} expansion' if unset else ''}: "
                                                           f"{' '.join(str(e).split())}"}]) from e
    if not isinstance(cfg, dict):
        raise ConfigError([{"path": "", "message": f"{p}: the config must be a mapping, got {_tname(cfg)}"}])
    if is_plan_form(cfg):
        cfg, n = from_plan_v2(cfg)
        if notes is not None:
            notes.extend(n)
    return cfg


def is_plan_form(cfg: dict) -> bool:
    return isinstance(cfg, dict) and any(k in cfg for k in PLAN_ONLY_TOP)


# ---- converter: docs/flexengine-v2-plan.md §3.6 form -> canonical form -------------------------------------------
def from_plan_v2(doc: dict) -> tuple[dict, list[str]]:
    """Convert the plan-§3.6 form to the canonical form. Returns (cfg, notes); notes name what was dropped because
    alpha does not implement it (the result behaves like alpha would). Raises ConfigError where dropping would change
    what the operator asked for in a way that matters (another trust store, cosign trust, a non-file provider)."""
    cfg = copy.deepcopy(doc)
    notes: list[str] = []
    errors: list[dict] = []
    av = cfg.pop("apiVersion", PLAN_API_VERSION)
    if av != PLAN_API_VERSION:
        errors.append({"path": "/apiVersion", "message": f"unsupported apiVersion {av!r} (expected {PLAN_API_VERSION})"})
    cl = cfg.get("cloud")
    if isinstance(cl, dict):
        trust = cl.pop("releaseTrust", None)
        if trust is not None:
            if not isinstance(trust, dict):
                errors.append({"path": "/cloud/releaseTrust", "message": f"must be a mapping, got {_tname(trust)}"})
            else:
                extra = sorted(set(trust) - {"ed25519", "cosign"})
                if extra:
                    errors.append({"path": "/cloud/releaseTrust", "message": f"unknown key(s) {', '.join(extra)}"})
                if trust.get("cosign"):
                    errors.append({"path": "/cloud/releaseTrust/cosign",
                                   "message": "cosign release trust is a beta feature; alpha trusts ed25519 keys only"})
                keys = trust.get("ed25519") or []
                cl["releasePublicKeys"] = list(cl.get("releasePublicKeys") or []) + list(keys if isinstance(keys, list) else [keys])
    ident = cfg.pop("identity", None)
    if ident is not None:
        ks = ident.get("keyStore", "file") if isinstance(ident, dict) else ident
        if ks != "file" or (isinstance(ident, dict) and set(ident) - {"keyStore"}):
            errors.append({"path": "/identity", "message": "only identity.keyStore: file is available in alpha"})
    loc = cfg.get("local")
    if isinstance(loc, dict) and "http" in loc:
        # plan form `local: {socket, http: {bind, token}}` -> canonical `local: {socket, group?, tcp: {bind, tokenFile}}`
        http = loc.pop("http")
        http = http if isinstance(http, dict) else {}
        if http.get("token"):
            errors.append({"path": "/local/http/token",
                           "message": "the TCP bearer token must come from a file: use local.tcp.tokenFile"})
        if http.get("bind"):
            loc["tcp"] = {"bind": http["bind"], **({"tokenFile": http["tokenFile"]} if http.get("tokenFile") else {})}
        else:
            notes.append("/local http with an empty bind = no TCP listener (canonical form: local.tcp {bind, tokenFile})")
    obs = cfg.pop("observability", None)
    if obs is not None:
        # plan form `observability: {logFormat, otlpEndpoint, history}` -> canonical logFormat / otel / historyMax
        if not isinstance(obs, dict):
            errors.append({"path": "/observability", "message": f"must be a mapping, got {_tname(obs)}"})
        else:
            extra = sorted(set(obs) - {"logFormat", "otlpEndpoint", "history"})
            if extra:
                errors.append({"path": "/observability", "message": f"unknown key(s) {', '.join(extra)}"})
            if "logFormat" in obs:
                cfg.setdefault("logFormat", obs["logFormat"])
            if obs.get("otlpEndpoint"):
                cfg.setdefault("otel", {"enabled": True, "endpoint": obs["otlpEndpoint"]})
            if "history" in obs:
                cfg.setdefault("historyMax", obs["history"])
    ss = cfg.pop("safeState", None)
    if ss is not None:
        if not isinstance(ss, dict) or ss.get("provider", "file") != "file":
            errors.append({"path": "/safeState/provider", "message": "only safeState.provider: file is available in alpha"})
        else:
            extra = sorted(set(ss) - {"provider", "file", "staleAfter"})
            if extra:
                errors.append({"path": "/safeState", "message": f"unknown key(s) {', '.join(extra)}"})
            if ss.get("file") and isinstance(cl, dict):
                cl.setdefault("safeStateFile", ss["file"])
            if "staleAfter" in ss:
                notes.append("/safeState/staleAfter dropped: signal staleness is not checked in alpha")
    for i, dev in enumerate(cfg.get("devices") or []):
        drv = dev.get("drivers") if isinstance(dev, dict) else None
        if not isinstance(drv, dict):
            continue
        nat = drv.get("native")
        if isinstance(nat, dict):
            mode = nat.get("mode", "systemd")
            if mode == "process":
                drv["native"] = True
            else:
                del drv["native"]
                notes.append(f"/devices/{i}/drivers/native (mode {mode}) dropped: the systemd native driver is not "
                             "in alpha")
        if "os-rauc" in drv:
            r = drv["os-rauc"]
            if isinstance(r, dict) and r.get("sharedDataPath"):
                plan_only = sorted(set(r) & {"bundleDir", "commitTimeout"})
                drv["os-rauc"] = {k: v for k, v in r.items() if k not in plan_only}
                if plan_only:
                    notes.append(f"/devices/{i}/drivers/os-rauc/{plan_only[0]} dropped: not a key of the os-rauc "
                                 "driver (bundles stay in the engine's store; the gate timeout is the release's)")
            else:
                del drv["os-rauc"]
                notes.append(f"/devices/{i}/drivers/os-rauc dropped: the plan form names no sharedDataPath (the data "
                             "partition both slots share); set drivers.os-rauc.sharedDataPath")
    if errors:
        raise ConfigError(errors)
    return cfg, notes


# ---- validate ---------------------------------------------------------------------------------------------------------
def _tname(v) -> str:
    return {dict: "a mapping", list: "a list", str: "a string", bool: "a boolean", int: "an integer",
            float: "a number", type(None): "null"}.get(type(v), type(v).__name__)


_TYPES = {"object": "a mapping", "array": "a list", "string": "a string", "boolean": "a boolean",
          "integer": "an integer", "number": "a number", "null": "null"}


def _allowed(e) -> list[str]:
    return sorted((e.schema.get("properties") or {}).keys())


def _plain(e) -> list[tuple[str, str]]:
    """jsonschema error -> [(pointer, message)] in plain words."""
    path = _ptr(e.absolute_path)
    v, inst = e.validator, e.instance
    if v == "additionalProperties":
        if e.schema.get("additionalProperties") is False and isinstance(inst, dict):
            allowed = _allowed(e)
            return [(f"{path}{_ptr([k])}", f"unknown key {k!r}" + (f" (allowed: {', '.join(allowed)})" if allowed else ""))
                    for k in sorted(set(inst) - set(allowed))]
    if v == "required":
        missing = [k for k in e.validator_value if isinstance(inst, dict) and k not in inst]
        return [(path, f"missing required key {k!r}") for k in missing]
    if v == "dependentRequired":
        msgs = []
        for k, deps in e.validator_value.items():
            for d in deps:
                if isinstance(inst, dict) and k in inst and d not in inst:
                    why = {("mcu", "parent"): " (an MCU child is attached to a gateway)",
                           ("parent", "mcu"): " (only MCU children have a parent in alpha)",
                           ("relay", "mcu"): " (the relay runs on an MCU child)"}.get((k, d), "")
                    msgs.append((path, f"{k!r} requires {d!r}{why}"))
        return msgs
    if v == "type":
        want = e.validator_value if isinstance(e.validator_value, list) else [e.validator_value]
        return [(path, f"must be {' or '.join(_TYPES.get(t, t) for t in want)}, got {_tname(inst)}")]
    if v == "enum":
        return [(path, f"must be one of {', '.join(map(str, e.validator_value))}; got {inst!r}")]
    if v == "const":
        return [(path, f"must be {e.validator_value!r}; got {inst!r}")]
    if v == "pattern":
        hint = (e.schema.get("description") or "").split(".")[0]
        return [(path, f"{inst!r} is not a valid value" + (f": {hint}" if hint else f" (pattern {e.validator_value})"))]
    if v in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        op = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}[v]
        return [(path, f"must be {op} {e.validator_value}; got {inst!r}")]
    if v == "minLength":
        return [(path, "must not be empty")]
    if v in ("minItems", "maxItems"):
        n = e.validator_value
        return [(path, f"must have {'at least' if v == 'minItems' else 'at most'} {n} item(s); got {len(inst)}")]
    if v == "items" and e.validator_value is False:
        return [(path, f"has too many items ({len(inst)})")]
    return [(path, e.message)]


def _env_refs(node, parts=()) -> list[tuple[str, str]]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _env_refs(v, (*parts, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _env_refs(v, (*parts, i))
    elif isinstance(node, str):
        for m in _VAR.finditer(node):
            out.append((_ptr(parts), f"environment variable {m.group(1)} is not set"))
    return out


def _semantic(cfg: dict) -> list[tuple[str, str, str | None]]:
    """Cross-key rules JSON Schema cannot say plainly: (pointer, message, supersedes-prefix or None)."""
    out: list[tuple[str, str, str | None]] = []
    cl = cfg.get("cloud") if isinstance(cfg.get("cloud"), dict) else None
    if cl is not None and not cl.get("stackInfo"):
        if not cl.get("apiUrl"):
            out.append(("/cloud", "set apiUrl (or stackInfo)", None))
        if not cl.get("cloudPublicKey"):
            out.append(("/cloud", "set cloudPublicKey (or stackInfo)", None))
    devs = cfg.get("devices") if isinstance(cfg.get("devices"), list) else []
    ids: dict[str, int] = {}
    for i, d in enumerate(devs):
        if isinstance(d, dict) and isinstance(d.get("deviceId"), str):
            if d["deviceId"] in ids:
                out.append((f"/devices/{i}/deviceId", f"duplicate deviceId {d['deviceId']!r} (also devices/{ids[d['deviceId']]})", None))
            ids.setdefault(d["deviceId"], i)
    for i, d in enumerate(devs):
        if not isinstance(d, dict):
            continue
        base = f"/devices/{i}"
        if "mcu" in d:
            if "drivers" in d:
                out.append((f"{base}/drivers", "an MCU child (mcu) has no drivers: it gets mcu-module and mcu-firmware", None))
            if "role" in d:
                out.append((f"{base}/role", "an MCU child's role is always child-device; remove role", None))
        par = d.get("parent")
        if isinstance(par, str):
            if par not in ids:
                out.append((f"{base}/parent", f"parent {par!r} is not a device of this config", None))
            elif par == d.get("deviceId"):
                out.append((f"{base}/parent", "a device cannot be its own parent", None))
            elif isinstance(devs[ids[par]], dict) and "mcu" in devs[ids[par]]:
                out.append((f"{base}/parent", f"parent {par!r} is an MCU child, not a gateway", None))
        m = d.get("mcu")
        smp = m.get("smp") if isinstance(m, dict) else None
        if isinstance(smp, dict) and ({"host", "port"} & set(smp)) and ({"tty", "baud"} & set(smp)):
            out.append((f"{base}/mcu/smp", "use either {host, port} (TCP) or {tty, baud} (serial), not both",
                        f"{base}/mcu/smp"))
        r = d.get("relay")
        if isinstance(r, dict) and r.get("enabled") is True:
            if not (r.get("model") or cfg.get("model")):
                out.append((f"{base}/relay", "an enabled relay needs a model (relay.model or top-level model)", None))
            if not (r.get("deployment") or cfg.get("deployment")):
                out.append((f"{base}/relay", "an enabled relay needs a deployment (relay.deployment or top-level "
                                             "deployment)", None))
            if not (r.get("modelDevice") or d.get("modelDevice")):
                out.append((f"{base}/relay", "an enabled relay needs modelDevice (relay.modelDevice or the device's "
                                             "modelDevice)", None))
    return out


def validate(cfg) -> list[dict]:
    """[] when valid, else [{"path": <JSON pointer>, "message": <plain text>}] sorted by path."""
    if not isinstance(cfg, dict):
        return [{"path": "", "message": f"the config must be a mapping, got {_tname(cfg)}"}]
    found: list[tuple[str, str]] = []
    env = _env_refs(cfg)
    sem = _semantic(cfg)
    covered = {p for p, _ in env} | {s for _, _, s in sem if s}
    for e in _validator().iter_errors(cfg):
        for p, msg in _plain(e):
            if not any(p == c or p.startswith(c + "/") for c in covered):
                found.append((p, msg))
    found += env + [(p, m) for p, m, _ in sem]
    seen, out = set(), []
    for p, m in sorted(found, key=lambda t: (t[0].count("/"), t[0], t[1])):
        if (p, m) not in seen:
            seen.add((p, m))
            out.append({"path": p, "message": m})
    return out


# ---- resolve ----------------------------------------------------------------------------------------------------------
def resolve(cfg: dict, *, check_tokens: bool = False, warnings: list[str] | None = None) -> dict:
    """Read the coordination files of the other components (in place; returns cfg), as host.load_config always did:

      cloud.stackInfo                demo/cloud's stack.json -> apiUrl (its internal P2 URL) and cloudPublicKey, if unset
      cloud.releaseKeyFiles          Ed25519 release key files (flex release `release_key`) -> releasePublicKeys

    devices[].onboardingTokenFile is not read into the config any more (EV-06): the engine consumes the file at
    onboarding and deletes it afterwards. `check_tokens` (config check --resolve) verifies that the token file of a
    device that is not onboarded yet (its state.json) is readable, and warns when its mode would be refused (not 0600).
    """
    errors: list[dict] = []
    cl = cfg.setdefault("cloud", {})
    if cl.get("stackInfo"):
        try:
            info = json.loads(Path(cl["stackInfo"]).read_text())
            cl.setdefault("apiUrl", (info.get("internal") or {}).get("p2") or info["urls"]["p2"])
            cl.setdefault("cloudPublicKey", info["cloudPublicKey"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            errors.append({"path": "/cloud/stackInfo", "message": f"cannot use {cl['stackInfo']}: {_why(e)}"})
    keys = list(cl.get("releasePublicKeys") or [])
    for i, f in enumerate(cl.get("releaseKeyFiles") or []):
        from flexengine2.identity import crypto

        try:
            keys.append(crypto.public_b64(crypto.load_private(f)))
        except (OSError, ValueError) as e:
            errors.append({"path": f"/cloud/releaseKeyFiles/{i}", "message": f"cannot use {f}: {_why(e)}"})
    cl["releasePublicKeys"] = keys
    for i, dev in enumerate(cfg.get("devices", []) if check_tokens else []):
        tf = dev.get("onboardingTokenFile")
        if not tf or dev.get("onboardingToken") or _onboarded(cfg, dev):
            continue
        try:
            if not Path(tf).read_text().strip():
                raise OSError("file is empty")
        except OSError as e:
            errors.append({"path": f"/devices/{i}/onboardingTokenFile", "message": f"cannot read {tf}: {_why(e)}"})
            continue
        from flexengine2.identity.keys import check_private_file

        try:
            check_private_file(tf, what="onboarding token")
        except PermissionError as e:
            if warnings is not None:
                warnings.append(f"/devices/{i}/onboardingTokenFile: {e} (the engine refuses it; chmod 600)")
    if errors:
        raise ConfigError(errors)
    return cfg


def _onboarded(cfg: dict, dev: dict) -> bool:
    data = Path(dev.get("dataDir") or Path(str(cfg.get("dataDir") or ".")) / str(dev.get("deviceId")))
    try:
        return bool(json.loads((data / "state.json").read_text()).get("onboarded"))
    except (OSError, ValueError, AttributeError):
        return False


def _why(e: Exception) -> str:
    if isinstance(e, OSError):
        return e.strerror or str(e)
    if isinstance(e, KeyError):
        return f"missing key {e.args[0]!r}"
    return str(e) or type(e).__name__


# ---- entry points -----------------------------------------------------------------------------------------------------
def load(path: str | Path, *, strict: bool = True, notes: list[str] | None = None) -> dict:
    """JSON or YAML -> canonical config dict with the referenced files resolved. strict (default): validate before
    resolving and raise ConfigError on any error. Relative paths are taken as given (relative to the cwd)."""
    cfg = parse(path, notes)
    if strict:
        errors = validate(cfg)
        if errors:
            raise ConfigError(errors)
    return resolve(cfg)


def check(path: str | Path, *, resolve_files: bool = False) -> dict:
    """{"valid": bool, "errors": [{path, message}], "warnings": [str]} for `flexengine config check`.
    resolve_files also reads stackInfo / releaseKeyFiles and checks onboardingTokenFile (run it on the target host)."""
    notes: list[str] = []
    try:
        cfg = parse(path, notes)
        errors = validate(cfg)
        if not errors and resolve_files:
            errors = validate(resolve(cfg, check_tokens=True, warnings=notes))
    except ConfigError as e:
        errors = e.errors
    return {"valid": not errors, "errors": errors, "warnings": notes}

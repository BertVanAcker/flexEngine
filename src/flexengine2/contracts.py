"""Connection-point contracts (CP-1...CP-8, docs/flexengine-v2-plan.md §5.0): load and validate.

The canonical machine-readable specs are package data in ``flexengine2/api/`` (``*.schema.json``, the reason-code
registry, the OpenAPI files), so they ship in the wheel; ``flexengine/api/`` in the repo holds the prose specs, test
vectors and generator scripts (see its README).

    from flexengine2 import contracts
    contracts.validate("desired-state", doc)                  # raises ContractError with every problem
    contracts.validate("onboarding#OnboardingRequest", body)  # a $defs entry of a schema file
    contracts.reason_code("artefact rejected: bad signature") # -> registry entry (FE-REC-002) or None

A schema name is the file name up to its version (``desired-state`` -> ``desired-state.v1alpha1.schema.json``).
"""

from __future__ import annotations

import functools
import json
import re
from importlib import resources
from pathlib import Path

import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


class ContractError(ValueError):
    """A document does not satisfy its connection-point contract. `errors` lists "<json path>: <message>"."""

    def __init__(self, name: str, errors: list[str]):
        self.name, self.errors = name, errors
        super().__init__(f"{name}: {len(errors)} contract violation(s): " + "; ".join(errors[:10])
                         + (" ..." if len(errors) > 10 else ""))


def api_dir() -> Path:
    return Path(str(resources.files("flexengine2") / "api"))


@functools.cache
def _files() -> dict[str, Path]:
    out = {}
    for p in sorted(api_dir().glob("*.schema.json")):
        out[re.split(r"\.v[0-9]", p.name, maxsplit=1)[0]] = p
    return out


def schema_names() -> list[str]:
    return sorted(_files())


@functools.cache
def _registry() -> tuple[Registry, dict[str, dict]]:
    docs = {n: json.loads(p.read_text(encoding="utf-8")) for n, p in _files().items()}
    reg = Registry().with_resources((d["$id"], Resource.from_contents(d, default_specification=DRAFT202012))
                                    for d in docs.values())
    return reg, docs


def load(name: str) -> dict:
    """The schema document (``name``) or a wrapper that points at ``name#Def`` inside it."""
    base, _, sub = name.partition("#")
    reg, docs = _registry()
    if base not in docs:
        raise KeyError(f"unknown contract {base!r} (known: {schema_names()})")
    doc = docs[base]
    if not sub:
        return doc
    if sub not in doc.get("$defs", {}):
        raise KeyError(f"{base} has no $defs/{sub}")
    return {"$schema": doc["$schema"], "$ref": f"{doc['$id']}#/$defs/{sub}"}


def _path(err) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)


def errors(name: str, doc) -> list[str]:
    """Every violation of the schema plus the semantic rules of the contract ([] = valid)."""
    reg, _ = _registry()
    v = jsonschema.Draft202012Validator(load(name), registry=reg, format_checker=jsonschema.FormatChecker())
    out = [f"{_path(e)}: {e.message}" for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))]
    if not out:
        out += _SEMANTIC.get(name, lambda d: [])(doc)
    return out


def validate(name: str, doc) -> None:
    errs = errors(name, doc)
    if errs:
        raise ContractError(name, errs)


def is_valid(name: str, doc) -> bool:
    return not errors(name, doc)


# ---- semantic rules (not expressible in JSON Schema 2020-12) ----------------------------------------------------

def _desired_state_rules(doc: dict) -> list[str]:
    out, seen = [], set()
    for i, d in enumerate(doc["deployments"]):
        dep_id = d["metadata"]["id"]
        if dep_id in seen:
            out.append(f"$.deployments[{i}].metadata.id: duplicate deployment id {dep_id!r}")
        seen.add(dep_id)
        dev = d["metadata"].get("deviceId")
        if dev is not None and dev != doc["deviceId"]:
            out.append(f"$.deployments[{i}].metadata.deviceId: {dev!r} differs from deviceId {doc['deviceId']!r}")
    return out


_SEMANTIC = {"desired-state": _desired_state_rules}


# ---- reason codes (CP-2) ----------------------------------------------------------------------------------------

@functools.cache
def reason_codes() -> dict:
    return json.loads((api_dir() / "reason-codes.v1alpha1.json").read_text(encoding="utf-8"))


def reason_code(text: str, field: str | None = None) -> dict | None:
    """The registry entry whose pattern matches a reported text (most specific = fewest wildcards first)."""
    entries = [e for e in reason_codes()["codes"] if field is None or e["field"] == field]
    for e in sorted(entries, key=lambda e: (e["template"].count("{"), -len(e["template"]))):
        if re.fullmatch(e["pattern"], text, flags=re.S):
            return e
    return None

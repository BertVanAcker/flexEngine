"""CP-1...CP-8 specs: the schema files themselves, golden valid/invalid examples, the OpenAPI documents."""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

from flexengine2 import contracts

from _api_helpers import GOLDEN, REPO, SRC, load_ref

API = contracts.api_dir()


def test_api_dir_is_package_data():
    assert API == SRC / "api" and (API / "desired-state.v1alpha1.schema.json").is_file()
    assert set(contracts.schema_names()) >= {"desired-state", "status", "capabilities", "onboarding", "descriptor",
                                             "oci-profile", "trust", "cli-status"}


@pytest.mark.parametrize("name", contracts.schema_names())
def test_schema_is_valid_2020_12_and_id_matches_file(name):
    doc = contracts.load(name)
    jsonschema.Draft202012Validator.check_schema(doc)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == f"https://flex.dev/schemas/{contracts._files()[name].name}"


def _golden_valid():
    out = []
    for p in sorted(GOLDEN.rglob("*.json")):
        rel = p.relative_to(GOLDEN)
        if rel.parts[0] in ("SOURCES.json", "invalid.json") or len(rel.parts) < 3:
            continue
        schema, sub = rel.parts[0], rel.parts[1]
        out.append(pytest.param(schema if sub == "valid" else f"{schema}#{sub}", p, id=str(rel)))
    return out


@pytest.mark.parametrize("name,path", _golden_valid())
def test_golden_valid(name, path):
    contracts.validate(name, json.loads(path.read_text()))


def _pointer_set(doc, ptr: str, value=None, delete=False):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ptr.lstrip("/").split("/")]
    cur = doc
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        i = int(last)
        if delete:
            del cur[i]
        elif i == len(cur):
            cur.append(value)
        else:
            cur[i] = value
    elif delete:
        del cur[last]
    else:
        cur[last] = value


INVALID = json.loads((GOLDEN / "invalid.json").read_text())


@pytest.mark.parametrize("case", INVALID, ids=[f"{c['schema']}:{c['error'][:40]}" for c in INVALID])
def test_golden_invalid_rejected_with_expected_error(case):
    doc = copy.deepcopy(load_ref(case["base"]))
    if not case.get("baseInvalid"):
        contracts.validate(case["schema"], doc)  # the base is valid
    for ptr, value in case["set"].items():
        _pointer_set(doc, ptr, value)
    for ptr in case["delete"]:
        _pointer_set(doc, ptr, delete=True)
    with pytest.raises(contracts.ContractError) as ei:
        contracts.validate(case["schema"], doc)
    assert any(case["error"] in e for e in ei.value.errors), ei.value.errors


def test_golden_sources_documented():
    src = json.loads((GOLDEN / "SOURCES.json").read_text())
    for rel in src:
        assert (GOLDEN / rel).is_file(), rel
    rec = {str(p.relative_to(GOLDEN)) for p in GOLDEN.rglob("rec-*.json")} | {"capabilities/legacy-prototype.json"}
    assert rec <= set(src), rec - set(src)


# ---- driver profiles: the enum is exactly the set of in-tree driver profiles ------------------------------------

def _driver_profiles() -> set[str]:
    out = set()
    for p in (SRC / "drivers").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign) and any(getattr(t, "id", None) == "profile" for t in st.targets) \
                            and isinstance(st.value, ast.Constant) and st.value.value != "?":
                        out.add(st.value.value)
    return out


def test_driver_enum_equals_driver_profiles():
    enum = set(contracts.load("desired-state")["$defs"]["DriverProfile"]["enum"])
    assert enum == _driver_profiles() == {"container", "native", "applab", "mcu-module", "mcu-firmware", "plc-verify",
                                          "os-rauc", "arduino-ota"}
    caps = contracts.load("capabilities")["properties"]["x-flex"]["properties"]["drivers"]["properties"]
    assert set(caps) == enum
    desc = set(contracts.load("descriptor")["properties"]["profiles"]["items"]["enum"])
    assert desc == enum | {"system"}


# ---- OpenAPI documents ------------------------------------------------------------------------------------------

def _openapi(name: str):
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    p = API / name
    doc, _ = read_from_filename(str(p))
    validate(doc, base_uri=p.as_uri())
    return doc


def _refs(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == "$ref" and isinstance(v, str):
                yield v
            else:
                yield from _refs(v)
    elif isinstance(o, list):
        for x in o:
            yield from _refs(x)


@pytest.mark.parametrize("name", ["openapi-southbound.yaml", "local-api.v1.openapi.yaml"])
def test_openapi_valid_and_refs_resolve(name):
    doc = _openapi(name)
    for ref in set(_refs(doc)):
        file, _, frag = ref.partition("#")
        target = yaml.safe_load((API / file).read_text()) if file else doc
        for part in [p for p in frag.split("/") if p]:
            target = target[part]
        assert target is not None, ref


def test_southbound_keeps_every_p2_path_and_fixes_driver_enum():
    p2 = yaml.safe_load((REPO / "probes/p2-reconciling-engine/openapi.yaml").read_text())
    v1 = _openapi("openapi-southbound.yaml")
    assert v1["info"]["version"] == "v1alpha1"
    for path, ops in p2["paths"].items():
        assert path in v1["paths"], path
        assert set(ops) <= set(v1["paths"][path]), path
    assert v1["components"]["schemas"]["PlanRequest"]["properties"]["deployments"]["items"]["properties"]["driver"] == \
        {"$ref": "desired-state.v1alpha1.schema.json#/$defs/DriverProfile"}


def test_reference_fake_routes_are_in_the_southbound_spec():
    """Every route the reference fake (testing/fakecloud.py) serves is a path of the spec."""
    src = (SRC / "testing" / "fakecloud.py").read_text()
    spec = _openapi("openapi-southbound.yaml")["paths"]
    samples = {r"([\w.-]+)": "dev-1.child", r"(.+)": "flexnodes/mod", r"(manifests|blobs)": "manifests",
               r"(sha256:[0-9a-f]{64})": "sha256:" + "0" * 64}
    routes = re.findall(r'_match\(r"(/[^"]+)"', src) + re.findall(r'path == "(/[^"]+)"', src)
    assert len(routes) >= 15
    templates = [re.sub(r"\\\{repository\\\}", ".+", re.sub(r"\\\{(?!repository)[^}]+\\\}", "[^/]+", re.escape(t)))
                 for t in spec]
    for r in routes:
        path = r
        for g, v in samples.items():
            path = path.replace(g, v)
        assert any(re.fullmatch(t, path) for t in templates), (r, path)


def test_status_schema_lists_every_field_the_engine_reports():
    """Keyword arguments of report()/notice() calls in the reconciler are status fields (closed schema)."""
    props = set(contracts.load("status")["properties"])
    tree = ast.parse((SRC / "core" / "reconciler.py").read_text())
    kws = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) in ("report", "notice"):
            kws |= {k.arg for k in n.keywords if k.arg}
    kws |= {"failure", "failFast"}           # the `detail` dict
    kws |= {"faultVisibleAt", "faultToHealthySeconds"}  # mcu failure_detail()
    kws |= {"traceparent", "correlationId"}  # obs/instrument.py
    assert kws <= props, kws - props

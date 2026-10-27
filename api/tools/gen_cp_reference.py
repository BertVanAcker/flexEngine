"""Generate api/cp-reference.md (connection points CP-1...CP-8, EV-16) from the specs in the package.

    python api/tools/gen_cp_reference.py [--check] [--out PATH]

Sources: the JSON Schemas, OpenAPI documents and the reason-code registry in src/flexengine2/api/, the Driver class
of drivers/base.py (CP-5) and the headings of the prose specs in api/*.md. The output is deterministic;
tests/api/test_cp_reference.py fails when the committed file differs (regenerate with this script).
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
API_MD = HERE.parent                                    # api (prose specs)
FE = API_MD.parent                                      # flexengine/
OUT = API_MD / "cp-reference.md"
SPECS = FE / "src" / "flexengine2" / "api"              # machine-readable specs (package data)

# CP -> (title, machine-readable files in SPECS, prose specs in api/)
CPS = [
    ("CP-1", "Desired-state model", ["desired-state.v1alpha1.schema.json", "descriptor.v1alpha1.schema.json"],
     ["README.md"]),
    ("CP-2", "Status, capabilities, onboarding", ["status.v1alpha1.schema.json", "device-event.v1alpha1.schema.json",
                                                   "capabilities.v1alpha1.schema.json", "onboarding.v1alpha1.schema.json",
                                                   "reason-codes.v1alpha1.json"], []),
    ("CP-3", "Artefact access (OCI subset) and offline bundle", ["oci-profile.v1alpha1.schema.json"],
     ["bundle.v1alpha1.md"]),
    ("CP-4", "Trust", ["trust.v1alpha1.schema.json"], ["trust.v1alpha1.md"]),
    ("CP-5", "Driver SPI v1", ["driver-spi.v1.schema.json"], ["cp5-driver-spi.md"]),
    ("CP-6", "Child-device interfaces", [], ["cp6-children.md"]),
    ("CP-7", "Local API and CLI", ["local-api.v1.openapi.yaml", "cli-status.v1.schema.json"], []),
    ("CP-8", "Telemetry", ["telemetry.v1.schema.json"], ["telemetry.v1.md"]),
]
SOUTHBOUND = "openapi-southbound.yaml"
VERSION_RE = re.compile(r"\.(v\d+(?:alpha\d+|beta\d+)?)\.")


def version_of(name: str) -> str | None:
    """Version tag of a spec file: from its name (`x.v1alpha1.schema.json`) or the title of a prose spec."""
    m = VERSION_RE.search(name)
    if m:
        return m.group(1)
    if name.endswith(".md") and name != "README.md" and (API_MD / name).exists():
        h1 = (API_MD / name).read_text().splitlines()[0]
        m = re.search(r"\b(v\d+(?:alpha\d+|beta\d+)?)\b", h1)
        return m.group(1) if m else None
    return None


def cell(text) -> str:
    s = " ".join(str(text or "").split())
    return s.replace("|", "\\|")


def first_sentence(text: str | None, limit: int = 220) -> str:
    s = " ".join((text or "").split())
    m = re.search(r"(?<=[a-z0-9)\]`'\"])\. (?=[A-Z`(])", s)
    if m and m.start() < limit:
        s = s[:m.start() + 1]
    elif len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + " …"
    return cell(s)


def typ(node: dict) -> str:
    if not isinstance(node, dict):
        return ""
    if "$ref" in node:
        ref = node["$ref"]
        name = ref.rsplit("/", 1)[-1]
        return f"[{name}](#{anchor_def(name)})" if ref.startswith("#/") else f"`{ref}`"
    if "const" in node:
        return f"`{json.dumps(node['const'])}`"
    if "enum" in node:
        vals = node["enum"]
        shown = ", ".join(f"`{v}`" if not isinstance(v, str) else f"`{v}`" for v in vals[:10])
        return f"enum {shown}" + (" …" if len(vals) > 10 else "")
    for k in ("oneOf", "anyOf"):
        if k in node:
            return " or ".join(typ(n) or "object" for n in node[k])
    t = node.get("type")
    if isinstance(t, list):
        t = " or ".join(t)
    if t == "array":
        return f"array of {typ(node.get('items', {})) or 'any'}"
    if t == "object" and "additionalProperties" in node and isinstance(node["additionalProperties"], dict):
        return f"map of {typ(node['additionalProperties']) or 'any'}"
    return t or ("object" if "properties" in node else "")


_DOC = [""]  # stem of the schema being rendered: $defs anchors are per document


def anchor_def(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{_DOC[0]}-{name}".lower()).strip("-")


def props_table(node: dict) -> list[str]:
    props = node.get("properties") or {}
    if not props:
        return []
    req = set(node.get("required") or [])
    out = ["| Field | Type | Req. | Description |", "|---|---|---|---|"]
    for k, v in props.items():
        out.append(f"| `{cell(k)}` | {typ(v)} | {'✔' if k in req else ''} | {first_sentence(v.get('description'))} |")
    extra = node.get("additionalProperties")
    if extra is False:
        out.append("")
        out.append("Closed object: other keys are refused.")
    return out


def schema_section(fname: str) -> list[str]:
    doc = json.loads((SPECS / fname).read_text())
    _DOC[0] = fname.split(".")[0]
    out = [f"### `{fname}`", "", f"- `$id`: `{doc.get('$id')}`", f"- Title: {cell(doc.get('title'))}",
           f"- Version: `{version_of(fname)}`", "", first_sentence(doc.get("description"), 600), ""]
    t = props_table(doc)
    if t:
        out += t + [""]
    for name, d in (doc.get("$defs") or {}).items():
        out += [f"<a id=\"{anchor_def(name)}\"></a>", f"#### `$defs/{name}` ({typ({k: v for k, v in d.items() if k != 'description'}) or 'schema'})", ""]
        if d.get("description"):
            out += [first_sentence(d["description"], 400), ""]
        t = props_table(d)
        out += (t + [""]) if t else []
    return out


def reason_codes_section(fname: str) -> list[str]:
    doc = json.loads((SPECS / fname).read_text())
    codes = doc["codes"]
    out = [f"### `{fname}` (reason-code registry)", "", f"- Version: `{version_of(fname)}`, {len(codes)} codes "
           f"({sum(1 for c in codes if c.get('retired'))} retired)",
           "- Fields: `reason` = status `reason`, `failure` = status `failure`, `event` = device event, `detail` = "
           "text inside another entry's `{detail}`, `log` = logged only.", "",
           "| Code | Field | Stage | Profile | Text |", "|---|---|---|---|---|"]
    for c in codes:
        text = c["template"] + (" *(retired)*" if c.get("retired") else "")
        out.append(f"| `{c['code']}` | {c['field']} | {c['stage']} | {c['profile']} | {cell(text)} |")
    return out + [""]


def openapi_section(fname: str) -> list[str]:
    doc = yaml.safe_load((SPECS / fname).read_text())
    info = doc.get("info", {})
    out = [f"### `{fname}`", "", f"- Title: {cell(info.get('title'))}", f"- Version: `{info.get('version')}`", "",
           "| Method | Path | Summary | Request | Responses |", "|---|---|---|---|---|"]
    for path, ops in (doc.get("paths") or {}).items():
        for method, op in ops.items():
            if method not in ("get", "put", "post", "delete", "patch", "head"):
                continue
            body = (((op.get("requestBody") or {}).get("content") or {}))
            req = ", ".join(sorted(body)) if body else ""
            codes = ", ".join(_response(c, r) for c, r in (op.get("responses") or {}).items())
            out.append(f"| {method.upper()} | `{cell(path)}` | {first_sentence(op.get('summary') or op.get('description'), 160)} "
                       f"| {cell(req)} | {codes} |")
    return out + [""]


def _response(code, resp) -> str:
    """`200` or `200 (Status)` when the answer's JSON body names a schema."""
    for media in ((resp or {}).get("content") or {}).values():
        ref = ((media or {}).get("schema") or {}).get("$ref", "")
        if ref:
            name = ref.rsplit("/", 1)[-1].split("#")[0].replace(".schema.json", "")
            return f"{code} ({cell(name)})"
    return str(code)


def spi_section() -> list[str]:
    sys.path.insert(0, str(FE / "src"))
    from flexengine2.drivers.base import SPI_VERSION, Driver
    out = [f"### `drivers/base.py` (SPI `{SPI_VERSION}`)", "",
           "Normative text: the module docstring of `src/flexengine2/drivers/base.py`. Guide for driver authors: "
           "[../docs/spi-guide.md](../docs/spi-guide.md).", "",
           "| Member | Kind | Signature / default |", "|---|---|---|"]
    abstract = set(getattr(Driver, "__abstractmethods__", ()))
    for name in ("profile", "name", "exclusive", "max_deployments", "reboots_on_apply", "repairs_digest_drift", "runtime"):
        out.append(f"| `{name}` | class attribute | `{getattr(Driver, name)!r}` |")
    for name, fn in inspect.getmembers(Driver, inspect.isfunction):
        if name.startswith("_"):
            continue
        sig = str(inspect.signature(fn)).replace("|", "\\|")
        kind = "required" if name in abstract else "optional (default in base)"
        out.append(f"| `{name}` | {kind} | `{name}{sig}` |")
    out.append("| `failure_detail` | optional, looked up by name (no base default) | `failure_detail(self, dep_id, healthy_at) -> dict` |")
    out.append("| `verified` | profile extension (plc-verify only) | `verified(self, dep_id, want) -> bool` |")
    return out + [""]


def prose_section(fname: str) -> list[str]:
    text = (API_MD / fname).read_text()
    heads = [h for h in re.findall(r"^(#{1,3}) (.+)$", text, re.M)]
    out = [f"### [{fname}]({fname})", ""]
    for level, h in heads:
        if len(level) == 1:
            continue
        out.append(f"{'  ' * (len(level) - 2)}- {cell(h)}")
    return out + [""]


def render() -> str:
    out = ["# Connection-point reference CP-1 … CP-8", "",
           "<!-- generated by api/tools/gen_cp_reference.py; do not edit by hand -->", "",
           "Generated from the specs in `src/flexengine2/api/` (package data), `drivers/base.py` and the prose specs "
           "in this directory. It lists what each contract contains; the specs themselves are normative. Overview, "
           "versioning rules and the CP-1 field table: [README.md](README.md).", "",
           "Regenerate after changing a spec: `python api/tools/gen_cp_reference.py` "
           "(`tests/api/test_cp_reference.py` fails while this file is stale).", "",
           "## Index", "", "| CP | Connection point | Version | Machine-readable spec | Prose spec |", "|---|---|---|---|---|"]
    for cp, title, files, prose in CPS:
        vers = sorted({v for v in (version_of(f) for f in files + prose) if v})
        out.append(f"| [{cp}](#{cp.lower()}) | {title} | {', '.join(f'`{v}`' for v in vers) or '–'} | "
                   f"{', '.join(f'`{f}`' for f in files) or '–'} | {', '.join(f'[{p}]({p})' for p in prose) or '–'} |")
    out.append(f"| [all](#southbound-api) | Southbound HTTP API (flexCloud ⇄ engine) | "
               f"`{yaml.safe_load((SPECS / SOUTHBOUND).read_text())['info']['version']}` | `{SOUTHBOUND}` | – |")
    out.append("")
    for cp, title, files, prose in CPS:
        out += [f"<a id=\"{cp.lower()}\"></a>", f"## {cp} {title}", ""]
        for f in files:
            if f.endswith(".schema.json"):
                out += schema_section(f)
            elif f.startswith("reason-codes"):
                out += reason_codes_section(f)
            elif f.endswith((".yaml", ".yml")):
                out += openapi_section(f)
        if cp == "CP-5":
            out += spi_section()
        for p in prose:
            if p != "README.md":
                out += prose_section(p)
    out += ["<a id=\"southbound-api\"></a>", "## Southbound HTTP API", ""] + openapi_section(SOUTHBOUND)
    final = out
    return "\n".join(final).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--check", action="store_true", help="exit 1 when the file differs from the specs")
    a = ap.parse_args(argv)
    text, out = render(), Path(a.out)
    if a.check:
        if not out.exists() or out.read_text() != text:
            print(f"{out} is stale: run python api/tools/gen_cp_reference.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

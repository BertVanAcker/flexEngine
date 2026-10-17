"""Find every rejection/failure reason text the engine can put into a status report (CP-2 reason registry).

Static scan (AST) of the engine sources. A *sink* is a place whose string ends up in a status report's `reason` or
`failure` field, directly or embedded in another reason (e.g. `artefact rejected: {e}` embeds a VerificationError):

  * keyword `reason=` of any call; a dict literal key "reason"
  * `raise VerificationError|DriverError|ReleaseError|SmpError|ValueError(<text>)`
  * `return <text>` inside `preflight` / `failed`; `return False, <text>` anywhere
  * assignments to `gate_failure`, `why`, `reason` (incl. tuple assignments) in the reconciler
  * `reasons.append(<text>)` (artefact store: signature check details)
  * `super().__init__(<text>)` of cloud.client.Rejected (a 4xx from the cloud while fetching artefacts)
  * `self.log(<text>)` in a block of accept_manifest that ends with `return False`: desired-state refusals, logged only (no status report today)

Each found text is rendered as a template: f-string fields become `{}`; an `a if c else b` yields both branches.
Only literal texts are templates; a bare variable just forwards a text found elsewhere.

    python flexengine/api/tools/reasonscan.py      # prints {file:line: template}
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "flexengine2"
FILES = ["core/reconciler.py", "artefacts/store.py", "cloud/client.py", "drivers/base.py", "drivers/container.py", "drivers/process.py",
         "drivers/native/systemd.py", "drivers/native/pyenv.py",
         "drivers/applab/driver.py", "drivers/applab/ard.py", "drivers/mcu/driver.py", "drivers/mcu/flxm.py",
         "drivers/mcu/link.py", "drivers/plc/verify.py", "drivers/os_rauc.py", "drivers/arduino_ota.py"]
RAISES = {"VerificationError", "DriverError", "ReleaseError", "SmpError", "ValueError", "RuntimeError",
          "RaucUnavailable"}
ASSIGN_NAMES = {"gate_failure", "why", "reason"}
LOG_FUNCS = {"accept_manifest"}   # desired-state refusals are logged (and counted), not reported


def _texts(node) -> list[str]:
    """Templates of a str expression; [] when it is not a literal text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.replace("{", "{{").replace("}", "}}")]
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant):
                out += str(v.value).replace("{", "{{").replace("}", "}}")
            else:
                out += "{}"
        return [out]
    if isinstance(node, ast.IfExp):
        return _texts(node.body) + _texts(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _texts(node.left), _texts(node.right)
        if left and right:
            return [a + b for a in left for b in right]
        if left:
            return [a + "{}" for a in left]
        if right:
            return ["{}" + b for b in right]
    return []


def unbrace(t: str) -> str:
    return t.replace("{{", "{").replace("}}", "}")


def _name(n) -> str | None:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return n.attr
    return None


def scan_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), str(path))
    found: list[tuple[int, str]] = []

    def add(node, line):
        for t in _texts(node):
            found.append((line, t))

    class V(ast.NodeVisitor):
        def __init__(self):
            self.func: list[str] = []
            self.cls: list[str] = []

        def visit_ClassDef(self, n):
            self.cls.append(n.name)
            self.generic_visit(n)
            self.cls.pop()

        def visit_FunctionDef(self, n):
            self.func.append(n.name)
            self.generic_visit(n)
            self.func.pop()

        def visit_Call(self, n):
            for kw in n.keywords:
                if kw.arg == "reason":
                    add(kw.value, n.lineno)
            if _name(n.func) == "__init__" and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Call) \
                    and _name(n.func.value.func) == "super" and self.cls and self.cls[-1] == "Rejected":
                for a in n.args[:1]:
                    add(a, n.lineno)
            if _name(n.func) == "append" and isinstance(n.func, ast.Attribute) and _name(n.func.value) == "reasons":
                for a in n.args:
                    add(a, n.lineno)
            self.generic_visit(n)

        def visit_Dict(self, n):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == "reason":
                    add(v, n.lineno)
            self.generic_visit(n)

        def generic_visit(self, n):
            # `self.log(<text>) ... return False` inside accept_manifest: a refused desired state
            for name in ("body", "orelse"):
                block = getattr(n, name, None)
                if isinstance(block, list) and self.func and self.func[-1] in LOG_FUNCS:
                    last = block[-1] if block else None
                    if isinstance(last, ast.Return) and isinstance(last.value, ast.Constant) and last.value.value is False:
                        for a in block:
                            if isinstance(a, ast.Expr) and isinstance(a.value, ast.Call) and _name(a.value.func) == "log":
                                add(a.value.args[0], a.lineno)
            super().generic_visit(n)

        def visit_Raise(self, n):
            if isinstance(n.exc, ast.Call) and _name(n.exc.func) in RAISES and n.exc.args:
                add(n.exc.args[0], n.lineno)
            self.generic_visit(n)

        def visit_Return(self, n):
            v = n.value
            if v is not None:
                if self.func and self.func[-1] in ("preflight", "failed"):
                    add(v, n.lineno)
                if isinstance(v, ast.Tuple) and len(v.elts) == 2 and isinstance(v.elts[0], ast.Constant) \
                        and v.elts[0].value is False:
                    add(v.elts[1], n.lineno)
            self.generic_visit(n)

        def visit_Assign(self, n):
            if path.name == "reconciler.py":
                for t in n.targets:
                    if _name(t) in ASSIGN_NAMES:
                        add(n.value, n.lineno)
                    if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple):
                        for te, ve in zip(t.elts, n.value.elts):
                            if _name(te) in ASSIGN_NAMES:
                                add(ve, n.lineno)
            self.generic_visit(n)

    V().visit(tree)
    return found


def scan(src: Path = SRC) -> dict[str, list[str]]:
    """{template: ["file:line", ...]} over all engine files."""
    out: dict[str, list[str]] = {}
    for rel in FILES:
        for line, t in scan_file(src / rel):
            out.setdefault(unbrace(t), []).append(f"{rel}:{line}")
    return out


def normalize(template: str) -> str:
    """Registry templates name their fields ({digest}); the scan renders them as {}."""
    return re.sub(r"\{[^{}]*\}", "{}", template)


def pattern_of(template: str) -> str:
    """Regex for the runtime text of a template: literal parts escaped, each field = one or more characters."""
    parts = re.split(r"\{[^{}]*\}", template)
    return "^" + "(.+?)".join(re.escape(p) for p in parts) + "$"


if __name__ == "__main__":
    json.dump(scan(Path(sys.argv[1]) if len(sys.argv) > 1 else SRC), sys.stdout, indent=1, sort_keys=True)
    print()

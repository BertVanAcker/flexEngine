"""EV-16: the connection-point reference (api/cp-reference.md) is generated from the specs and never stale."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

FE = Path(__file__).resolve().parents[2]
GEN = FE / "api" / "tools" / "gen_cp_reference.py"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_cp_reference", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_cp_reference_is_up_to_date():
    gen = _gen()
    assert gen.OUT.read_text() == gen.render(), "regenerate: python flexengine/api/tools/gen_cp_reference.py"
    assert gen.main(["--check"]) == 0


def test_check_fails_on_a_stale_file(tmp_path):
    gen = _gen()
    stale = tmp_path / "cp-reference.md"
    stale.write_text(gen.render().replace("CP-4 Trust", "CP-4 Trusst", 1))
    assert gen.main(["--check", "--out", str(stale)]) == 1


def test_every_cp_and_every_spec_file_is_covered():
    gen = _gen()
    assert [c[0] for c in gen.CPS] == [f"CP-{i}" for i in range(1, 9)]
    listed = {f for c in gen.CPS for f in c[2]} | {gen.SOUTHBOUND}
    shipped = {p.name for p in gen.SPECS.iterdir() if p.suffix in (".json", ".yaml")}
    assert shipped == listed, f"spec files without a CP entry: {shipped - listed}; missing: {listed - shipped}"
    for _, _, _, prose in gen.CPS:
        for p in prose:
            assert (gen.API_MD / p).is_file(), p


def test_every_spec_carries_a_version_tag():
    gen = _gen()
    for cp, _, files, prose in gen.CPS:
        tags = {gen.version_of(f) for f in files + [p for p in prose if p != "README.md"]}
        assert tags and None not in tags, f"{cp}: a spec without a version tag ({files + prose})"


def test_reference_lists_every_reason_code_and_route():
    import json

    import yaml

    gen = _gen()
    text = gen.OUT.read_text()
    codes = json.loads((gen.SPECS / "reason-codes.v1alpha1.json").read_text())["codes"]
    assert all(f"`{c['code']}`" in text for c in codes)
    for f in ("openapi-southbound.yaml", "local-api.v1.openapi.yaml"):
        for path in yaml.safe_load((gen.SPECS / f).read_text())["paths"]:
            assert f"`{path}`" in text, path
    # every intra-document link resolves to an anchor of the page
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', text))
    for target in re.findall(r"\]\(#([^)]+)\)", text):
        assert target in anchors, target

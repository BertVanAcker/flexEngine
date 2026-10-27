"""FE-3 freeze gate (tools/freeze.py): the recorded public surface (api/freeze/) matches the tree, and the comparison
rules hold: a removal or a change fails, an addition passes only with a CHANGES.md row naming gate and entry."""

from __future__ import annotations

import importlib.util
import json

import pytest
import yaml

from _api_helpers import FLEXENGINE

_spec = importlib.util.spec_from_file_location("_freeze_tool", FLEXENGINE / "tools" / "freeze.py")
freeze = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(freeze)


def test_the_tree_matches_the_recorded_baseline(capsys):
    rc = freeze.check(list(freeze.GATES.values()))
    out = capsys.readouterr().out
    assert rc == 0, out


@pytest.mark.parametrize("name", list(freeze.GATES.values()))
def test_every_gate_has_a_baseline_with_its_changes_row(name):
    g = freeze.load_golden(name)
    assert g["gate"] == freeze.BY_NAME[name] and g["entries"]
    rows = freeze.changes_rows()
    assert any(r["date"] == g["recorded"]["date"] and g["gate"] in r["gates"] for r in rows)


def _row(gate, entries, date="2026-11-03"):
    return freeze.changes_rows(f"| Date | Gate | Entries | Why |\n|---|---|---|---|\n| {date} | {gate} | {entries} | x |\n")


BASE = {"s.json:/$defs/A/properties/x/type": "string", "s.json:/$defs/A/required": ["x"],
        "s.json:/$defs/A/additionalProperties": False, "s.json:/$defs/P/enum": ["a", "b"],
        "o.yaml:/paths/~1v1~1x/get/responses/200/content/application~1json/schema/$ref": "#/X"}


def _cmp(now, rows=(), gate="FE-F2"):
    return freeze.compare(gate, BASE, now, list(rows))


def test_unchanged_passes():
    r = _cmp(dict(BASE))
    assert not (r["removed"] or r["changed"] or r["unmatched"] or r["added"])


def test_removed_field_fails():
    now = dict(BASE)
    del now["s.json:/$defs/A/properties/x/type"]
    assert _cmp(now)["removed"] == ["s.json:/$defs/A/properties/x/type"]


def test_retyped_field_and_new_required_fail():
    now = {**BASE, "s.json:/$defs/A/properties/x/type": "integer"}
    assert _cmp(now)["changed"]
    now = {**BASE, "s.json:/$defs/A/properties/y/type": "string", "s.json:/$defs/A/required": ["x", "y"]}
    r = _cmp(now, _row("FE-F2", "`s.json:/$defs/A/properties/y`"))
    assert r["changed"] and r["added"]          # the property is an addition, making it required is not


def test_new_constraint_on_an_existing_node_fails_even_with_a_row():
    now = {**BASE, "s.json:/$defs/A/properties/x/maxLength": 3}
    r = _cmp(now, _row("FE-F2", "`s.json:*`"))
    assert r["changed"] and "new constraint" in r["changed"][0]


def test_new_optional_property_needs_a_row():
    now = {**BASE, "s.json:/$defs/A/properties/y/type": "string"}
    r = _cmp(now)
    assert r["unmatched"] == ["s.json:/$defs/A/properties/y"] and not r["changed"]
    r = _cmp(now, _row("FE-F2", "`s.json:/$defs/A/properties/y`"))
    assert not r["unmatched"] and r["added"]
    r = _cmp(now, _row("FE-F3", "`s.json:/$defs/A/properties/y`"))     # wrong gate
    assert r["unmatched"]
    r = _cmp(now, _row("FE-F2", "all"))                                # a re-record row admits nothing
    assert r["unmatched"]


def test_enum_value_and_new_route_are_additions():
    now = {**BASE, "s.json:/$defs/P/enum": ["a", "b", "c"],
           "o.yaml:/paths/~1v1~1y/get/responses/200/content/application~1json/schema/$ref": "#/Y",
           "o.yaml:/paths/~1v1~1x/post/responses/200/content/application~1json/schema/$ref": "#/X"}
    r = _cmp(now)
    assert sorted(r["unmatched"]) == ["o.yaml:/paths/~1v1~1x/post", "o.yaml:/paths/~1v1~1y",
                                      "s.json:/$defs/P/enum=c"]
    r = _cmp(now, _row("FE-F2", "`s.json:/$defs/P/enum=*`, `o.yaml:/paths/*`"))
    assert not r["unmatched"] and len(r["added"]) == 3


def test_removed_or_reordered_enum_value_fails():
    assert _cmp({**BASE, "s.json:/$defs/P/enum": ["a"]})["changed"]
    assert _cmp({**BASE, "s.json:/$defs/P/enum": ["b", "a"]})["changed"]


def test_new_required_cli_option_is_a_change():
    base = {"flexengine/commands/run/options/--config/required": True}
    now = {**base, "flexengine/commands/run/options/--x/required": True,
           "flexengine/commands/run/options/--y/required": False}
    r = freeze.compare("FE-F4", base, now, _row("FE-F4", "`flexengine/commands/run/options/*`"))
    assert len(r["changed"]) == 1 and "--x" in r["changed"][0]
    assert r["added"] == ["flexengine/commands/run/options/--y  (2026-11-03)"]


def test_reason_code_meaning_change_fails_retiring_is_an_addition():
    base = {"codes/FE-X-001/meaning": "m", "codes/FE-X-001/field": "reason"}
    assert freeze.compare("FE-F3", base, {**base, "codes/FE-X-001/meaning": "other"}, [])["changed"]
    r = freeze.compare("FE-F3", base, {**base, "codes/FE-X-001/retired": True}, [])
    assert r["unmatched"] == ["codes/FE-X-001/retired"] and not r["changed"]
    r = freeze.compare("FE-F3", base, {**base, "codes/FE-X-002/meaning": "new", "codes/FE-X-002/field": "log"},
                       _row("FE-F3", "`codes/FE-X-002`"))
    assert r["added"] and not r["unmatched"]


def test_changed_vector_fails_changed_spec_file_hash_is_a_note():
    assert freeze.compare("FE-F6", {"a.json": "1"}, {"a.json": "2"}, [])["changed"]
    r = freeze.compare("FE-F1", {"a.json": "1"}, {"a.json": "2", "b.json": "3"}, [])
    assert r["notes"] and r["unmatched"] == ["b.json"] and not r["changed"]


def test_flatten_keys_parameters_by_name_and_drops_docs():
    doc = {"paths": {"/a": {"get": {"description": "d", "parameters": [{"name": "since", "in": "query",
                                                                         "required": False}]}}}}
    flat = freeze.flatten(doc)
    assert flat == {"/paths/~1a/get/parameters/since@query/name": "since",
                    "/paths/~1a/get/parameters/since@query/in": "query",
                    "/paths/~1a/get/parameters/since@query/required": False}


def test_changes_rows_parse_gate_ranges():
    rows = _row("FE-F1…FE-F6", "all")
    assert rows[0]["gates"] == {f"FE-F{i}" for i in range(1, 7)} and rows[0]["all"]


def test_ci_runs_the_gate_and_ci_ok_needs_it():
    ci = yaml.safe_load((FLEXENGINE / ".github" / "workflows" / "ci.yml").read_text())
    job = ci["jobs"]["freeze"]
    assert any("tools/freeze.py check" in (s.get("run") or "") for s in job["steps"])
    assert "freeze" in ci["jobs"]["ci-ok"]["needs"]


def test_golden_files_are_stable_json():
    for name in freeze.GATES.values():
        p = freeze._golden_path(name)
        doc = json.loads(p.read_text())
        assert list(doc["entries"]) == sorted(doc["entries"])

"""JSON pointer matchers of the flex step types (pure)."""

from __future__ import annotations

import flexsteps as f

DOC = {"devices": [{"deviceId": "panda", "onboarded": True, "manifestVersion": 3,
                    "deployments": [{"id": "pg", "state": "Installed", "version": "1.1.0", "health": "healthy"},
                                    {"id": "dl", "state": "Failed", "version": "3.0.0", "rolledBackTo": None}]}],
       "slots": [{"rootfs.0": {"state": "inactive", "boot_status": "bad"}},
                 {"rootfs.1": {"state": "booted", "boot_status": "good"}}],
       "a/b": {"c~d": 1}}


def test_resolve_selectors_and_escapes():
    assert f.resolve(DOC, "/devices[deviceId=panda]/deployments[id=pg]/version") == "1.1.0"
    assert f.resolve(DOC, "/devices/0/manifestVersion") == 3
    assert f.resolve(DOC, "/devices/-1/onboarded") is True
    assert f.resolve(DOC, "/slots/*[state=booted]/boot_status") == "good"
    assert f.resolve(DOC, "/a~1b/c~0d") == 1
    assert f.resolve(DOC, "") is DOC
    assert f.resolve(DOC, "/devices[deviceId=nope]/x") is f._MISSING
    assert f.resolve(DOC, "/devices/7") is f._MISSING


def test_literals_compare_with_json_text():
    assert f.check_value(True, "true") is None and f.check_value(True, True) is None
    assert f.check_value(3, "3") is None and f.check_value(3.0, 3) is None
    assert f.check_value(None, "null") is None
    assert f.check_value(1, True) is not None          # 1 is not true
    assert f.check_value("1.0", "1.0.0") is not None


def test_operators():
    assert f.check_value("FLEX01_ab", {"regex": "^FLEX01_"}) is None
    assert f.check_value(5, {"gte": 1, "lte": 5}) is None and f.check_value(6, {"lte": 5})
    assert f.check_value("x", {"in": ["x", "y"]}) is None and f.check_value("z", {"in": ["x"]})
    assert f.check_value(f._MISSING, {"exists": False}) is None and f.check_value(1, {"exists": False})
    assert f.check_value([1, 2], {"len": 2}) is None
    assert f.check_value([{"a": 1}, {"a": 2}], {"contains": {"/a": 2}}) is None
    assert f.check_value("abc", {"contains": "b"}) is None
    assert f.check_value("a", {"ne": "a"}) and f.check_value("a", {"ne": "b"}) is None


def test_where_count():
    lines = [{"action": "apply", "dep": "pg", "version": "1.0.0"}, {"action": "reject", "dep": "pg", "version": "3.0.0"},
             {"action": "apply", "dep": "pg", "version": "1.1.0"}]
    assert f.check_value(lines, {"where": {"/action": "apply"}, "count": 2}) is None
    assert f.check_value(lines, {"where": {"/version": {"in": ["3.0.0"]}, "/action": {"ne": "reject"}}, "count": 0}) is None
    assert "count 1 != 2" in f.check_value(lines, {"where": {"/action": "reject"}, "count": 2})
    assert f.check_value(lines, {"where": {"/action": "remove"}}) is not None    # default: at least one


def test_match_doc_reasons_and_save():
    why = f.match_doc(DOC, {"/devices[deviceId=panda]/deployments[id=dl]/state": "Installed", "/devices/0/onboarded": True})
    assert why == ["/devices[deviceId=panda]/deployments[id=dl]/state: Failed != Installed"]

    class C:
        vars = {}
    saved = f.save_values(C, DOC, {"mv": "/devices/0/manifestVersion", "gone": "/nope"})
    assert saved == ["mv=3"] and C.vars == {"mv": "3"}


def test_json_output_parsing():
    assert f.parse_json_output('banner\n{"a": 1}') == {"a": 1}
    assert f.parse_json_output('{"a": 1}\n{"b": 2}\n', json_lines=True) == [{"a": 1}, {"b": 2}]
    assert f.parse_json_output('[1, 2]') == [1, 2]

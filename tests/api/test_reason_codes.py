"""CP-2 reason-code registry: every reason text in the engine sources is registered, and nothing else is."""

from __future__ import annotations

import json
import re

import pytest

from flexengine2 import contracts

from _api_helpers import GOLDEN, recorded, tool

scan = tool("reasonscan")
gen = tool("gen_reason_codes")
REG = contracts.reason_codes()


def test_registry_file_is_generated_and_current():
    assert REG == gen.build(), "run api/tools/gen_reason_codes.py"


def test_every_source_reason_is_registered_and_no_entry_is_stale():
    assert gen.problems(REG) == []


def test_scanner_finds_the_known_sinks():
    """Guard against a scanner that silently finds nothing (e.g. after a refactor of the reconciler)."""
    found = scan.scan()
    for t in ["artefact rejected: {}", "health check failed", "timeout after {} s", "held: PLC not verified: {}",
              "release not signed by a trusted key (no signature)", "module container: {}", "container dead",
              "{} runs interface {}…, the release expects {}… ({}): program not provisioned for this release",
              "desired state with bad/missing cloud signature ignored", "{}: {}"]:
        assert t in found, t
    assert len(found) >= 95


def test_codes_unique_well_formed_and_patterns_derived():
    codes = [e["code"] for e in REG["codes"]]
    assert len(codes) == len(set(codes))
    for e in REG["codes"]:
        assert re.fullmatch(r"FE-[A-Z]+(-[A-Z]+)?-[0-9]{3}", e["code"]), e["code"]
        assert e["field"] in REG["fields"]
        re.compile(e["pattern"])
        if "example" not in e:
            assert e["pattern"] == scan.pattern_of(e["template"])
        sample = e.get("example") or re.sub(r"\{[^{}]*\}", "X", e["template"])
        assert re.fullmatch(e["pattern"], sample), (e["code"], sample)
        assert contracts.reason_code(sample, e["field"])["code"] == e["code"], (e["code"], sample)


@pytest.mark.parametrize("text,field,code,inner", [
    ("artefact rejected: release not signed by a trusted key (no signature)", "reason", "FE-REC-002", "FE-ART-SIG-001"),
    ("artefact rejected: module ABI 2.0 incompatible with base ABI 1.0 on gw1.mcu0", "reason", "FE-REC-002", "FE-MCU-PRE-003"),
    ("artefact rejected: 404: {\"title\": \"Blob unknown\"}", "reason", "FE-REC-002", "FE-CLD-001"),
    ("artefact rejected: 'layers'", "reason", "FE-REC-002", "FE-ART-REL-098"),
    ("supervisor: restarted", "reason", "FE-REC-007", None),
    ("timeout after 8 s", "failure", "FE-HG-001", None),
    ("apply failed: arduino-app-cli app start: boom (exit 1)", "failure", "FE-REC-005", "FE-APL-001"),
    ("awaiting safe state: machine_parked, operator_confirmed", "reason", "FE-GATE-001", None),
])
def test_reason_lookup(text, field, code, inner):
    e = contracts.reason_code(text, field)
    assert e and e["code"] == code
    if inner:
        rest = re.fullmatch(e["pattern"], text).group(1)
        assert contracts.reason_code(rest, "detail")["code"] == inner


def _status_records():
    for p in sorted((GOLDEN / "status" / "valid").glob("*.json")):
        yield str(p.relative_to(GOLDEN)), json.loads(p.read_text())
    for f in recorded("demo/**/cloud-state.json") + recorded("probes/**/cloud-state.json"):
        for i, r in enumerate(json.loads(f.read_text())["statusLog"]):
            yield f"{f.name}#{i}", r


def test_every_recorded_reason_and_failure_has_a_code():
    n = 0
    for where, rec in _status_records():
        for field in ("reason", "failure"):
            if rec.get(field):
                n += 1
                e = contracts.reason_code(rec[field], field)
                assert e, (where, field, rec[field])
                if e["code"] in ("FE-REC-002", "FE-REC-005"):
                    inner = re.fullmatch(e["pattern"], rec[field], flags=re.S).group(1)
                    assert contracts.reason_code(inner, "detail"), (where, inner)
    assert n >= 10

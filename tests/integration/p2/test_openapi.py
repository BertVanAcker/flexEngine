"""The OpenAPI document is valid 3.1 and covers every route the stub serves."""
import re
from pathlib import Path

import yaml

import flexengine2.testing.fakecloud as fakecloud


# Since EV-00 the reference cloud is checked against V2's own frozen southbound contract (it serves the
# device-event route added on 2026-10-17, which the probe's spec does not know).
SPEC = Path(fakecloud.__file__).resolve().parents[1] / "api" / "openapi-southbound.yaml"


def test_openapi_valid_and_complete():
    from openapi_spec_validator import validate
    spec = yaml.safe_load(SPEC.read_text())
    validate(spec, base_uri=SPEC.parent.as_uri() + "/")  # relative $refs to the CP schemas
    src = Path(fakecloud.__file__).read_text()
    documented = {re.sub(r"\{[^}]+\}", "X", p) for p in spec["paths"]}
    served = set()
    for pat in re.findall(r'r"(/(?:api|v2)[^"]+)"', src) + re.findall(r'== "(/api/v1/[^"]+)"', src):
        for alt in (["manifests", "blobs"] if "(manifests|blobs)" in pat else [None]):
            q = pat.replace("(manifests|blobs)", alt) if alt else pat
            served.add(re.sub(r"\([^)]*\)", "X", q))
    missing = {s for s in served if s not in documented}
    assert not missing, missing

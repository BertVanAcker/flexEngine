"""The OpenAPI document is valid 3.1 and covers every route the stub serves."""
import re
from pathlib import Path

import yaml

import flexengine2.testing.fakecloud as fakecloud

from .p2lab import PROBE

# The stub's spec is still the probe's (read-only); flexengine/api/openapi-southbound.yaml is EV-11's frozen copy.
SPEC = PROBE / "openapi.yaml"


def test_openapi_valid_and_complete():
    from openapi_spec_validator import validate
    spec = yaml.safe_load(SPEC.read_text())
    validate(spec)
    src = Path(fakecloud.__file__).read_text()
    documented = {re.sub(r"\{[^}]+\}", "X", p) for p in spec["paths"]}
    served = set()
    for pat in re.findall(r'r"(/(?:api|v2)[^"]+)"', src) + re.findall(r'== "(/api/v1/[^"]+)"', src):
        for alt in (["manifests", "blobs"] if "(manifests|blobs)" in pat else [None]):
            q = pat.replace("(manifests|blobs)", alt) if alt else pat
            served.add(re.sub(r"\([^)]*\)", "X", q))
    missing = {s for s in served if s not in documented}
    assert not missing, missing

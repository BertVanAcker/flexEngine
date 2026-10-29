"""EV-16: the operator guide and the SPI guide stay correct: the worked engine.yaml is valid, relative links resolve."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from flexengine2.config import load as cl

FE = Path(__file__).resolve().parents[2]
DOCS = [FE / "README.md", FE / "docs" / "spi-guide.md", FE / "api" / "cp-reference.md",
        FE / "docs" / "capture-release.md"]


def _gh_anchor(heading: str) -> str:
    h = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return h.replace(" ", "-")


def test_readme_worked_engine_yaml_is_valid():
    text = (FE / "README.md").read_text()
    blocks = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S) if b.startswith("# /etc/flexengine/engine.yaml")]
    assert len(blocks) == 1, "README §2 must hold exactly one worked engine.yaml"
    errors = cl.validate(yaml.safe_load(blocks[0]))
    assert errors == [], errors


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    text = doc.read_text()
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
            continue
        path, _, frag = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            bad.append(target)
            continue
        if frag and dest.suffix == ".md":
            body = dest.read_text()
            anchors = {_gh_anchor(h) for h in re.findall(r"^#{1,6} (.+)$", body, re.M)}
            anchors |= set(re.findall(r'<a id="([^"]+)"></a>', body))
            if frag not in anchors:
                bad.append(target)
    assert not bad, f"{doc.name}: broken links {bad}"

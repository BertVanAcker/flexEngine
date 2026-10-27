"""docs/macos.md stays true to the engine: its engine.yaml examples pass `config check`, its launchd agent is a valid
plist that runs `flexengine run --config`, and it stops later than the engine's shutdown grace. The smoke test of the
guide (§7) was run by hand; these checks catch schema or CLI drift in the copy-paste parts."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from flexengine2.config import load as cl
from flexengine2.host import SHUTDOWN_GRACE
from flexengine2.identity import crypto

DOC = Path(__file__).resolve().parents[2] / "docs" / "macos.md"


def _blocks(lang: str) -> list[str]:
    return re.findall(rf"^```{lang}\n(.*?)^```$", DOC.read_text(encoding="utf-8"), flags=re.S | re.M)


def _configs() -> list[str]:
    return [b for b in _blocks("yaml") if b.startswith("# engine.yaml:")]


def _module_key_hex() -> str:
    pub = ec.generate_private_key(ec.SECP256R1()).public_key()
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:].hex()


def test_the_guide_has_the_three_setups():
    heads = [c.splitlines()[0] for c in _configs()]
    assert len(heads) == 3, heads
    assert [h.split(":")[1].split(",")[0].strip() for h in heads] == ["setup A", "setup B", "setup C"]


@pytest.mark.parametrize("i", range(3), ids=["A-no-mcu", "B-mcu", "C-mcu-relay"])
def test_engine_yaml_examples_pass_config_check(i, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/tester")       # fixed: macOS's tmp_path would overflow sun_path by itself
    text = (_configs()[i]
            .replace("<cloud-public-key>", crypto.public_b64(crypto.generate()))
            .replace("<release-public-key>", crypto.public_b64(crypto.generate()))
            .replace("<module-public-key>", _module_key_hex()))
    assert "<" not in re.sub(r"#.*", "", text), "an unreplaced placeholder"
    p = tmp_path / "engine.yaml"
    p.write_text(text, encoding="utf-8")
    r = cl.check(p)
    assert r["valid"], r["errors"]
    assert not r["warnings"], r["warnings"]          # e.g. a socket path that does not fit sun_path
    cfg = cl.parse(p)
    assert all(d.get("drivers", {}).get("native", {"runtime": "process"})["runtime"] == "process"
               for d in cfg["devices"]), "a Mac has no systemd"
    assert cfg["dataDir"] == "/Users/tester/flexengine/data"


def test_launchd_agent_is_a_valid_plist_for_flexengine_run():
    sec8 = DOC.read_text(encoding="utf-8").split("## 8.")[1].split("## 9.")[0]
    body = re.search(r"<<EOF\n(.*?)^EOF$", sec8, flags=re.S | re.M).group(1)
    agent = plistlib.loads(body.replace("$HOME", "/Users/tester").encode())
    assert agent["Label"] == "dev.flex.flexengine"
    assert agent["ProgramArguments"] == ["/Users/tester/flexengine/venv/bin/flexengine", "run", "--config",
                                         "/Users/tester/flexengine/engine.yaml"]
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert agent["ExitTimeOut"] > SHUTDOWN_GRACE    # launchd must not SIGKILL a running install or health gate
    assert "/usr/bin" in agent["EnvironmentVariables"]["PATH"].split(":")

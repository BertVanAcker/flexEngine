"""EV-42 through the importer: compose `depends_on` and config.yml `dependencies` -> proposal `dependsOn` -> plan ->
x-flex-dependsOn -> a real engine installs dependencies first and removes them last.

* appdesc-native without its docker component: two native python runnables, reporter depends on counter (healthy);
  native process driver only, nothing is built with Docker
* compose-two (the compose example): worker `depends_on: [api]` (compose short form = service_started); real Docker
"""

from __future__ import annotations

import json
import shutil

import pytest
import yaml

from .flexialab import PACKAGES, import_flexia
from .test_flexia_e2e import lab  # noqa: F401  (fixture)

pytestmark = pytest.mark.flexmodel   # `flex import-flexia` is flexmodel's (not on PyPI)


def _applies(eng_dir) -> list[tuple]:
    p = eng_dir / "data" / "actions.jsonl"
    acts = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
    return [(a["action"], a["dep"]) for a in acts if a["action"] in ("apply", "remove", "commit")]


def _import_and_deploy(lab, pkg, dev):  # noqa: F811
    cloud, tmp = lab["cloud"], lab["tmp"]
    eng = lab["engine"](dev)
    rc, doc, err = import_flexia(pkg, tmp / f"out-{pkg.name}", "--key", str(lab["key"]), "--version", "1.0.0",
                                 "--publish", cloud.url, "--admin-token-file", str(cloud.token_file))
    assert rc == 0, (err[-3000:], json.dumps(doc.get("report", {}).get("notes"), indent=1)[:3000])
    deps = doc["proposal"]["devices"][dev]["deployments"]
    cloud.deploy(dev, deps)
    return eng, deps


def test_appdesc_dependencies_order_native_install_and_removal(lab):  # noqa: F811
    cloud, tmp = lab["cloud"], lab["tmp"]
    pkg = tmp / "appdesc-ordered"
    shutil.copytree(PACKAGES / "appdesc-native", pkg)
    cfg = yaml.safe_load((pkg / "config.yml").read_text())
    cfg["components"] = [c for c in cfg["components"] if c["name"] != "web"]          # native only
    cfg["manifests"] = [m for m in cfg["manifests"] if m["name"] != "web-manifest"]
    (pkg / "config.yml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    shutil.rmtree(pkg / "nodes" / "web")

    eng, deps = _import_and_deploy(lab, pkg, "edge1")
    by = {d["id"]: d for d in deps}
    assert by["reporter"]["dependsOn"] == [{"deployment": "counter", "condition": "healthy"}]
    served = {d["metadata"]["id"]: d for d in cloud.st.desired_doc("edge1")["deployments"]}
    assert served["reporter"]["x-flex-dependsOn"] == [{"deployment": "counter", "condition": "healthy"}]
    st = cloud.wait(lambda: cloud.converged("edge1", {"counter": "1.0.0", "reporter": "1.0.0"}), 120,
                    "edge1 converged")
    assert st["counter"]["health"] == st["reporter"]["health"] == "healthy"
    acts = _applies(eng.dir)
    assert acts.index(("apply", "counter")) < acts.index(("apply", "reporter")), acts

    n0 = len(acts)
    cloud.deploy("edge1", [])
    cloud.wait(lambda: all((cloud.status("edge1").get(i) or {}).get("state") == "Removed"
                           for i in ("counter", "reporter")), 60, "edge1 removed")
    assert [a[1] for a in _applies(eng.dir)[n0:] if a[0] == "remove"] == ["reporter", "counter"]


@pytest.mark.docker   # builds and runs a real image: skipped without a usable Docker daemon
def test_compose_example_depends_on_orders_the_install(lab):  # noqa: F811
    cloud, tmp = lab["cloud"], lab["tmp"]
    pkg = tmp / "compose-two"
    shutil.copytree(PACKAGES / "compose-two", pkg)
    eng, deps = _import_and_deploy(lab, pkg, "edge1")
    by = {d["id"]: d for d in deps}
    assert by["worker"]["dependsOn"] == [{"deployment": "api", "condition": "started"}]
    assert "dependsOn" not in by["api"]
    served = {d["metadata"]["id"]: d for d in cloud.st.desired_doc("edge1")["deployments"]}
    assert served["worker"]["x-flex-dependsOn"] == [{"deployment": "api", "condition": "started"}]
    cloud.wait(lambda: cloud.converged("edge1", {"api": "1.0.0", "worker": "1.0.0"}), 240, "edge1 converged")
    acts = _applies(eng.dir)
    assert acts.index(("apply", "api")) < acts.index(("apply", "worker")), acts

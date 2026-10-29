"""The process driver gives a workload its writable directory as FLEX_DATA_DIR, like the systemd driver: the same
place (<dataDir>/native/<dep>/data), absolute, created before the first start, kept across updates and rollbacks."""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from pathlib import Path

import pytest

from flexengine2.drivers.base import Workload, wait_until
from flexengine2.drivers.process import PROCFS, ProcessDriver
from flexengine2.testing.native_release import targz_tree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process driver: POSIX sessions and signals")

# writes its environment (FLEX_* only) into the data directory, then stays alive
APP = textwrap.dedent("""
    import json, os, pathlib, time
    data = pathlib.Path(os.environ["FLEX_DATA_DIR"])
    env = {k: v for k, v in os.environ.items() if k.startswith("FLEX_")}
    (data / ("env-%s.json" % os.environ["FLEX_PARAM_TAG"])).write_text(json.dumps(env))
    while True:
        time.sleep(0.1)
""").encode()


def _workload(tmp: Path, version: str, tag: str) -> Workload:
    data = targz_tree({"app.py": (APP, 0o644), "version.txt": (version.encode(), 0o644)})
    path = tmp / f"payload-{version}.tar.gz"
    path.write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return Workload(dep_id="cap", module="capture", version=version, digest=digest, payload_path=str(path),
                    payload_annotations={"dev.flex.profile": "native"}, parameters={"tag": tag},
                    restart={"policy": "on-failure", "maxRetries": 2, "restartSec": 0.2})


def _env(data: Path, tag: str) -> dict:
    p = data / f"env-{tag}.json"
    assert wait_until(p.exists, 10, 0.05), f"workload did not write {p}"
    return json.loads(p.read_text())


def test_flex_data_dir_is_set_absolute_and_kept_across_updates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    drv = ProcessDriver("dev-data", Path("rel-data"))          # a relative engine dataDir: the variable is absolute
    v1, v2 = _workload(tmp_path, "1.0.0", "one"), _workload(tmp_path, "2.0.0", "two")
    try:
        drv.apply(None, v1)
        data = drv.data_dir("cap")
        assert data == (tmp_path / "rel-data" / "native" / "cap" / "data").resolve() and data.is_absolute()
        env = _env(data, "one")
        assert env["FLEX_DATA_DIR"] == str(data)
        assert set(env) == {"FLEX_UNIT", "FLEX_HEALTH_PORT", "FLEX_DATA_DIR", "FLEX_PARAM_TAG"}
        assert Path(drv.units["cap"]["dir"]).resolve() != data               # not inside a payload tree
        (data / "session-1").write_text("captured")
        drv.apply(v1, v2)                                                      # update: new tree, same data
        assert _env(data, "two")["FLEX_DATA_DIR"] == str(data)
        assert (data / "session-1").read_text() == "captured"
        drv.apply(v2, v1)                                                      # rollback: still there
        assert (data / "session-1").read_text() == "captured"
    finally:
        drv.remove("cap")
    assert (data / "session-1").exists()       # remove() leaves the directory (the systemd driver shares the path)


def test_flex_data_dir_exists_for_a_staged_unit_and_after_a_restart(tmp_path):
    drv = ProcessDriver("dev-data2", tmp_path / "data")
    wl = _workload(tmp_path, "1.0.0", "s")
    try:
        drv.stage(None, wl)
        data = drv.data_dir("cap")
        assert data.is_dir() and not (data / "env-s.json").exists()            # created at install, not started
        drv.start("cap")
        _env(data, "s")
        if PROCFS:                                                             # restart by supervise(): same variable
            (data / "env-s.json").unlink()
            os.killpg(drv.units["cap"]["pid"], 9)
            assert wait_until(lambda: not drv.status("cap").running, 5, 0.05)
            assert wait_until(lambda: (drv.supervise() or True) and (data / "env-s.json").exists(), 10, 0.1)
            assert _env(data, "s")["FLEX_DATA_DIR"] == str(data)
    finally:
        drv.remove("cap")

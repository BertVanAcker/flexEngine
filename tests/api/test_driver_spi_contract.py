"""CP-5 driver SPI v1 (driver-spi.v1.schema.json): its x-flex-spi member table equals the Driver class of
drivers/base.py, its $defs carry exactly the fields of the SPI dataclasses, and what the in-tree fake driver and the
reconciler exchange validates."""

from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path

from flexengine2 import contracts
from flexengine2.drivers import base

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from fe_unit_fakes import FakeDriver, deployment, make_engine, set_desired  # noqa: E402

SCHEMA = contracts.load("driver-spi")
SPI = SCHEMA["x-flex-spi"]


def _sig(fn) -> str:
    return str(inspect.signature(fn)).replace("'", "")


def test_version_matches_base():
    assert SPI["version"] == base.SPI_VERSION


def test_members_equal_the_driver_class():
    abstract = set(base.Driver.__abstractmethods__)
    methods = {n: f for n, f in inspect.getmembers(base.Driver, inspect.isfunction) if not n.startswith("_")}
    assert set(SPI["required"]) == abstract
    assert set(SPI["required"]) | set(SPI["optional"]) == set(methods)
    for name, sig in {**SPI["required"], **SPI["optional"]}.items():
        assert _sig(methods[name]) == sig, f"{name}: base.py {_sig(methods[name])} != spec {sig}"
    for name, spec in SPI["attributes"].items():
        assert getattr(base.Driver, name) == spec["default"], name
    public_attrs = {n for n, v in vars(base.Driver).items()
                    if not n.startswith("_") and not callable(v) and not isinstance(v, (property, classmethod))}
    assert public_attrs == set(SPI["attributes"])
    for feature, meths in SPI["features"].items():
        assert set(meths) <= set(SPI["optional"])
        assert not base.supports(base.Driver, feature)


def test_errors_exist():
    for name in SPI["errors"]:
        assert issubclass(getattr(base, name), base.DriverError) or getattr(base, name) is base.DriverError


def test_defs_carry_the_dataclass_fields():
    for cls in (base.Workload, base.Observed, base.BuildRequest, base.BuildResult):
        props = set(SCHEMA["$defs"][cls.__name__]["properties"])
        fields = {f.name for f in dataclasses.fields(cls)} - {"emit"}
        assert props == fields, f"{cls.__name__}: schema {sorted(props)} != dataclass {sorted(fields)}"
        req = {f.name for f in dataclasses.fields(cls)
               if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
        assert set(SCHEMA["$defs"][cls.__name__]["required"]) == req, cls.__name__


def test_what_the_reconciler_passes_and_the_driver_returns_validates(tmp_path):
    seen = []

    class Recording(FakeDriver):
        def apply(self, current, desired):
            seen.append(desired.to_dict())
            super().apply(current, desired)

    drv = Recording()
    e = make_engine(tmp_path, drivers={"native": drv}, healthEveryTicks=100)
    set_desired(e, 1, [deployment("d1")])
    e.tick()
    assert seen
    for wl in seen:
        contracts.validate("driver-spi#Workload", wl)
    contracts.validate("driver-spi#Observed", dataclasses.asdict(drv.status("d1")))
    contracts.validate("driver-spi#Observed", dataclasses.asdict(drv.status("unknown")))
    contracts.validate("driver-spi#CapabilitiesFragment", drv.capabilities())
    contracts.validate("driver-spi#BuildResult", dataclasses.asdict(base.BuildResult(digest="sha256:" + "0" * 64)))
    req = base.BuildRequest(id="b1", source_path="/s.tgz", spec={"command": "make"}, work_dir="/w")
    contracts.validate("driver-spi#BuildRequest", {k: v for k, v in dataclasses.asdict(req).items() if k != "emit"})


def test_supervise_events_of_the_process_driver_validate():
    for ev in ({"dep": "d1", "event": "restarted", "restarts": 2}, {"dep": "d1", "event": "start-limit-hit"},
               {"dep": "d1", "event": "exited", "willRestartAt": 12.5},
               {"dep": "d1", "event": "boot-confirmed", "slot": "B", "version": "2"}):
        contracts.validate("driver-spi#SuperviseEvent", ev)
    assert not contracts.is_valid("driver-spi#SuperviseEvent", {"dep": "d1", "event": "exploded"})

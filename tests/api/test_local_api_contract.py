"""CP-7 conformance (EV-07): every response of the local API, over the real Unix socket and the TCP bind, matches
local-api.v1.openapi.yaml (status code listed for the operation, content type, body schema with the referenced
CP-2/CP-7 schema files), and every operation of the spec is exercised."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from flexengine2 import contracts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from _local_fixtures import BUNDLE, DEVICE, TAMPERED, installed_engine, write_config  # noqa: E402

SPEC_PATH = contracts.api_dir() / "local-api.v1.openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text())
BASE = "https://flex.dev/schemas/local-api.v1.openapi.yaml"


def _registry() -> Registry:
    reg = Registry()
    for p in contracts.api_dir().glob("*.schema.json"):
        doc = json.loads(p.read_text())
        reg = reg.with_resource(doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
    return reg.with_resource(BASE, Resource.from_contents(SPEC, default_specification=DRAFT202012))


REG = _registry()
SEEN: set[tuple[str, str, str]] = set()


def _ptr(*parts) -> str:
    return "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _operation(method: str, path: str) -> tuple[str, dict]:
    for tmpl, item in SPEC["paths"].items():
        rx = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(tmpl)) + "$"
        if re.match(rx, path) and method.lower() in item:
            return tmpl, item[method.lower()]
    raise AssertionError(f"{method} {path} is not in the spec")


def conform(method: str, target: str, status: int, ctype: str, body: bytes):
    path = target.split("?")[0]
    tmpl, op = _operation(method, path)
    responses = op["responses"]
    assert str(status) in responses, f"{method} {tmpl}: status {status} not in the spec ({sorted(responses)})"
    SEEN.add((method.upper(), tmpl, str(status)))
    resp = responses[str(status)]
    ref_ptr = ("paths", tmpl, method.lower(), "responses", str(status))
    if "$ref" in resp:
        name = resp["$ref"].rsplit("/", 1)[1]
        resp = SPEC["components"]["responses"][name]
        ref_ptr = ("components", "responses", name)
    content = resp.get("content")
    if not content:
        return
    mt = ctype.split(";")[0].strip()
    assert mt in content, f"{method} {tmpl} {status}: content type {mt} not in {sorted(content)}"
    if mt == "application/x-ndjson":
        for line in body.splitlines():
            assert isinstance(json.loads(line), dict)
        return
    schema = {"$ref": f"{BASE}#/{_ptr(*ref_ptr, 'content', mt, 'schema')}"}
    v = jsonschema.Draft202012Validator(schema, registry=REG, format_checker=jsonschema.FormatChecker())
    errs = sorted(v.iter_errors(json.loads(body)), key=lambda e: list(e.absolute_path))
    assert not errs, f"{method} {tmpl} {status}: " + "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errs[:5])


def test_spec_is_valid_openapi():
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    doc, url = read_from_filename(str(SPEC_PATH))
    validate(doc, base_uri=SPEC_PATH.as_uri())


@pytest.fixture
def served(tmp_path, sock_dir):
    from flexengine2.local.api import ApiClient, EngineProvider, LocalApi

    cfg, _ = write_config(tmp_path)
    eng = installed_engine(cfg)
    tok = tmp_path / "token"
    tok.write_text("tok\n")
    os.chmod(tok, 0o600)
    api = LocalApi(EngineProvider([eng], cfg, config_file="engine.yaml"), socket_path=str(sock_dir / "api.sock"),
                   tcp_bind="127.0.0.1:0", token_file=str(tok), log=lambda m: None).start()
    yield cfg, eng, ApiClient(str(sock_dir / "api.sock")), api.tcp_address[1]
    api.stop()


def sock(client, method, target, body=b"", ctype=None):
    r = client.request(method, target, body, ctype)
    data = r.body if not r.streaming else b"".join(r.body)
    conform(method, target, r.status, r.ctype, data)
    return r.status, data


def tcp(port, method, target, token="tok", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{target}", method=method, data=body,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            st, ct, data = r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        st, ct, data = e.code, e.headers.get("Content-Type", ""), e.read()
    conform(method, target, st, ct, data)
    return st, data


def test_every_response_conforms(served):
    cfg, eng, c, port = served
    tar = BUNDLE.read_bytes()
    reads = ["/v1/health", "/v1/status", "/v1/history?since=0", f"/v1/history?device={DEVICE}", "/v1/logs",
             f"/v1/logs?device={DEVICE}&tail=1", f"/v1/capabilities/{DEVICE}", "/v1/metrics",
             f"/v1/devices/{DEVICE}/deployments/native-app", f"/v1/devices/{DEVICE}/deployments/safety-app"]
    for t in reads:
        assert sock(c, "GET", t)[0] == 200, t
        assert tcp(port, "GET", t)[0] == 200, t
    for t in (f"/v1/devices/{DEVICE}/deployments/nope", "/v1/devices/nope/deployments/x", "/v1/capabilities/nope",
              "/v1/history?device=nope", "/v1/logs?device=nope"):
        assert sock(c, "GET", t)[0] == 404, t
    for t in ("/v1/history?since=x", "/v1/logs?follow=7"):
        assert sock(c, "GET", t)[0] == 400, t
    assert tcp(port, "GET", "/v1/status", token=None)[0] == 401
    assert tcp(port, "GET", "/v1/health", token="bad")[0] == 401
    # writes: socket only
    assert sock(c, "POST", "/v1/bundle", tar, "application/x-tar")[0] == 202
    assert sock(c, "POST", f"/v1/bundle?device={DEVICE}", tar, "application/x-tar")[0] == 202
    assert sock(c, "POST", "/v1/bundle", TAMPERED.read_bytes(), "application/x-tar")[0] == 422
    assert sock(c, "POST", "/v1/bundle", b"", "application/x-tar")[0] == 400
    assert tcp(port, "POST", "/v1/bundle", body=tar)[0] == 403
    assert sock(c, "POST", "/v1/reconcile")[0] == 202
    assert tcp(port, "POST", "/v1/reconcile", body=b"")[0] == 403


def test_offline_responses_conform(tmp_path):
    """The CLI's in-process mode answers with the same contract (no engine: 503 where the live engine is needed)."""
    from flexengine2.local.api import LocalApi, OfflineProvider

    cfg, _ = write_config(tmp_path)
    installed_engine(cfg)
    api = LocalApi(OfflineProvider(cfg), socket_path=None)
    want = {"/v1/health": 503, "/v1/status": 200, "/v1/history?since=0": 200, "/v1/logs": 200,
            f"/v1/capabilities/{DEVICE}": 503, "/v1/metrics": 503,
            f"/v1/devices/{DEVICE}/deployments/native-app": 200}
    for t, st in want.items():
        r = api.handle("GET", t)
        assert r.status == st, t
        conform("GET", t, r.status, r.ctype, r.body)
    for t, st in (("/v1/reconcile", 503), ("/v1/bundle", 202)):
        r = api.handle("POST", t, BUNDLE.read_bytes() if "bundle" in t else b"")
        assert r.status == st, t
        conform("POST", t, r.status, r.ctype, r.body)


def test_all_operations_covered():
    ops = {(m.upper(), p) for p, item in SPEC["paths"].items() for m in item if m in ("get", "post")}
    seen = {(m, p) for m, p, _ in SEEN}
    assert ops <= seen, f"operations never exercised: {sorted(ops - seen)}"


def test_resources_and_workload_logs_over_the_tcp_bind(tmp_path, sock_dir):
    """UM-4, as a monitor reads them: GET /v1/metrics carries `resources` and `deploymentResources` (telemetry v1
    Metrics), GET /v1/logs the workloads' lines as source `workload` (WorkloadLogRecord), also followed."""
    from flexengine2.local.api import EngineProvider, LocalApi
    from flexengine2.testing.observe import ObservableFakeDriver

    cfg, _ = write_config(tmp_path)
    drv = ObservableFakeDriver()
    eng = installed_engine(cfg, drv)
    drv.set_resources("native-app", cpuPercent=3.5, memoryBytes=50 << 20, memoryLimitBytes=1 << 30, pids=2)
    drv.write("native-app", "listening on 8080")
    drv.write("native-app", "warning: slow disk", stream="stderr")
    eng.observer.step()
    tok = tmp_path / "token"
    tok.write_text("tok\n")
    os.chmod(tok, 0o600)
    api = LocalApi(EngineProvider([eng], cfg), socket_path=None, tcp_bind="127.0.0.1:0", token_file=str(tok),
                   log=lambda m: None).start()
    try:
        port = api.tcp_address[1]
        st, data = tcp(port, "GET", "/v1/metrics")
        assert st == 200
        m = json.loads(data)[DEVICE]
        contracts.validate("telemetry#Metrics", m)
        assert m["deploymentResources"]["native-app"]["memoryBytes"] == 50 << 20
        if os.path.exists("/proc/meminfo"):
            assert m["resources"]["memory"]["totalBytes"] > 0
        st, data = tcp(port, "GET", f"/v1/logs?device={DEVICE}&tail=50")
        lines = [json.loads(x) for x in data.splitlines()]
        assert {x["source"] for x in lines} <= {"actions", "log", "workload"}
        wl = [x for x in lines if x["source"] == "workload"]
        assert [(x["msg"], x.get("stream")) for x in wl] == [("listening on 8080", "stdout"),
                                                             ("warning: slow disk", "stderr")]
        for x in wl:
            contracts.validate("telemetry#WorkloadLogRecord", x)
    finally:
        api.stop()

"""EV-11/EV-12 shared helpers: a capturing reverse proxy in front of the reference fake cloud, a validator of captured
exchanges against `openapi-southbound.yaml` (+ the CP schemas it `$ref`s), and a small lab (fake cloud seeded with
the vector releases, extra releases signed by the vector release key, an admin client, a version-aware FakeDriver).

Imported by tests/api/test_conformance_capture.py and tests/security/test_security_suite.py (which puts this
directory on sys.path).
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import http.client
import io
import json
import re
import shutil
import socket
import tarfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import jsonschema
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from flexengine2 import contracts
from flexengine2.artefacts.oci import MT_PAYLOAD_NATIVE, Layout
from flexengine2.testing import release as reltool
from flexengine2.testing.fakecloud import CloudServer
from flexengine2.util import canonical_json

from _api_helpers import VECTORS, FakeDriver

SPEC_PATH = contracts.api_dir() / "openapi-southbound.yaml"
ADMIN_TOKEN = "admin-secret"
KEYS = json.loads((VECTORS / "keys.json").read_text())
TRUSTED_RELEASE_KEYS = json.loads((VECTORS / "release" / "cases.json").read_text())["trustedReleaseKeys"]
CLOUD_PUB = KEYS["cloud"]["public"]


def vkey(role: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEYS[role]["private"]))


# ---- capture ------------------------------------------------------------------------------------------------------

@dataclass
class Exchange:
    method: str
    path: str                      # as sent (path + query): what the signature covers
    req_headers: dict              # lower-case names
    req_body: bytes
    status: int | None             # None: dropped by the proxy (link down), no response
    resp_headers: dict = field(default_factory=dict)
    resp_body: bytes = b""
    tag: str = ""                  # test phase label

    @property
    def device(self) -> str | None:
        return self.req_headers.get("x-flex-device")


class CaptureProxy:
    """HTTP reverse proxy: forwards every request unchanged (so request signatures stay valid) to the upstream and
    records request + response. `down = True` drops requests without an answer (link cut); `mutate(ex)` may change
    what the engine receives (MITM) and returns (status, headers list, body)."""

    def __init__(self, upstream_port: int):
        self.upstream = upstream_port
        self.exchanges: list[Exchange] = []
        self.lock = threading.Lock()
        self.down = False
        self.mutate = None
        self.tag = ""
        proxy = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req_h = {k.lower(): v for k, v in self.headers.items()}
                ex = Exchange(self.command, self.path, req_h, body, None, tag=proxy.tag)
                if proxy.down:
                    proxy._record(ex)
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                fwd = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "keep-alive", "host")}
                fwd["Connection"] = "close"
                c = http.client.HTTPConnection("127.0.0.1", proxy.upstream, timeout=30)
                c.request(self.command, self.path, body=body, headers=fwd)
                r = c.getresponse()
                data = r.read()
                hdrs = [(k, v) for k, v in r.getheaders()
                        if k.lower() not in ("connection", "transfer-encoding", "server", "date", "content-length")]
                status = r.status
                c.close()
                if proxy.mutate is not None:
                    got = proxy.mutate(Exchange(self.command, self.path, req_h, body, status,
                                                {k.lower(): v for k, v in hdrs}, data))
                    if got is not None:
                        status, hdrs, data = got
                ex.status, ex.resp_headers, ex.resp_body = status, {k.lower(): v for k, v in hdrs}, data
                proxy._record(ex)
                self.send_response(status)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                if data and status != 304:
                    self.wfile.write(data)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def _record(self, ex: Exchange):
        with self.lock:
            self.exchanges.append(ex)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def since(self, n: int) -> list[Exchange]:
        with self.lock:
            return list(self.exchanges[n:])


# ---- OpenAPI southbound validator ---------------------------------------------------------------------------------

@dataclass
class Checked:
    op: str | None                 # operationId (or "METHOD template")
    template: str | None
    status: int | None
    problems: list[str]
    notes: list[str]


class Southbound:
    """Matches a captured exchange to an operation of openapi-southbound.yaml and validates parameters, security
    headers, request body, response status, response headers and JSON response body against the referenced schemas
    (relative `$ref`s are resolved to the package-data schema `$id`s; whole-document refs also run the semantic rules
    of flexengine2.contracts)."""

    METHODS = ("get", "put", "post", "delete", "patch", "head")

    def __init__(self, path: Path = SPEC_PATH):
        self.spec = yaml.safe_load(path.read_text())
        self.files = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in contracts.api_dir().glob("*.schema.json")}
        self.ids = {n: d["$id"] for n, d in self.files.items()}
        self.names = {n: re.split(r"\.v[0-9]", n, maxsplit=1)[0] for n in self.files}
        self.registry = Registry().with_resources(
            (d["$id"], Resource.from_contents(d, default_specification=DRAFT202012)) for d in self.files.values())
        self.ops = []
        for tmpl, item in self.spec["paths"].items():
            rx = re.escape(tmpl)
            for name in re.findall(r"\{([^}]+)\}", tmpl):
                rx = rx.replace(re.escape("{" + name + "}"), f"(?P<{name}>.+)" if name == "repository" else f"(?P<{name}>[^/]+)")
            for m in self.METHODS:
                if m in item:
                    op = item[m]
                    self.ops.append({"method": m.upper(), "template": tmpl, "rx": re.compile(rx), "op": op,
                                     "item": item, "id": op.get("operationId") or f"{m.upper()} {tmpl}"})

    # -- refs ---------------------------------------------------------------------------------------------------
    def deref(self, obj):
        while isinstance(obj, dict) and isinstance(obj.get("$ref"), str) and obj["$ref"].startswith("#/"):
            node = self.spec
            for part in obj["$ref"][2:].split("/"):
                node = node[part]
            obj = node
        return obj

    def _schema(self, s):
        """Copy of an OpenAPI schema with every $ref turned into an absolute schema $id (components inlined)."""
        if isinstance(s, list):
            return [self._schema(x) for x in s]
        if not isinstance(s, dict):
            return s
        if "$ref" in s and isinstance(s["$ref"], str):
            ref = s["$ref"]
            if ref.startswith("#/"):
                return self._schema(self.deref(s))
            file, _, frag = ref.partition("#")
            return {"$ref": self.ids[file] + (f"#{frag}" if frag else "")}
        return {k: self._schema(v) for k, v in s.items()}

    def _contract_name(self, s) -> str | None:
        s = self.deref(s)
        if isinstance(s, dict) and set(s) == {"$ref"} and not s["$ref"].startswith("#"):
            file, _, frag = s["$ref"].partition("#")
            m = re.fullmatch(r"/\$defs/([^/]+)", frag) if frag else None
            if not frag:
                return self.names[file]
            if m:
                return f"{self.names[file]}#{m[1]}"
        return None

    def schema_errors(self, s, instance) -> list[str]:
        name = self._contract_name(s)
        if name:
            return contracts.errors(name, instance)
        v = jsonschema.Draft202012Validator({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                             **self._schema(s)}, registry=self.registry,
                                            format_checker=jsonschema.FormatChecker())
        return [f"{'/'.join(map(str, e.absolute_path)) or '$'}: {e.message}" for e in v.iter_errors(instance)]

    # -- matching -----------------------------------------------------------------------------------------------
    def match(self, method: str, path: str):
        p = urlsplit(path).path
        hits = [(o, m.groupdict()) for o in self.ops if o["method"] == method.upper() and (m := o["rx"].fullmatch(p))]
        return hits

    def device_operations(self) -> set[str]:
        """Operations of the device (southbound): signed with the device key, or the unsigned onboarding."""
        out = set()
        for o in self.ops:
            sec = [list(x) for x in o["op"].get("security", [])]
            if ["deviceSignature"] in sec or o["template"] == "/api/v1/onboarding":
                out.add(o["id"])
        return out

    def check(self, ex: Exchange) -> Checked:
        hits = self.match(ex.method, ex.path)
        if len(hits) != 1:
            return Checked(None, None, ex.status, [f"{ex.method} {ex.path}: {len(hits)} matching operations"], [])
        o, params = hits[0]
        op, probs, notes = o["op"], [], []
        # parameters (path item + operation)
        for prm in [self.deref(p) for p in o["item"].get("parameters", []) + op.get("parameters", [])]:
            loc, name = prm["in"], prm["name"]
            if loc == "path":
                val = unquote(params[name])
            elif loc == "header":
                val = ex.req_headers.get(name.lower())
                if val is None:
                    if prm.get("required"):
                        probs.append(f"required header {name} missing")
                    continue
            else:
                continue
            for e in self.schema_errors(prm.get("schema", {}), val):
                probs.append(f"{loc} parameter {name}={val!r}: {e}")
        # security
        sec = [list(x) for x in op.get("security", [])]
        if ["deviceSignature"] in sec:
            sig = ex.req_headers.get("x-flex-signature")
            if not sig:
                probs.append("X-Flex-Signature missing (deviceSignature)")
            else:
                probs += [f"X-Flex-Signature: {e}" for e in self.schema_errors({"$ref": "trust.v1alpha1.schema.json#/$defs/Ed25519Signature"}, sig)]
        elif not sec and "x-flex-signature" in ex.req_headers and o["template"] == "/api/v1/onboarding":
            probs.append("onboarding is documented as unsigned but carries X-Flex-Signature")
        # request body
        rb = self.deref(op.get("requestBody")) if op.get("requestBody") else None
        if rb and rb.get("required") and not ex.req_body:
            probs.append("required request body missing")
        if ex.req_body:
            if not rb:
                probs.append("request body sent, none documented")
            else:
                ctype = (ex.req_headers.get("content-type") or "").split(";")[0].strip()
                media = rb.get("content", {})
                if ctype not in media:
                    probs.append(f"request Content-Type {ctype!r} not in {sorted(media)}")
                elif media[ctype].get("schema") is not None:
                    try:
                        doc = json.loads(ex.req_body)
                    except ValueError as e:
                        probs.append(f"request body is not JSON: {e}")
                    else:
                        probs += [f"request body: {e}" for e in self.schema_errors(media[ctype]["schema"], doc)]
        # response
        if ex.status is None:
            return Checked(o["id"], o["template"], None, probs, ["dropped (no response)"])
        responses = op.get("responses", {})
        resp = responses.get(str(ex.status)) or responses.get("default")
        if resp is None:
            probs.append(f"response status {ex.status} not documented (documented: {sorted(responses)})")
            return Checked(o["id"], o["template"], ex.status, probs, notes)
        resp = self.deref(resp)
        for hname, h in (resp.get("headers") or {}).items():
            h = self.deref(h)
            val = ex.resp_headers.get(hname.lower())
            if val is None:
                continue
            schema = self.deref(h.get("schema", {}))
            inst = int(val) if isinstance(schema, dict) and schema.get("type") == "integer" and val.isdigit() else val
            probs += [f"response header {hname}={val!r}: {e}" for e in self.schema_errors(schema, inst)]
        content = resp.get("content")
        if content:
            ctype = (ex.resp_headers.get("content-type") or "").split(";")[0].strip()
            if ctype not in content:
                probs.append(f"response Content-Type {ctype!r} not in {sorted(content)}")
            elif content[ctype].get("schema") is not None:
                try:
                    doc = json.loads(ex.resp_body)
                except ValueError as e:
                    probs.append(f"response body is not JSON: {e}")
                else:
                    sch = content[ctype]["schema"]
                    if "oneOf" in sch:
                        errs = [self.schema_errors(alt, doc) for alt in sch["oneOf"]]
                        if sum(1 for x in errs if not x) != 1:
                            probs.append(f"response body matches {sum(1 for x in errs if not x)} of the oneOf "
                                         f"alternatives: {[x[:2] for x in errs]}")
                    else:
                        probs += [f"response body: {e}" for e in self.schema_errors(sch, doc)]
        elif ex.resp_body and ex.status != 304:
            notes.append(f"{ex.status} carries an undocumented body "
                         f"({(ex.resp_headers.get('content-type') or '?').split(';')[0]})")
        return Checked(o["id"], o["template"], ex.status, probs, notes)


# ---- lab: fake cloud + releases + engines -------------------------------------------------------------------------

def admin(srv: CloudServer, method: str, path: str, body=None, user: str = "carol"):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    c.request(method, path, body=json.dumps(body).encode() if body is not None else b"",
              headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "X-Flex-User": user,
                       "Content-Type": "application/json", "Connection": "close"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data and "json" in (r.getheader("Content-Type") or "") else data)


def token(srv: CloudServer, dev: str) -> str:
    s, b = admin(srv, "POST", "/api/v1/admin/onboarding-tokens", {"deviceId": dev})
    assert s == 201, b
    return b["token"]


def deploy(srv: CloudServer, dev: str, items: list[dict], approver: str = "dave", unknown: bool = False) -> dict:
    s, p = admin(srv, "POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items, "allowUnknownDevice": unknown})
    assert s == 201, p
    if p["requiresApproval"]:
        s, b = admin(srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/approve", {}, approver)
        assert s == 200, b
    s, b = admin(srv, "POST", f"/api/v1/admin/plans/{p['planHash']}/apply", {})
    assert s == 200, b
    return b


def start_cloud(d: Path) -> CloudServer:
    """Reference fake cloud with the vector OCI layout (vec_native, vec_unsigned, vec_rogue, vec_badsig, vec_safety)
    and the vector cloud key; no model (safety classes come from the release descriptors)."""
    d.mkdir(parents=True, exist_ok=True)
    shutil.copytree(VECTORS / "release" / "oci", d / "oci")
    return CloudServer(d, 0, ADMIN_TOKEN, vkey("cloud")).start()


def descriptor(module: str, version: str, safety: str = "none", timeout: float = 8) -> dict:
    """Release descriptor in the vector style (no flexModel needed)."""
    return {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module, "moduleVersion": "0.1.0",
            "releaseVersion": version, "interfaceHash": "FLEX01_" + hashlib.sha256(module.encode()).hexdigest(),
            "language": "python", "profiles": ["native"],
            "health": {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.1,
                       "successThreshold": 2, "timeoutSeconds": timeout},
            "restartPolicy": {"policy": "on-failure", "maxRetries": 5, "restartSec": 0.5},
            "safetyClass": safety, "safetyClassSource": "test (EV-11/EV-12)"}


def tiny_native_payload(module: str, version: str) -> bytes:
    files = {"app.py": f"# {module} {version}\nprint('ok')\n".encode(),
             "release.json": canonical_json({"app": module, "version": version})}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(files):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mtime, ti.mode = len(files[name]), 0, 0o644
            tf.addfile(ti, io.BytesIO(files[name]))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def publish(srv: CloudServer, module: str, version: str, *, runnable: bool = False, fail_health: bool = False,
            safety: str = "none", approvers: list[str] | None = None, timeout: float = 8, key: str = "release") -> dict:
    """Add a release to the fake's registry, signed by a vector key. `runnable`: the P2 native stand-in app (serves
    /healthz; `fail_health` keeps it unready) for the real process driver."""
    layout: Layout = srv.state.layout
    payload = (reltool.build_native_payload(module, version, fail_health=fail_health) if runnable
               else tiny_native_payload(module, version))
    repo = f"flexnodes/{module}"
    m = reltool.publish_release(layout, repo, descriptor(module, version, safety, timeout),
                                [(MT_PAYLOAD_NATIVE, payload, {"dev.flex.profile": "native",
                                                               "dev.flex.platform": "linux/any+python3"})],
                                reltool.sbom_stub(module, version, "native"))
    approval = {"approved": True, "approvers": approvers, "role": "safety-engineer"} if approvers else None
    reltool.sign_release(layout, vkey(key), repo, m, version, approval)
    return m


def engine_cfg(data: Path, dev: str, api_url: str, **kw) -> dict:
    cfg = {"deviceId": dev, "dataDir": str(data), "apiUrl": api_url, "cloudPublicKey": CLOUD_PUB,
           "releasePublicKeys": list(TRUSTED_RELEASE_KEYS), "httpTimeout": 5, "pollInterval": 0.2,
           "backoff": [0.1, 0.5]}
    cfg.update(kw)
    return cfg


class VersionFakeDriver(FakeDriver):
    """FakeDriver whose health fails (fail-fast, with a terminal reason) for the listed versions only, so a failed
    update can be rolled back to a healthy previous version."""

    def __init__(self, profile: str = "native", bad_versions: set[str] | None = None):
        super().__init__(profile)
        self.bad = set(bad_versions or ())

    def _bad(self, dep_id):
        return (self.running.get(dep_id) or {}).get("version") in self.bad

    def health(self, dep_id, probe):
        return dep_id in self.running and not self._bad(dep_id)

    def failed(self, dep_id):
        return "process exited (Restart=never)" if self._bad(dep_id) else None


def actions(data: Path) -> list[dict]:
    p = Path(data) / "actions.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def wait(pred, timeout: float = 30, period: float = 0.05, what: str = ""):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(period)
    raise AssertionError(f"timeout waiting for {what or pred}")


def inject_desired(srv: CloudServer, dev: str, manifest_version: int, deployments: list[dict]):
    """What a compromised / buggy backend (or a replayed old document) would serve: bypasses plan/apply, signed by
    the fake like any other desired state."""
    with srv.state.lock:
        srv.state.s["desired"][dev] = {"manifestVersion": manifest_version, "deployments": copy.deepcopy(deployments)}

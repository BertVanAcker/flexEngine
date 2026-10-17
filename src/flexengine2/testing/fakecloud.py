"""flexCloud desired-state API stub (Margo-shaped, stdlib only).

Southbound (device) API, every request Ed25519-signed by the device key registered at onboarding:
  POST /api/v1/onboarding                                   one-time token + device public key
  PUT  /api/v1/devices/{id}/capabilities                    capabilities document (POST accepted too)
  GET  /api/v1/devices/{id}/deployments                     desired state; ETag / If-None-Match -> 304
  POST /api/v1/devices/{id}/deployments/{dep}/status        deployment status (idempotent by seq)
  POST /api/v1/devices/{id}/events                          device event, e.g. DesiredStateRefused (same seq space)
  GET  /api/v1/devices/{id}/bundle                          offline bundle (manifest + all blobs, tar)
  GET  /v2/{repo}/manifests/{digest} | blobs/{digest} | referrers/{digest}   OCI distribution subset
Northbound (operator / console / P6 MCP gateway), bearer admin token:
  POST /api/v1/admin/onboarding-tokens                      issue a one-time onboarding token
  GET  /api/v1/admin/devices[/{id}]                         fleet.read
  POST /api/v1/admin/plans                                  deploy.plan -> {planHash, diff, requiresApproval}
  POST /api/v1/admin/plans/{hash}/approve                   second person (approver != requester)
  POST /api/v1/admin/plans/{hash}/apply                     deploy.apply bound to the plan hash; body may carry
                                                            {approvals: [signed console approvals]} or
                                                            {preauthorized: "rollback-known-good"} (S4)
  POST /api/v1/admin/releases                               upload a release (tar of an OCI layout) (S4)
  GET  /api/v1/admin/audit                                  hash-chained audit log
  POST /api/v1/admin/faults                                 fault injection for tests (503 + Retry-After)
Errors are RFC 9457 problem+json.

S4 additions:
* Safety class from the model: with `model_dirs`, the stub derives every module's class with `flex safety`
  (the rule of flexmodel/safety.py). A release whose descriptor declares another class is refused at upload
  and at plan time, and a plan item that declares a class different from the model is refused (422).
* Backend-side approval verification: with `approval_policy` ({keys: [console public keys], standard,
  safety, requesterMayApprove}) `apply` needs approvals signed by a trusted console key over exactly this
  plan hash (TTL, distinct humans, roles, requester excluded), or a verifiable known-good rollback. The
  admin token alone is then no longer enough (P6 report section 10.1, U1).
* `--bind`: listen address (default 127.0.0.1). Binding to other interfaces is for the lab only and needs
  TLS termination and firewalling in front of this stdlib server, which itself speaks plain HTTP.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from flexengine2.identity import crypto
from flexengine2.util import atomic_write_json, canonical_json, iso, read_json, sha256_digest
from flexengine2.artefacts.oci import Layout

SKEW_S = 300
NONCE_CAP = 10000


class Problem(Exception):
    def __init__(self, status: int, title: str, detail: str = "", headers: dict | None = None):
        super().__init__(title)
        self.status, self.title, self.detail, self.headers = status, title, detail, headers or {}


class CloudState:
    """All control-plane state, persisted as one JSON file (stub; K2 says: a real DB)."""

    def __init__(self, data_dir: Path, admin_token: str, cloud_key, model_dirs: list[Path] | None = None,
                 approval_policy: dict | None = None):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.admin_token = admin_token
        self.key = cloud_key
        self.layout = Layout(self.dir / "oci")
        self.path = self.dir / "cloud-state.json"
        self.s = read_json(self.path) or {"devices": {}, "tokens": {}, "desired": {}, "plans": {},
                                          "status": {}, "statusLog": [], "lastSeq": {}, "audit": [],
                                          "publishedAt": {}}
        self.nonces: OrderedDict[str, float] = OrderedDict()
        self.faults = {"unavailableUntil": 0.0}
        self.counters: dict[str, int] = {}
        self.model_dirs = [Path(d) for d in model_dirs or []]
        self.safety: dict | None = None
        if self.model_dirs:
            from .release import model_safety
            self.safety = model_safety(*self.model_dirs)
        self.approval_policy = approval_policy

    # ---- safety class (S4) --------------------------------------------------------------------
    def model_class(self, module: str) -> str | None:
        """Model-derived class, or None when no model is configured or the module is not in it."""
        if self.safety is None:
            return None
        info = self.safety["modules"].get(module)
        return info["class"] if info else None

    def release_descriptor(self, desc: dict) -> dict:
        man = self.layout.get_manifest(desc["digest"])
        return json.loads(self.layout.get_blob(man["config"]["digest"]))

    def check_release_class(self, module: str, descriptor: dict) -> str:
        """The release's declared class must equal the model's. A composite release (P4: an App Lab App =
        several modules released as one artefact, `module` = the System) is checked member by member: every
        member must be in the model, and the App's class is the maximum over its members (P4 amendment)."""
        declared = descriptor.get("safetyClass", "none")
        members = composite_members(descriptor)
        if members and self.safety is not None:
            order = {"none": 0, "safety-relevant": 1}
            unknown = sorted({m for m in members.values() if self.model_class(m) is None})
            if unknown:
                raise Problem(422, "Composite release member not in the model",
                              f"{module} {descriptor.get('releaseVersion')}: members {unknown} are not modules "
                              f"of the configured model")
            derived = max((self.model_class(m) for m in members.values()), key=order.get)
            if declared != derived:
                worst = sorted(i for i, m in members.items() if self.model_class(m) == derived)
                raise Problem(422, "Release safety class differs from the model",
                              f"{module} {descriptor.get('releaseVersion')}: release declares {declared!r}, model "
                              f"derives {derived!r} (max over composite members; {derived!r} from {worst})")
            return declared
        derived = self.model_class(module)
        if derived is not None and declared != derived:
            raise Problem(422, "Release safety class differs from the model",
                          f"{module} {descriptor.get('releaseVersion')}: release declares {declared!r}, "
                          f"model derives {derived!r}")
        return declared

    def check_placement(self, device_id: str, deployments: list[dict]):
        """Plan-time placement for exclusive executors (P4 amendment, Margo M8): a driver whose capability
        fragment says `exclusive` (App Lab: one app per board) gets at most `maxDeployments` deployments."""
        caps = ((self.s["devices"].get(device_id) or {}).get("capabilities") or {})
        drivers = (caps.get("x-flex") or {}).get("drivers") or {}
        by_driver: dict[str, list[str]] = {}
        for d in deployments:
            by_driver.setdefault(d["x-flex-driver"], []).append(d["metadata"]["id"])
        for drv, ids in sorted(by_driver.items()):
            frag = drivers.get(drv) or {}
            limit = frag.get("maxDeployments") if frag.get("exclusive") else None
            if limit is None and "maxRunningApps" in frag:  # P4 applab capability, before `exclusive` existed
                limit = frag["maxRunningApps"]
            if limit is not None and len(ids) > int(limit):
                raise Problem(422, "Placement refused: exclusive executor",
                              f"device {device_id}: driver {drv!r} runs at most {limit} deployment(s), the plan "
                              f"places {len(ids)}: {ids}")

    def deployment_class(self, module: str, version: str, item: dict, desc: dict) -> str:
        """Class a deployment is gated with: the release's (checked against the model); a plan item may
        repeat it but never declare another one."""
        released = self.check_release_class(module, self.release_descriptor(desc))
        declared = item.get("safetyClass")
        if declared is not None and declared != released:
            raise Problem(422, "Declared safety class differs from the model",
                          f"{module} {version}: plan declares {declared!r}, model/release say {released!r}")
        return released

    def save(self):
        atomic_write_json(self.path, self.s)

    def count(self, key: str):
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + 1

    # ---- audit (FC-S4: append-only, hash-chained) -----------------------------------------------
    def audit(self, actor: str, action: str, detail: dict):
        prev = self.s["audit"][-1]["hash"] if self.s["audit"] else "sha256:" + "0" * 64
        entry = {"seq": len(self.s["audit"]) + 1, "at": iso(), "actor": actor, "action": action,
                 "detail": detail, "prev": prev}
        entry["hash"] = sha256_digest(canonical_json(entry))
        self.s["audit"].append(entry)

    # ---- desired state ------------------------------------------------------------------------
    def desired_doc(self, device_id: str) -> dict:
        d = self.s["desired"].get(device_id) or {"manifestVersion": 0, "deployments": []}
        return {"apiVersion": "flex.dev/v1alpha1", "kind": "DesiredState", "deviceId": device_id,
                "manifestVersion": d["manifestVersion"], "deployments": d["deployments"]}

    def resolve_deployment(self, device_id: str, item: dict, base_url: str) -> dict:
        module, version = item["module"], item["version"]
        repo = f"flexnodes/{module}"
        desc = self.layout.resolve(f"{repo}:{version}")
        if not desc:
            raise Problem(422, "Unknown release", f"{repo}:{version} is not in the registry")
        dep_id = item.get("id") or item.get("name") or module
        dep = {
            "apiVersion": "application.margo.org/v1alpha1",
            "kind": "ApplicationDeployment",
            "metadata": {"id": dep_id, "name": item.get("name", dep_id), "namespace": "default",
                         "deviceId": device_id},
            "spec": {
                "applicationId": module,
                "deploymentProfile": {"type": "custom", "components": [{
                    "name": module,
                    "properties": {"repository": f"oci://{base_url}/{repo}", "revision": version,
                                   "digest": desc["digest"], "size": desc["size"]}}]},
                "parameters": item.get("parameters", {}),
            },
            "x-flex-driver": item.get("driver", "container"),
            "x-flex-safetyClass": self.deployment_class(module, version, item, desc),
        }
        if item.get("activation"):
            dep["x-flex-activation"] = item["activation"]
        return dep


def composite_members(descriptor: dict) -> dict[str, str]:
    """{instance: module} of a composite release; {} for a single-module release. Accepts the P4 shape
    {side: {instance: {module, ...}}} and a flat {instance: {module, ...}} / [{instance, module}] list."""
    comp = descriptor.get("composite")
    if not comp:
        return {}
    out: dict[str, str] = {}
    if isinstance(comp, list):
        for m in comp:
            out[m.get("instance") or m["module"]] = m["module"]
        return out
    for k, v in comp.items():
        if isinstance(v, dict) and "module" in v:
            out[k] = v["module"]
        elif isinstance(v, dict):
            for i, m in v.items():
                out[f"{k}.{i}" if i in out else i] = m["module"]
    return out


def _match(pattern: str, path: str):
    return re.fullmatch(pattern, path)


def make_handler(st: CloudState, base_url_holder: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "flexCloudStub/0.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ---- plumbing ---------------------------------------------------------------------
        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16 << 20:
                raise Problem(413, "Payload too large")
            return self.rfile.read(n) if n else b""

        def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None):
            self.send_response(status)
            if status != 304:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj, headers: dict | None = None):
            self._send(status, json.dumps(obj, indent=1).encode(), headers=headers)

        def _problem(self, p: Problem):
            body = json.dumps({"type": "about:blank", "title": p.title, "status": p.status,
                               "detail": p.detail}).encode()
            self._send(p.status, body, "application/problem+json", p.headers)

        def _device_auth(self, body: bytes) -> str:
            h = self.headers
            dev, date, nonce, sig = (h.get("X-Flex-Device"), h.get("X-Flex-Date"), h.get("X-Flex-Nonce"),
                                     h.get("X-Flex-Signature"))
            if not all([dev, date, nonce, sig]):
                raise Problem(401, "Unsigned request")
            info = st.s["devices"].get(dev)
            if not info:
                raise Problem(401, "Unknown device")
            if abs(time.time() * 1000 - int(date)) > SKEW_S * 1000:
                raise Problem(401, "Request time outside the allowed window")
            msg = crypto.request_string(self.command, self.path, date, nonce, body)
            if not crypto.verify(info["publicKey"], msg, sig):
                raise Problem(401, "Bad request signature")
            with st.lock:  # bounded replay cache (F3: "bounded replay window")
                key = f"{dev}:{nonce}"
                if key in st.nonces:
                    raise Problem(401, "Replayed request")
                st.nonces[key] = time.time()
                while len(st.nonces) > NONCE_CAP:
                    st.nonces.popitem(last=False)
                info["lastSeen"] = iso()
            return dev

        def _admin_auth(self) -> str:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {st.admin_token}":
                raise Problem(401, "Admin token required")
            return self.headers.get("X-Flex-User", "operator")

        def _scoped(self, caller: str, device_id: str):
            if caller != device_id:
                raise Problem(403, "Device may only access its own resources")

        def _faults(self):
            left = st.faults["unavailableUntil"] - time.time()
            if left > 0:
                raise Problem(503, "Service unavailable (injected)", headers={"Retry-After": str(max(1, int(left + 0.999)))})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method: str):
            try:
                path = urlsplit(self.path).path
                if method == "POST" and path == "/api/v1/admin/releases":
                    user = self._admin_auth()
                    return self._json(201, self._upload_release(user))
                body = self._body()
                if path.startswith("/api/v1/admin/"):
                    return self._admin(method, path, body)
                self._faults()
                return self._device(method, path, body)
            except Problem as p:
                self._problem(p)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._problem(Problem(400, "Bad request", repr(e)))

        def _upload_release(self, user: str) -> dict:
            """Import a tar of an OCI image layout (index.json + blobs/sha256/*) written by flexp2.publish."""
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 8 << 30:
                raise Problem(413 if n else 400, "Release upload must have a Content-Length up to 8 GiB")
            with tempfile.TemporaryDirectory() as tmp:
                tpath = Path(tmp) / "upload.tar"
                with open(tpath, "wb") as f:
                    left = n
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise Problem(400, "Truncated upload")
                        f.write(chunk)
                        left -= len(chunk)
                src = Path(tmp) / "layout"
                with tarfile.open(tpath) as tf:
                    tf.extractall(src, filter="data")
                idx = read_json(src / "index.json")
                if not idx:
                    raise Problem(422, "Upload has no index.json")
                blobs = {}
                for p in (src / "blobs" / "sha256").glob("*"):
                    data = p.read_bytes()
                    if sha256_digest(data) != "sha256:" + p.name:
                        raise Problem(422, "Blob digest mismatch", p.name)
                    blobs["sha256:" + p.name] = data
                entries = idx.get("manifests", [])
                # check every release before importing anything (safety class from the model)
                checked = []
                for e in entries:
                    man = json.loads(blobs[e["digest"]])
                    if man.get("artifactType") == "application/vnd.flex.node.v1+json":
                        desc = json.loads(blobs[man["config"]["digest"]])
                        st.check_release_class(desc["module"], desc)
                        checked.append(f"{desc['module']}:{desc['releaseVersion']} ({desc.get('safetyClass', 'none')})")
                with st.lock:
                    for d, data in blobs.items():
                        st.layout.put_blob(data)
                    out = []
                    for e in entries:
                        man = json.loads(blobs[e["digest"]])
                        ref = (e.get("annotations") or {}).get("org.opencontainers.image.ref.name")
                        got = st.layout.put_manifest(man, ref_name=ref)
                        if got["digest"] != e["digest"]:
                            raise Problem(422, "Manifest is not in canonical form", e["digest"])
                        out.append({"digest": got["digest"], "ref": ref})
                    st.audit(user, "release.uploaded", {"manifests": out, "releases": checked})
                    st.save()
            return {"imported": out, "releases": checked}

        # ---- southbound ----------------------------------------------------------------------
        def _device(self, method: str, path: str, body: bytes):
            if method == "POST" and path == "/api/v1/onboarding":
                req = json.loads(body)
                with st.lock:
                    dev = st.s["tokens"].pop(req["token"], None)
                    if dev is None or dev != req["deviceId"]:
                        raise Problem(403, "Invalid or used onboarding token")
                    crypto.load_public(req["publicKey"])  # validates the key
                    st.s["devices"][dev] = {"publicKey": req["publicKey"], "keyId": crypto.key_id(req["publicKey"]),
                                            "onboardedAt": iso(), "capabilities": None, "lastSeen": iso()}
                    st.audit(f"device:{dev}", "device.onboarded", {"keyId": crypto.key_id(req["publicKey"])})
                    st.save()
                return self._json(201, {"deviceId": dev, "cloudPublicKey": crypto.public_b64(st.key)})

            caller = self._device_auth(body)
            if m := _match(r"/api/v1/devices/([\w.-]+)/capabilities", path):
                if method not in ("PUT", "POST"):
                    raise Problem(405, "Method not allowed")
                self._scoped(caller, m[1])
                with st.lock:
                    st.s["devices"][caller]["capabilities"] = json.loads(body)
                    st.save()
                return self._json(200, {"ok": True})
            if (m := _match(r"/api/v1/devices/([\w.-]+)/deployments", path)) and method == "GET":
                self._scoped(caller, m[1])
                st.count(f"{caller}:desired")
                with st.lock:
                    doc = canonical_json(st.desired_doc(caller))
                etag = '"' + sha256_digest(doc) + '"'
                if self.headers.get("If-None-Match") == etag:
                    st.count(f"{caller}:desired304")
                    return self._send(304, headers={"ETag": etag})
                return self._send(200, doc, headers={"ETag": etag,
                                                     "X-Flex-Manifest-Signature": crypto.sign(st.key, doc)})
            if (m := _match(r"/api/v1/devices/([\w.-]+)/deployments/([\w.-]+)/status", path)) and method == "POST":
                self._scoped(caller, m[1])
                rep = json.loads(body)
                with st.lock:
                    seq = int(rep["seq"])
                    if seq <= st.s["lastSeq"].get(caller, 0):
                        st.count(f"{caller}:statusDuplicate")
                        return self._json(200, {"ok": True, "duplicate": True})
                    st.s["lastSeq"][caller] = seq
                    rep["receivedAt"] = time.time()
                    st.s["status"].setdefault(caller, {})[m[2]] = rep
                    st.s["statusLog"].append({"deviceId": caller, "deploymentId": m[2], **rep})
                    st.save()
                st.count(f"{caller}:status")
                return self._json(200, {"ok": True})
            if (m := _match(r"/api/v1/devices/([\w.-]+)/events", path)) and method == "POST":
                self._scoped(caller, m[1])
                ev = json.loads(body)
                with st.lock:
                    seq = int(ev["seq"])
                    if seq <= st.s["lastSeq"].get(caller, 0):
                        st.count(f"{caller}:eventDuplicate")
                        return self._json(200, {"ok": True, "duplicate": True})
                    st.s["lastSeq"][caller] = seq
                    ev["receivedAt"] = time.time()
                    st.s.setdefault("events", []).append({"deviceId": caller, **ev})
                    st.save()
                st.count(f"{caller}:event")
                return self._json(200, {"ok": True})
            if (m := _match(r"/api/v1/devices/([\w.-]+)/bundle", path)) and method == "GET":
                self._scoped(caller, m[1])
                return self._send(200, self._bundle(caller), "application/x-tar")
            if (m := _match(r"/v2/(.+)/(manifests|blobs)/(sha256:[0-9a-f]{64})", path)) and method == "GET":
                st.count(f"{caller}:{m[2]}")
                if not st.layout.has_blob(m[3]):
                    raise Problem(404, "Blob unknown")
                data = st.layout.get_blob(m[3])
                ctype = "application/vnd.oci.image.manifest.v1+json" if m[2] == "manifests" else "application/octet-stream"
                return self._send(200, data, ctype, {"Docker-Content-Digest": m[3]})
            if (m := _match(r"/v2/(.+)/referrers/(sha256:[0-9a-f]{64})", path)) and method == "GET":
                return self._json(200, st.layout.referrers(m[2]))
            raise Problem(404, "Not found", path)

        def _bundle(self, device_id: str) -> bytes:
            """Margo 'bundle': the desired state plus every blob it references, for poor/no links (UC1)."""
            with st.lock:
                doc = canonical_json(st.desired_doc(device_id))
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                def add(name, data):
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))
                add("desired.json", doc)
                add("desired.sig", crypto.sign(st.key, doc).encode())
                seen = set()
                for dep in json.loads(doc)["deployments"]:
                    for comp in dep["spec"]["deploymentProfile"]["components"]:
                        md = comp["properties"]["digest"]
                        todo = [md] + [r["digest"] for r in st.layout.referrers(md)["manifests"]]
                        for d in todo:
                            man = st.layout.get_manifest(d)
                            for b in [d, man["config"]["digest"]] + [l["digest"] for l in man["layers"]]:
                                if b not in seen:
                                    seen.add(b)
                                    add(f"blobs/{b.replace(':', '/')}", st.layout.get_blob(b))
            return buf.getvalue()

        # ---- northbound ---------------------------------------------------------------------
        def _admin(self, method: str, path: str, body: bytes):
            user = self._admin_auth()
            req = json.loads(body) if body else {}
            if method == "POST" and path == "/api/v1/admin/onboarding-tokens":
                tok = uuid.uuid4().hex
                with st.lock:
                    st.s["tokens"][tok] = req["deviceId"]
                    st.audit(user, "device.token-issued", {"deviceId": req["deviceId"]})
                    st.save()
                return self._json(201, {"token": tok, "deviceId": req["deviceId"]})
            if method == "GET" and path == "/api/v1/admin/devices":
                with st.lock:
                    return self._json(200, {"devices": [
                        {"deviceId": d, "lastSeen": i.get("lastSeen"), "keyId": i.get("keyId"),
                         "manifestVersion": st.s["desired"].get(d, {}).get("manifestVersion", 0),
                         "status": st.s["status"].get(d, {})} for d, i in st.s["devices"].items()]})
            if method == "GET" and (m := _match(r"/api/v1/admin/devices/([\w.-]+)", path)):
                with st.lock:
                    d = m[1]
                    return self._json(200, {"deviceId": d, "device": st.s["devices"].get(d),
                                            "desired": st.desired_doc(d), "status": st.s["status"].get(d, {}),
                                            "statusLog": [s for s in st.s["statusLog"] if s["deviceId"] == d],
                                            "publishedAt": st.s["publishedAt"].get(d, {}),
                                            "counters": {k: v for k, v in st.counters.items() if k.startswith(d + ":")}})
            if method == "GET" and (m := _match(r"/api/v1/admin/devices/([\w.-]+)/bundle", path)):
                st.audit(user, "bundle.exported", {"deviceId": m[1]})
                return self._send(200, self._bundle(m[1]), "application/x-tar")
            if method == "GET" and path == "/api/v1/admin/audit":
                return self._json(200, {"entries": st.s["audit"]})
            if method == "POST" and path == "/api/v1/admin/faults":
                st.faults["unavailableUntil"] = time.time() + float(req.get("unavailableSeconds", 0))
                return self._json(200, {"ok": True})
            if method == "POST" and path == "/api/v1/admin/plans":
                return self._json(201, self._plan(user, req))
            if method == "POST" and (m := _match(r"/api/v1/admin/plans/(sha256:[0-9a-f]{64})/approve", path)):
                with st.lock:
                    plan = st.s["plans"].get(m[1])
                    if not plan:
                        raise Problem(404, "Unknown plan")
                    if user == plan["requestedBy"]:
                        raise Problem(403, "Approver must differ from requester (two-person rule)")
                    plan["approvedBy"] = user
                    st.audit(user, "plan.approved", {"planHash": m[1]})
                    st.save()
                return self._json(200, {"planHash": m[1], "approvedBy": user})
            if method == "POST" and (m := _match(r"/api/v1/admin/plans/(sha256:[0-9a-f]{64})/apply", path)):
                return self._json(200, self._apply(user, m[1], req))
            raise Problem(404, "Not found", path)

        def _plan(self, user: str, req: dict) -> dict:
            dev = req["deviceId"]
            with st.lock:
                if dev not in st.s["devices"] and not req.get("allowUnknownDevice"):
                    raise Problem(404, "Unknown device")
                cur = st.desired_doc(dev)
                new = [st.resolve_deployment(dev, it, base_url_holder["url"]) for it in req["deployments"]]
                ids = [d["metadata"]["id"] for d in new]
                if len(ids) != len(set(ids)):
                    raise Problem(422, "Duplicate deployment id")
                st.check_placement(dev, new)
                old = {d["metadata"]["id"]: d for d in cur["deployments"]}
                diff, needs_approval = [], False
                for d in new:
                    i = d["metadata"]["id"]
                    o = old.get(i)
                    if o is None:
                        diff.append({"op": "add", "id": i, "to": _rev(d)})
                    elif canonical_json(o) != canonical_json(d):
                        diff.append({"op": "update", "id": i, "from": _rev(o), "to": _rev(d)})
                    else:
                        continue
                    needs_approval |= d["x-flex-safetyClass"] != "none" or (o or {}).get("x-flex-safetyClass", "none") != "none"
                for i, o in old.items():
                    if i not in ids:
                        diff.append({"op": "remove", "id": i, "from": _rev(o)})
                        needs_approval |= o.get("x-flex-safetyClass", "none") != "none"
                body = {"deviceId": dev, "baseManifestVersion": cur["manifestVersion"], "deployments": new}
                h = sha256_digest(canonical_json(body))
                plan = {"planHash": h, **body, "diff": diff, "requiresApproval": needs_approval,
                        "requestedBy": user, "reason": req.get("reason", ""), "createdAt": iso(), "approvedBy": None}
                st.s["plans"][h] = plan
                st.audit(user, "plan.created", {"planHash": h, "deviceId": dev, "diff": diff})
                st.save()
            return {k: plan[k] for k in ("planHash", "deviceId", "baseManifestVersion", "diff", "requiresApproval")}

        def _verify_approvals(self, plan: dict, req: dict) -> dict:
            """Backend-side check of console-signed approvals (S4). Returns the audit evidence."""
            pol = st.approval_policy
            now = time.time()
            if req.get("preauthorized") == "rollback-known-good":
                if plan["requiresApproval"]:
                    raise Problem(403, "Pre-authorised rollback is not allowed for safety-class deployments")
                dev = plan["deviceId"]
                for e in plan["diff"]:
                    good = e["op"] == "update" and any(
                        s.get("deploymentId") == e["id"] and s.get("state") == "Installed"
                        and s.get("version") == e["to"]["revision"] and s.get("health") != "unhealthy"
                        for s in st.s["statusLog"] if s["deviceId"] == dev)
                    if not good:
                        raise Problem(403, "Not a known-good rollback",
                                      f"{e['id']}: target was never reported Installed and healthy on {dev}")
                return {"rule": "rollback-known-good"}
            rule = pol["safety"] if plan["requiresApproval"] else pol["standard"]
            requester = plan["requestedBy"].split("|obo:", 1)[-1]
            valid, notes = {}, []
            for a in req.get("approvals") or []:
                body = {k: v for k, v in a.items() if k != "sig"}
                if a.get("planHash") != plan["planHash"] or not any(
                        crypto.verify(k, canonical_json(body), a.get("sig", "")) for k in pol["keys"]):
                    notes.append(f"approval by {a.get('approver')} is not signed for this plan")
                    continue
                if a.get("decision") != "approve":
                    raise Problem(403, f"Plan was rejected by {a.get('approver')}")
                if now > float(a.get("expiresAtTs", 0)):
                    notes.append(f"approval by {a['approver']} expired")
                    continue
                if not pol.get("requesterMayApprove") and a["approver"] == requester:
                    notes.append(f"approval by {a['approver']} ignored (requester)")
                    continue
                if not set(a.get("roles", [])) & set(rule["roles"]):
                    notes.append(f"approval by {a['approver']} lacks a required role")
                    continue
                valid[a["approver"]] = a
            if len(valid) < rule["count"]:
                raise Problem(403, "Plan lacks verified approvals",
                              "; ".join([f"needs {rule['count']} distinct approver(s), has {len(valid)}", *notes]))
            for role in rule.get("requireRoles", []):
                if not any(role in a["roles"] for a in valid.values()):
                    raise Problem(403, "Plan lacks verified approvals", f"needs an approval by a {role}")
            return {"rule": "safety" if plan["requiresApproval"] else "standard",
                    "approvers": sorted(valid), "keyIds": sorted({crypto.key_id(k) for k in pol["keys"]})}

        def _apply(self, user: str, h: str, req: dict | None = None) -> dict:
            req = req or {}
            with st.lock:
                plan = st.s["plans"].get(h)
                if not plan:
                    raise Problem(404, "Unknown plan")
                if plan.get("appliedAt"):
                    raise Problem(409, "Plan already applied")
                dev = plan["deviceId"]
                cur = st.desired_doc(dev)
                if cur["manifestVersion"] != plan["baseManifestVersion"]:
                    raise Problem(409, "Stale plan", f"desired state moved to {cur['manifestVersion']}; re-plan")
                evidence = None
                if st.approval_policy and plan["diff"]:
                    evidence = self._verify_approvals(plan, req)
                elif plan["requiresApproval"] and not plan.get("approvedBy"):
                    raise Problem(403, "Plan needs approval by a second person")
                mv = cur["manifestVersion"] + 1
                st.s["desired"][dev] = {"manifestVersion": mv, "deployments": plan["deployments"]}
                plan["appliedAt"], plan["appliedBy"] = iso(), user
                st.s["publishedAt"].setdefault(dev, {})[str(mv)] = time.time()
                st.audit(user, "plan.applied", {"planHash": h, "deviceId": dev, "manifestVersion": mv,
                                                **({"verifiedApprovals": evidence} if evidence else {})})
                st.save()
            return {"planHash": h, "deviceId": dev, "manifestVersion": mv}

    return Handler


def _rev(d: dict) -> dict:
    p = d["spec"]["deploymentProfile"]["components"][0]["properties"]
    return {"applicationId": d["spec"]["applicationId"], "revision": p["revision"], "digest": p["digest"],
            "driver": d["x-flex-driver"], "safetyClass": d["x-flex-safetyClass"]}


class CloudServer:
    def __init__(self, data_dir: Path, port: int = 0, admin_token: str = "admin-secret", cloud_key=None,
                 host: str = "127.0.0.1", model_dirs: list[Path] | None = None, approval_policy: dict | None = None,
                 public_host: str | None = None):
        key = cloud_key or crypto.load_or_create(Path(data_dir) / "cloud.key")
        self.state = CloudState(Path(data_dir), admin_token, key, model_dirs, approval_policy)
        holder = {"url": ""}
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.state, holder))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        shown = public_host or host  # address devices use (e.g. the lab IP when binding 0.0.0.0)
        self.url = f"http://{shown}:{self.port}"
        holder["url"] = f"{shown}:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="flexCloud desired-state API stub")
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--admin-token", default="admin-secret")
    ap.add_argument("--admin-token-file", help="read the admin token from a file (preferred over --admin-token)")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="listen address (default 127.0.0.1). Non-local binding is for the lab only: plain HTTP, "
                         "so put TLS termination and a firewall in front of it")
    ap.add_argument("--public-host", help="host name/IP devices use to reach this stub (default: --bind)")
    ap.add_argument("--model", action="append", default=[],
                    help="flexModel root(s): derive safety classes from the model and refuse mismatching releases")
    ap.add_argument("--approval-policy", help="JSON {keys, standard, safety, requesterMayApprove}: verify console "
                                              "approvals at apply")
    a = ap.parse_args()
    token = Path(a.admin_token_file).read_text().strip() if a.admin_token_file else a.admin_token
    pol = json.loads(Path(a.approval_policy).read_text()) if a.approval_policy else None
    if a.bind not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {a.bind} over plain HTTP; use TLS + firewalling in the lab", flush=True)
    srv = CloudServer(Path(a.data), a.port, token, host=a.bind, model_dirs=[Path(m) for m in a.model],
                      approval_policy=pol, public_host=a.public_host)
    print(f"flexCloud stub on {srv.url}  cloud key {crypto.public_b64(srv.state.key)}", flush=True)
    srv.httpd.serve_forever()


if __name__ == "__main__":
    main()

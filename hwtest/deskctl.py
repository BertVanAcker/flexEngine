#!/usr/bin/env python3
"""Desk control for the flexEngineV2 runbooks: the operator side of a run against the flexCloud desired-state API
(the reference fake `flexengine2.testing.fakecloud` or the lab stub with the same northbound API).

    deskctl.py --cloud URL --admin-token-file F [--ca CA] [--catalog CATALOG.yaml] VERB ...

      token DEVICE [--out FILE]               one-time onboarding token (0600 file, or stdout)
      device DEVICE                           admin record: device, desired, status, statusLog (JSON)
      wait-device DEVICE [--capabilities]     until the device is onboarded (and sent its capabilities)
      publish NAME...                         build the catalog's releases and upload them
      deploy DEVICE [DEP=NAME ...] [--keep] [--remove DEP] [--expect 201] [--approver dave]
                                              plan -> second-person approve (when the plan needs it) -> apply
      converged DEVICE                        {converged, desired: {dep: version}, actual: {dep: state version}}
      bundle DEVICE --out FILE                export the device's signed offline bundle
      tamper NAME --layout DIR                flip one payload byte of a published release in an OCI layout (the
                                              stub's registry on disk; the signature stays valid, the digest not)

The catalog (YAML) names the releases a runbook uses, how to build them and how to deploy them:

    model: ../../examples/models/uc1       # flexModel workspace (descriptors, model-derived safety classes)
    releaseKey: keys/release.key           # Ed25519 release signing key (created when missing)
    keys: {rogue: keys/rogue.key}          # other signing keys a build may name
    work: work/                            # payload caches, published.json (what this tool uploaded)
    releases:
      app_v1:  {module: perception_guard, version: 1.0.0, driver: container,
                build: {kind: container, approvers: [alice, bob]}}
      app_bad: {module: perception_guard, version: 1.2.0, driver: container,
                build: {kind: container, failHealth: true, approvers: [alice, bob]}}
      servo_3: {module: servo_drive, version: 3.0.0, driver: mcu-module,
                parameters: {_interfaceHash: "@interfaceHash"}, build: {kind: flex-release, targets: [...]}}
      gw_app:  {module: uc3_greenhouse, version: 1.0.0, driver: applab}        # no build: published elsewhere

Build kinds: container | native (testing.release.make_release: stand-in apps, failHealth, sign, key, approvers,
runOptions, runtime, image), flex-release (flexmodel `flex release` targets), mcu-flxm {flxm, genMeta}, mcu-firmware
{image}, os-rauc {bundle | fake: {bootable, healthy}}. Relative paths are relative to the catalog file; `${NAME}`
is taken from the environment. Prints JSON; exit 0 = done (deploy: the plan status was the expected one).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import ssl
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))


class DeskError(Exception):
    pass


# ---- admin API client ---------------------------------------------------------------------------------------------
class Cloud:
    def __init__(self, url: str, token_file: str, ca: str | None = None, user: str = "carol", timeout: float = 60):
        self.url = url.rstrip("/")
        self.token = Path(os.path.expanduser(token_file)).read_text().strip()
        self.user, self.timeout = user, timeout
        self.ctx = None
        if self.url.startswith("https:"):
            from flexsteps import tls_context   # same trust rule as the engine: the pinned lab CA only

            self.ctx = tls_context(ca)

    def call(self, method: str, path: str, body=None, user: str | None = None, raw: bytes | None = None,
             ctype: str = "application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "X-Flex-User": user or self.user, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                status, out, rtype = r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            status, out, rtype = e.code, e.read(), e.headers.get("Content-Type", "")
        if "json" in rtype and out:
            return status, json.loads(out)
        return status, out


# ---- catalog --------------------------------------------------------------------------------------------------------
def _expand(v):
    if isinstance(v, str):
        def env(m):
            if m.group(1) not in os.environ:
                raise DeskError(f"catalog: ${{{m.group(1)}}} is not set in the environment")
            return os.environ[m.group(1)]
        return re.sub(r"\$\{(\w+)\}", env, v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


class Catalog:
    def __init__(self, path: str | None):
        self.path = Path(path).resolve() if path else None
        self.doc = _expand(yaml.safe_load(self.path.read_text())) if self.path else {}
        self.base = self.path.parent if self.path else Path.cwd()
        self.releases = self.doc.get("releases") or {}

    def p(self, v) -> Path:
        v = Path(os.path.expanduser(str(v)))
        return v if v.is_absolute() else (self.base / v).resolve()

    def get(self, name: str) -> dict:
        if name not in self.releases:
            raise DeskError(f"release {name!r} is not in the catalog {self.path} (known: {', '.join(self.releases)})")
        return self.releases[name]

    @property
    def work(self) -> Path:
        w = self.p(self.doc.get("work", "work"))
        w.mkdir(parents=True, exist_ok=True)
        return w

    def published(self) -> dict:
        f = self.work / "published.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def remember(self, name: str, info: dict) -> None:
        pub = self.published()
        pub[name] = info
        (self.work / "published.json").write_text(json.dumps(pub, indent=1, sort_keys=True))

    def key(self, name: str | None):
        from flexengine2.identity import crypto

        if name in (None, "", "release"):
            return crypto.load_or_create(self.p(self.doc.get("releaseKey", "keys/release.key")))
        keys = self.doc.get("keys") or {}
        if name not in keys:
            raise DeskError(f"signing key {name!r} not in the catalog's keys")
        return crypto.load_or_create(self.p(keys[name]))


# ---- releases -------------------------------------------------------------------------------------------------------
def _layout_tar(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(root)))
    return buf.getvalue()


def _upload(cloud: Cloud, root: Path) -> dict:
    s, b = cloud.call("POST", "/api/v1/admin/releases", raw=_layout_tar(root), ctype="application/x-tar",
                      user="release-tool")
    if s >= 300:
        raise DeskError(f"release upload: HTTP {s} {b}")
    return b


def build_release(cat: Catalog, cloud: Cloud, name: str) -> dict:
    from flexengine2.artefacts.oci import Layout

    ent = cat.get(name)
    b = dict(ent.get("build") or {})
    kind = b.get("kind")
    if not kind:
        raise DeskError(f"{name}: no build recipe (published by other tooling)")
    module, version = ent["module"], str(ent["version"])
    with tempfile.TemporaryDirectory(prefix="deskctl-") as tmp:
        lay = Layout(Path(tmp) / "oci")
        key = cat.key(b.get("key"))
        sign = b.get("sign", True)
        if kind in ("container", "native"):
            from flexengine2.testing.release import make_release, model_safety, module_hashes

            model = cat.p(b.get("model") or cat.doc["model"])
            appr = b.get("approvers")
            approval = {"approved": True, "approvers": appr, "role": "safety-engineer"} if appr else None
            info = make_release(lay, cat.work, key, model_dir=model, module=module, version=version, profile=kind,
                                fail_health=bool(b.get("failHealth")), approval=approval, sign=sign,
                                hashes=module_hashes(model), classes=model_safety(model),
                                runtime=b.get("runtime") or cat.doc.get("runtime", "docker"),
                                run_options=b.get("runOptions"), image=b.get("image"),
                                native_dir=cat.p(b["nativeDir"]) if b.get("nativeDir") else None)
        elif kind == "mcu-flxm":
            from flexengine2.testing.mcu_lab import gen_meta, make_module_release

            info = make_module_release(lay, key, cat.p(b["flxm"]), gen_meta(cat.p(b["genMeta"])), version, sign=sign)
        elif kind == "mcu-firmware":
            from flexengine2.testing import mcu_lab

            saved = mcu_lab.FIRMWARE_HEALTH
            if b.get("health"):  # e.g. a soak (minSeconds) long enough for a power cut by hand during TESTING
                mcu_lab.FIRMWARE_HEALTH = {**saved, **b["health"]}
            try:
                info = mcu_lab.make_firmware_release(lay, key, cat.p(b["image"]), version, sign=sign)
            finally:
                mcu_lab.FIRMWARE_HEALTH = saved
        elif kind == "os-rauc":
            from flexengine2.testing import rauc as trauc

            if b.get("bundle"):
                bundle = cat.p(b["bundle"])
            else:
                fk = b.get("fake") or {}
                bundle = trauc.build_fake_bundle(Path(tmp) / f"{module}-{version}.raucb", version,
                                                 bootable=fk.get("bootable", True), healthy=fk.get("healthy", True),
                                                 compatible=b.get("compatible", trauc.COMPATIBLE))
            info = trauc.make_rauc_release(lay, key, bundle, module=module, version=version,
                                           compatible=b.get("compatible", trauc.COMPATIBLE), health=b.get("health"),
                                           sign=sign)
        elif kind == "flex-release":
            return _flex_release(cat, cloud, name, ent, b)
        else:
            raise DeskError(f"{name}: unknown build kind {kind!r}")
        up = _upload(cloud, lay.root)
    out = {"name": name, "module": module, "version": version, "digest": info.get("digest"),
           "interfaceHash": info.get("interfaceHash"), "uploaded": up.get("releases") or up.get("imported")}
    cat.remember(name, out)
    return out


def _flex_release(cat: Catalog, cloud: Cloud, name: str, ent: dict, b: dict) -> dict:
    from flexmodel.release import Release

    fr = dict(cat.doc.get("flexRelease") or {})
    fr.update(b.get("config") or {})
    model = cat.p(b.get("model") or fr.pop("model", None) or cat.doc["model"])
    out = cat.work / "flex-release"
    cfg = {"deployment": fr.get("deployment"), "out": str(out),
           "release_key": str(cat.p(cat.doc.get("releaseKey", "keys/release.key"))), "targets": b["targets"]}
    if fr.get("mcu"):
        mcu = dict(fr["mcu"])
        if mcu.get("module_key"):
            mcu["module_key"] = str(cat.p(mcu["module_key"]))
        mcu["edk"] = {k: str(cat.p(v)) for k, v in (mcu.get("edk") or {}).items()}
        cfg["mcu"] = mcu
    rel = Release(model, cfg, out, approvers=list(b.get("approvers") or ["alice", "bob"]), force=bool(b.get("force")),
                  log=lambda m: None)
    res = rel.run(cloud=cloud.url, token=cloud.token)
    bad = [t for t in res["targets"] if t["status"] == "failed"]
    if bad:
        raise DeskError(f"{name}: flex release failed: {bad}")
    tgt = next((t for t in res["targets"] if t.get("module") == ent["module"]), res["targets"][0])
    out_info = {"name": name, "module": ent["module"], "version": str(ent["version"]), "digest": tgt.get("digest"),
                "interfaceHash": tgt.get("interfaceHash"), "uploaded": [t["name"] for t in res["targets"]]}
    cat.remember(name, out_info)
    return out_info


# ---- deployments ----------------------------------------------------------------------------------------------------
def iface_of(cat: Catalog, name: str) -> str:
    v = cat.published().get(name, {}).get("interfaceHash") or cat.get(name).get("interfaceHash")
    if not v:
        raise DeskError(f"{name}: interfaceHash unknown (publish it with this tool, or set interfaceHash)")
    return v


def resolve_refs(cat: Catalog, name: str, v):
    """`@interfaceHash` = the release's own FLEX01 hash, `@iface:<release>` inside a string = another release's
    (e.g. activation `plc:cx-bridge@@iface:cx_1_0`)."""
    if isinstance(v, str):
        if v == "@interfaceHash":
            return iface_of(cat, name)
        return re.sub(r"@iface:([\w.-]+)", lambda m: iface_of(cat, m.group(1)), v)
    if isinstance(v, list):
        return [resolve_refs(cat, name, x) for x in v]
    if isinstance(v, dict):
        return {k: resolve_refs(cat, name, x) for k, x in v.items()}
    return v


def deploy_item(cat: Catalog, dep: str, name: str) -> dict:
    ent = cat.get(name)
    item = {"id": dep, "module": ent["module"], "version": str(ent["version"]), "driver": ent.get("driver", "container")}
    if ent.get("parameters"):
        item["parameters"] = resolve_refs(cat, name, ent["parameters"])
    for k in ("activation", "safetyClass", "dependsOn", "name"):
        if k in ent:
            item[k] = resolve_refs(cat, name, ent[k])
    return item


def wait_device(cloud: Cloud, dev: str, timeout: float, capabilities: bool = False) -> dict:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        s, b = cloud.call("GET", f"/api/v1/admin/devices/{dev}")
        if s == 200 and b.get("device") and (not capabilities or b["device"].get("capabilities")):
            return b
        last = (s, b if s != 200 else "not onboarded yet")
        time.sleep(0.5)
    raise DeskError(f"{dev}: not onboarded{' with capabilities' if capabilities else ''} within {timeout} s ({last})")


def deploy(cat: Catalog, cloud: Cloud, dev: str, pairs: list[str], keep: bool, remove: list[str], expect: int,
           approver: str, wait: float) -> dict:
    wait_device(cloud, dev, wait)
    items = []
    if keep or remove:
        s, b = cloud.call("GET", f"/api/v1/admin/devices/{dev}")
        for d in (b.get("desired") or {}).get("deployments", []):
            props = d["spec"]["deploymentProfile"]["components"][0]["properties"]
            it = {"id": d["metadata"]["id"], "module": d["spec"]["applicationId"], "version": props["revision"],
                  "driver": d["x-flex-driver"]}
            if d["spec"].get("parameters"):
                it["parameters"] = d["spec"]["parameters"]
            if d.get("x-flex-activation"):
                it["activation"] = d["x-flex-activation"]
            if d.get("x-flex-dependsOn"):
                it["dependsOn"] = d["x-flex-dependsOn"]
            if d.get("x-flex-safetyClass", "none") != "none":
                it["safetyClass"] = d["x-flex-safetyClass"]
            if it["id"] not in remove:
                items.append(it)
    for pair in pairs:
        if "=" not in pair:
            raise DeskError(f"deploy: {pair!r} is not DEP=RELEASE")
        dep, name = pair.split("=", 1)
        items = [i for i in items if i["id"] != dep] + [deploy_item(cat, dep, name)]
    s, plan = cloud.call("POST", "/api/v1/admin/plans", {"deviceId": dev, "deployments": items})
    if s != 201:
        if s == expect:
            return {"status": s, "expected": True, "problem": plan}
        raise DeskError(f"plan: HTTP {s} {plan}")
    if expect != 201:
        raise DeskError(f"plan: HTTP 201, expected {expect}: {plan.get('diff')}")
    if plan.get("requiresApproval"):
        s, b = cloud.call("POST", f"/api/v1/admin/plans/{plan['planHash']}/approve", {}, user=approver)
        if s != 200:
            raise DeskError(f"approve as {approver}: HTTP {s} {b}")
    s, b = cloud.call("POST", f"/api/v1/admin/plans/{plan['planHash']}/apply", {})
    if s != 200:
        raise DeskError(f"apply: HTTP {s} {b}")
    return {"status": 201, "planHash": plan["planHash"],
            "diff": [f"{d['op']} {d['id']}" + (f" {d['to']['revision']}" if d.get("to") else "") for d in plan.get("diff") or []],
            "approved": bool(plan.get("requiresApproval")), "manifestVersion": b.get("manifestVersion"),
            "deployments": [f"{i['id']}={i['module']}:{i['version']}" for i in items], "appliedAt": time.time()}


def converged(cloud: Cloud, dev: str) -> dict:
    s, b = cloud.call("GET", f"/api/v1/admin/devices/{dev}")
    if s != 200:
        raise DeskError(f"device {dev}: HTTP {s} {b}")
    desired = {d["metadata"]["id"]: d["spec"]["deploymentProfile"]["components"][0]["properties"]["revision"]
               for d in (b.get("desired") or {}).get("deployments", [])}
    actual = {k: {"state": v.get("state"), "version": v.get("version"), "health": v.get("health")}
              for k, v in (b.get("status") or {}).items()}
    ok = all(actual.get(k, {}).get("state") == "Installed" and actual[k].get("version") == v
             and actual[k].get("health") != "unhealthy" for k, v in desired.items())
    ok = ok and all(v["state"] == "Removed" for k, v in actual.items() if k not in desired)
    return {"deviceId": dev, "converged": ok, "manifestVersion": (b.get("desired") or {}).get("manifestVersion"),
            "desired": desired, "actual": actual}


def tamper(cat: Catalog, name: str | None, layout_dir: str, ent: dict | None = None) -> dict:
    from flexengine2.artefacts.oci import Layout

    ent = ent or cat.get(name)
    name = name or f"{ent['module']}:{ent['version']}"
    lay = Layout(Path(layout_dir))
    desc = lay.resolve(f"flexnodes/{ent['module']}:{ent['version']}")
    if not desc:
        raise DeskError(f"{name}: flexnodes/{ent['module']}:{ent['version']} not in {layout_dir}")
    man = json.loads(lay.blob_path(desc["digest"]).read_bytes())
    for layer in man["layers"]:
        if "payload" in layer["mediaType"]:
            p = lay.blob_path(layer["digest"])
            data = bytearray(p.read_bytes())
            data[min(400, len(data) - 1)] ^= 0x01
            p.chmod(0o644)
            p.write_bytes(bytes(data))
            return {"name": name, "manifest": desc["digest"], "blob": layer["digest"], "tampered": True}
    raise DeskError(f"{name}: no payload layer")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cloud", default="", help="flexCloud (stub) base URL (every verb but tamper)")
    ap.add_argument("--admin-token-file", default="")
    ap.add_argument("--ca", default="", help="lab CA for an https:// cloud URL")
    ap.add_argument("--catalog", default="")
    ap.add_argument("--user", default="carol", help="requester recorded in the stub's audit log")
    ap.add_argument("--pretty", action="store_true", help="indented JSON")
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("token")
    p.add_argument("device")
    p.add_argument("--out")
    p = sub.add_parser("device")
    p.add_argument("device")
    p = sub.add_parser("wait-device")
    p.add_argument("device")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--capabilities", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("deploy")
    p.add_argument("device")
    p.add_argument("pairs", nargs="*", metavar="DEP=RELEASE")
    p.add_argument("--keep", action="store_true", help="keep the other deployments of the current desired state")
    p.add_argument("--remove", action="append", default=[], metavar="DEP")
    p.add_argument("--expect", type=int, default=201, help="expected plan status (e.g. 422 for a refused plan)")
    p.add_argument("--approver", default="dave", help="second person for a plan that needs approval")
    p.add_argument("--wait", type=float, default=60, help="seconds to wait for the device to be onboarded")
    p = sub.add_parser("converged")
    p.add_argument("device")
    p = sub.add_parser("bundle")
    p.add_argument("device")
    p.add_argument("--out", required=True)
    p = sub.add_parser("tamper")
    p.add_argument("name", nargs="?", help="catalog release (or --module/--version)")
    p.add_argument("--module")
    p.add_argument("--version")
    p.add_argument("--layout", required=True)
    a = ap.parse_args(argv)
    try:
        cat = Catalog(a.catalog or None)
        if a.verb == "tamper":
            ent = {"module": a.module, "version": a.version} if a.module else None
            print(json.dumps(tamper(cat, a.name, a.layout, ent)))
            return 0
        if not (a.cloud and a.admin_token_file):
            raise DeskError("--cloud and --admin-token-file are required")
        cloud = Cloud(a.cloud, a.admin_token_file, a.ca or None, a.user)
        if a.verb == "token":
            s, b = cloud.call("POST", "/api/v1/admin/onboarding-tokens", {"deviceId": a.device})
            if s != 201:
                raise DeskError(f"token: HTTP {s} {b}")
            if a.out:
                out = Path(os.path.expanduser(a.out))
                out.parent.mkdir(parents=True, exist_ok=True)
                out.touch(mode=0o600)
                out.chmod(0o600)
                out.write_text(b["token"] + "\n")
                doc = {"deviceId": a.device, "tokenFile": str(out)}
            else:
                print(b["token"])
                return 0
        elif a.verb == "device":
            s, doc = cloud.call("GET", f"/api/v1/admin/devices/{a.device}")
            if s != 200:
                raise DeskError(f"device: HTTP {s} {doc}")
        elif a.verb == "wait-device":
            b = wait_device(cloud, a.device, a.timeout, a.capabilities)
            doc = {"deviceId": a.device, "onboarded": True,
                   "capabilities": bool((b.get("device") or {}).get("capabilities"))}
        elif a.verb == "publish":
            doc = {"published": [build_release(cat, cloud, n) for n in a.names]}
        elif a.verb == "deploy":
            doc = deploy(cat, cloud, a.device, a.pairs, a.keep, a.remove, a.expect, a.approver, a.wait)
        elif a.verb == "converged":
            doc = converged(cloud, a.device)
        elif a.verb == "bundle":
            s, data = cloud.call("GET", f"/api/v1/admin/devices/{a.device}/bundle")
            if s != 200:
                raise DeskError(f"bundle: HTTP {s} {data}")
            out = Path(os.path.expanduser(a.out))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            doc = {"deviceId": a.device, "bundle": str(out), "bytes": len(data)}
    except (DeskError, OSError, KeyError, ValueError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        print(f"deskctl: {e}", file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=1 if a.pretty else None, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

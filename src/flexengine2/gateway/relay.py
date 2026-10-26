"""IG-07 (D2 decision: gateway relay; zenoh-pico on the MCU is D3): MCU local bus <-> Zenoh / ROS 2.

The flexEngine-MCU has no network stack in D1/D2. Its SMP `flexmod status` reports, per module socket, the *last*
message the module published (topic id = first 4 bytes of the type's FLEX01 hash, payload XCDR2, <= 64 B) and a
publication counter. The relay, running in the gateway's engine process and sharing its SMP link:

  MCU -> Zenoh   polls `status` at <= max_rate_hz (default 10 Hz, plan: indicative), and for every module whose
                 publication counter advanced, decodes the last message with the model's types, re-encodes it as XCDR1
                 and publishes it on the rmw_zenoh key of that port (flexmodel.ros2.node_mapping -> `ros2 topic echo
                 /gripper_station/state` works on the Mac). Only the last message per poll is seen: a 50 Hz topic is
                 sampled at the relay rate (that is the D2 limit the plan accepts).
  Zenoh -> MCU   subscribes to the rmw_zenoh key of every input port of the MCU modules and serves the rpc server
                 ports as ROS services; a sample / request is transcoded to XCDR2 and injected onto the MCU local bus
                 with the new SMP command `flexmod inject` (group 64, id 7). An rpc request is answered with the relay's
                 receipt (GripAck-shaped: accepted = delivered to >= 1 module, reason "relayed"); the module's own
                 response goes to the local bus, where only `status` can see it (P3 has no rpc into modules).

Also usable without ROS: `python -m flexedge relay-inject --smp HOST:PORT --instance gripper --port command
--json '{"seq": 1, "mode": "close", "max_force_n": 30}'` injects one message (scenario driver, tests).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import queue
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from flexmodel.codegen import generate as gen_types
from flexmodel.codegen.nodes.mcu_module import topic_id
from flexmodel.ihash import all_hashes
from flexmodel.ros2 import node_mapping
from flexmodel.runtime import cdr
from flexmodel.workspace import Workspace


@dataclass
class Port:
    instance: str
    module: str
    port: str
    qname: str                 # flexModel type carried on the bus (request type for rpc)
    topic: int
    key: str | None = None     # rmw_zenoh key expression
    direction: str = "out"
    pattern: str = "pubsub"
    response: str | None = None
    gen: int = 0               # model generation (the relay follows model changes, see McuRelay.follow)


@dataclass
class RelayStats:
    polls: int = 0
    published: int = 0
    injected: int = 0
    inject_failed: int = 0
    decode_errors: int = 0
    last_poll_ms: float = 0.0
    per_port: dict = field(default_factory=dict)


class Codec:
    """Model types -> generated Python CDR classes (py-cdr, generated once into the relay's work dir)."""

    _n = 0

    def __init__(self, model: Path, work: Path, fresh: bool = False):
        out = Path(work) / "pycdr"
        if fresh or not (out / "python").exists():
            import shutil

            shutil.rmtree(out, ignore_errors=True)
            gen_types(Path(model), out, ["py-cdr"])
        self.dir = out / "python"
        Codec._n += 1
        self.tag = Codec._n          # one module namespace per codec: two model generations can coexist
        self._mods: dict[str, object] = {}

    def cls(self, qname: str):
        pkg, name = qname.split("/", 1)
        if pkg not in self._mods:
            spec = importlib.util.spec_from_file_location(f"_relay{self.tag}_{pkg}_types", self.dir / f"{pkg}_types.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            self._mods[pkg] = mod
        return getattr(self._mods[pkg], name)

    def transcode(self, qname: str, data: bytes, version: int) -> bytes:
        return bytes(cdr.encode(cdr.decode(self.cls(qname), bytes(data)), version))

    def from_json(self, qname: str, obj: dict) -> bytes:
        c = self.cls(qname)
        kw = {}
        for k, v in obj.items():
            ann = c.__dataclass_fields__[k].type if hasattr(c, "__dataclass_fields__") else None
            enum_cls = getattr(self._mods[qname.split("/")[0]], str(ann), None) if isinstance(ann, str) else None
            if enum_cls is not None and hasattr(enum_cls, "__members__") and isinstance(v, str):
                v = enum_cls[v]
            kw[k] = v
        return bytes(cdr.encode(c(**kw), cdr.XCDR2))


def mcu_ports(ws: Workspace, deployment: str, device: str) -> list[Port]:
    """Ports of the instances allocated to `device` (the MCU), with topic ids and rmw_zenoh keys."""
    dep = ws.deployments[deployment].doc.raw
    sysdoc = ws.systems[dep["spec"]["system"]].doc.raw
    insts = {i["name"]: i for i in sysdoc["spec"]["instances"]}
    th = all_hashes(ws)["types"]
    out = []
    for a in dep["spec"]["allocations"]:
        if a["device"] != device:
            continue
        inst = insts[a["instance"]]
        module = inst["module"].split("@")[0]
        mapping = node_mapping(ws, module, a["instance"]).to_dict()
        keys = {e["port"]: e["key_expr"] for e in mapping["endpoints"]}
        loc = ws.find_module(inst["module"]) or ws.find_module(module)
        for p in loc.obj.spec.ports or []:
            pattern = str(getattr(p.pattern, "value", p.pattern) or "pubsub")
            direction = str(getattr(p.direction, "value", p.direction))
            q = ws.resolve_quiet(p.type if pattern == "pubsub" else p.request)
            resp = ws.resolve_quiet(p.response) if pattern == "rpc" else None
            out.append(Port(a["instance"], module, p.name, q, topic_id(th[q]), keys.get(p.name), direction, pattern,
                            resp))
            if resp and direction == "in":  # rpc server response on the bus (mcu-module rpc-over-localbus)
                out.append(Port(a["instance"], module, p.name + ".response", resp, topic_id(th[resp]), None, "out",
                                "rpc-response"))
    return out


class McuRelay:
    def __init__(self, links, model: Path, deployment: str, device: str, work: Path, *, max_rate_hz: float = 10.0,
                 connect: str | None = "tcp/127.0.0.1:7447", listen: str | None = None, mode: str = "peer",
                 zenoh_enabled: bool = True, follow_model: bool = True, log=print):
        self.links, self.log = links, log
        self.model, self.deployment, self.device, self.work = Path(model), deployment, device, Path(work)
        self.period = 1.0 / min(float(max_rate_hz), 10.0) if max_rate_hz else 0.1
        self.next_poll = 0.0
        self.last_pubs: dict = {}
        self.inbox: queue.Queue = queue.Queue()
        self.stats = RelayStats()
        self.transports: dict[tuple[int, str], object] = {}
        self.session = None
        self.zenoh_enabled = zenoh_enabled
        self.follow_model = follow_model
        self.gens: list[dict] = []
        self.by_topic: dict[int, Port] = {}
        self.next_follow = time.time() + 2.0
        if zenoh_enabled:
            self._open_zenoh(connect, listen, mode)
        self._new_generation()

    @property
    def ws(self) -> Workspace:
        return self.gens[-1]["ws"]

    @property
    def ports(self) -> list[Port]:
        return self.gens[-1]["ports"]

    @property
    def codec(self) -> "Codec":
        return self.gens[-1]["codec"]

    def _fingerprint(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self.model.rglob("*.yaml")):
            try:
                h.update(str(p.relative_to(self.model)).encode() + b"\0" + p.read_bytes())
            except OSError:
                pass
        return h.hexdigest()

    def _new_generation(self) -> None:
        """Load the model (again) and map its MCU ports. Older generations stay mapped for the topics the new one
        does not know, so a module that still runs its previous release (interface hash = old topic id) keeps being
        relayed on its old keys until the new release is installed; then the new keys take over."""
        ws = Workspace.load(self.model)
        if ws.sink.errors:
            if not self.gens:
                raise RuntimeError("relay: model has errors")
            self.log("relay: model has errors; keeping the previous mapping")
            return
        g = len(self.gens)
        ports = mcu_ports(ws, self.deployment, self.device)
        for p in ports:
            p.gen = g
        codec = Codec(self.model, self.work / f"gen{g}", fresh=g > 0)
        gen = {"ws": ws, "ports": ports, "codec": codec, "fingerprint": self._fingerprint(), "at": time.time()}
        self.gens.append(gen)
        for p in ports:
            if p.direction == "out":
                self.by_topic[p.topic] = p
        if self.session is not None:
            self._declare(gen, g)
        if g:
            self.log(f"relay: model changed -> generation {g} ({len(ports)} ports)")

    def follow(self) -> None:
        if not self.follow_model or time.time() < self.next_follow:
            return
        self.next_follow = time.time() + 2.0
        if self._fingerprint() != self.gens[-1]["fingerprint"]:
            self._new_generation()

    # ---- Zenoh side ---------------------------------------------------------------------------------
    def _open_zenoh(self, connect, listen, mode):
        import zenoh

        conf: dict = {"mode": mode, "scouting": {"multicast": {"enabled": False}}}
        if mode != "client":
            conf["listen"] = {"endpoints": [listen or "tcp/127.0.0.1:0"]}
        if connect:
            conf["connect"] = {"endpoints": [connect]}
        self.session = zenoh.open(zenoh.Config.from_json5(json.dumps(conf)))

    def _declare(self, gen: dict, g: int) -> None:
        from flexmodel.runtime.ros2_zenoh import RmwZenohTransport

        dep_insts = sorted({(p.instance, p.module) for p in gen["ports"]})
        for nid, (inst, module) in enumerate(dep_insts):
            mapping = node_mapping(gen["ws"], module, inst).to_dict()
            t = RmwZenohTransport(mapping, session=self.session, node_id=100 + 10 * g + nid)
            self.transports[(g, inst)] = t
            for p in gen["ports"]:
                if p.instance != inst or p.key is None or p.direction != "in":
                    continue
                if any(t2.by_key.get(p.key) for (g2, _), t2 in self.transports.items() if g2 != g):
                    continue  # same key served by an older generation (the input type did not change)
                if p.pattern == "pubsub":
                    t.subscribe(p.key, lambda data, enc, p=p: self.inbox.put((p, bytes(data), None)))
                else:
                    t.serve(p.key, lambda data, enc, p=p: self._serve(p, bytes(data)))

    def _serve(self, p: Port, data: bytes):
        """ROS service request (zenoh thread): queue it for the main loop and wait for the inject receipt."""
        done: queue.Queue = queue.Queue(maxsize=1)
        self.inbox.put((p, data, done))
        try:
            delivered = done.get(timeout=2.0)
        except queue.Empty:
            delivered = 0
        codec = self.gens[p.gen]["codec"]
        req = cdr.decode(codec.cls(p.qname), data)
        ack_cls = codec.cls(p.response)
        fields = getattr(ack_cls, "__dataclass_fields__", {})
        kw = {}
        if "seq" in fields:
            kw["seq"] = getattr(req, "seq", 0)
        if "accepted" in fields:
            kw["accepted"] = delivered > 0
        if "reason" in fields:
            kw["reason"] = "relayed" if delivered > 0 else "not delivered"
        return bytes(cdr.encode(ack_cls(**kw), cdr.XCDR1)), f"application/cdr;{p.response}"

    # ---- main-loop work (same thread as the engines: the SMP link is not shared across threads) -----------
    def due(self) -> bool:
        return time.time() >= self.next_poll or not self.inbox.empty()

    def tick(self) -> None:
        self.follow()
        while not self.inbox.empty():
            p, data, done = self.inbox.get_nowait()
            n = self.inject(p, data)
            if done is not None:
                done.put(n)
        if time.time() < self.next_poll:
            return
        self.next_poll = time.time() + self.period
        self.poll()

    def inject(self, p: Port, data: bytes) -> int:
        """Transcode to XCDR2 (what the MCU modules decode) and inject onto the local bus. Returns modules reached."""
        try:
            payload = self.gens[p.gen]["codec"].transcode(p.qname, data, cdr.XCDR2)
        except Exception as e:  # noqa: BLE001 - bad sample from the network: count and drop
            self.stats.decode_errors += 1
            self.log(f"relay: undecodable {p.qname} for {p.instance}.{p.port}: {e}")
            return 0
        return self.inject_raw(p.topic, payload, f"{p.instance}.{p.port}")

    def inject_raw(self, topic: int, payload: bytes, what: str = "") -> int:
        try:
            r = self.links.call(lambda l: l.flexmod("inject", topic=topic, data=payload, timeout=5), retries=0)
        except Exception as e:  # noqa: BLE001 - DriverError: MCU unreachable or old base image
            self.stats.inject_failed += 1
            self.log(f"relay: inject {what} failed: {e}")
            return 0
        if r.get("rc", 0) != 0:
            self.stats.inject_failed += 1
            self.log(f"relay: inject {what} refused: {r.get('reason')}")
            return 0
        self.stats.injected += 1
        return int(r.get("delivered", 0))

    def poll(self) -> list[dict]:
        t0 = time.time()
        try:
            st = self.links.call(lambda l: l.flexmod("status"))
        except Exception as e:  # noqa: BLE001 - MCU unreachable: skip this poll
            self.log(f"relay: status failed: {e}")
            return []
        self.stats.polls += 1
        out = []
        for s in st.get("sockets", []):
            topic, pubs = s.get("last_topic"), s.get("pubs", 0)
            if not s.get("running") or topic not in self.by_topic:
                continue
            key = (s["sock"], topic)
            if self.last_pubs.get(key) == pubs:
                continue
            self.last_pubs[key] = pubs
            p = self.by_topic[topic]
            try:
                x1 = self.gens[p.gen]["codec"].transcode(p.qname, bytes(s["last_msg"]), cdr.XCDR1)
            except Exception:  # noqa: BLE001 - truncated (> 64 B) or foreign message
                self.stats.decode_errors += 1
                continue
            msg = {"instance": p.instance, "port": p.port, "type": p.qname, "key": p.key, "xcdr1": x1, "pubs": pubs}
            out.append(msg)
            self.stats.per_port[f"{p.instance}.{p.port}"] = pubs
            t = self.transports.get((p.gen, p.instance))
            if t is not None and p.key:
                t.publish(p.key, x1, f"application/cdr;{p.qname}")
                self.stats.published += 1
        self.stats.last_poll_ms = round((time.time() - t0) * 1000, 1)
        return out

    def close(self):
        for t in self.transports.values():
            for e in t._keep + list(t._pubs.values()) + t._tokens:
                try:
                    e.undeclare()
                except Exception:  # noqa: BLE001
                    pass
        if self.session is not None:
            self.session.close()
            self.session = None


def find_port(ports: list[Port], instance: str, port: str) -> Port:
    for p in ports:
        if p.instance == instance and p.port == port and p.direction == "in":
            return p
    raise KeyError(f"{instance}.{port} is not an input port of an MCU module")

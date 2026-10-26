"""flexEngine driver `arduino-ota` (EV-38): firmware updates of flexNode-arduino boards (Portenta H7, Portenta Machine
Control, Opta) over their Arduino_Portenta_OTA path, health = the node's flexCloud status / version keys.

Protocol: api/cp6-children.md §5 and flexengine/proposals/flexnode-arduino-remote-ota/README.md. The remote command
is a PROPOSED upstream change of flexNode-arduino (held by project rule S7); a board without it never accepts the
command (apply fails with "did not accept the OTA command").

Payload: one `.ota` image (Arduino_Portenta_OTA format: [length u32][crc32 u32][magic u32][8 bytes][LZSS body], as
written by flexNode-arduino's tools/hwtest/make_ota.py) as the layer `application/vnd.flex.payload.arduino-ota.v1+ota`,
annotated `dev.flex.profile: arduino-ota`, `dev.flex.board: <FQBN>` and `dev.flex.firmwareVersion: <the version the
sketch reports with set_application_version()>`.

Security: the Portenta bootloader and Arduino_Portenta_OTA check no signature (only the header CRC and the board
magic). The engine verifies the release (Ed25519 release signature over the manifest, so over the image's sha256)
before anything reaches the board; the driver then serves the image on the LAN from its own small HTTP(S) server
under a one-time, unguessable path and sends the node an OTA_UPDATE command that is HMAC-SHA256-signed with the
node's flexCloud secret key (flexCommunicator's command format) and carries the image's sha256 and size, which the
patched library checks before installing. What the engine verified is therefore what the board installs; somebody
holding the node key (or USB access) can still install anything.

SPI mapping
  preflight  the deployment's board (parameter `board`, or the only configured board) is configured; the layer's
             dev.flex.board equals its FQBN; dev.flex.firmwareVersion is present; the .ota header is well formed
             (length, CRC-32, board magic of the FQBN); the board's command key file is readable
  platform   {"dev.flex.board": <FQBN>} when all configured boards share one FQBN (multi-board releases), else None
  apply      the board already runs the installed digest (state file + its version key) -> nothing to do. Else:
             serve the image under /ota/<token>.ota, publish the signed OTA_UPDATE on flexcloud/<cid>/<id>/cmd
             (QoS 1) and follow the node's `ota` key: accepted -> downloading -> verifying -> installing ->
             restarting -> booted (the new firmware reported its version). rejected / failed / booted into another
             version / no answer -> DriverError (fail fast: the engine skips the gate and rolls back)
  status     the board's version key against the firmware this engine installed there; running = status key in
             healthyStatus and a live heartbeat key; another firmware (flashed over USB) = not present (drift);
             broker/Redis down or the board offline = present, not running (no false drift)
  health     running (as status) and no update in flight
  failed     the node reported the in-flight update failed/rejected, or the installed firmware reports a status in
             failedStatus (default ERROR)
  commit     none: the Portenta bootloader has no test/confirm step. A firmware that boots but is unhealthy cannot be
             reverted by the board; the engine's rollback re-serves the previous release's .ota (apply(bad, previous))
  remove     stop managing the board (the firmware keeps running: an MCU has nothing to uninstall)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import ssl
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flexengine2.drivers.arduino_wire import MqttClient, RedisClient, WireError
from flexengine2.drivers.base import Driver, DriverError, Observed, Workload
from flexengine2.util import atomic_write_json, read_json

MT_PAYLOAD_ARDUINO_OTA = "application/vnd.flex.payload.arduino-ota.v1+ota"
ANN_BOARD = "dev.flex.board"
ANN_FIRMWARE = "dev.flex.firmwareVersion"
CMD_OTA = "OTA_UPDATE"
# board magic of the .ota header (USB VID/PID, as flexNode-arduino tools/hwtest/make_ota.py and Arduino's bin2ota.py)
OTA_MAGIC = {
    "arduino:mbed_portenta:envie_m7": 0x2341025B,
    "arduino:mbed_opta:opta": 0x23410064,
    "arduino:mbed_giga:giga": 0x23410266,
    "arduino:mbed_nicla:nicla_vision": 0x2341025F,
}
OTA_STATES = ("accepted", "downloading", "verifying", "installing", "restarting", "booted", "failed", "rejected")
FINAL = ("booted", "failed", "rejected")


# ---- protocol (shared with the board simulator, flexengine2/testing/arduino_sim.py) -------------------------------
def keys(connector: str, app: str) -> dict[str, str]:
    """flexCloud Redis keys of one node (`status`, `version`, `logs` exist in flexNode-arduino 0.1.0; `ota` and
    `heartbeat` are added by the proposed patch)."""
    p = f"{connector}:applications:{app}"
    return {"status": f"{p}:status", "version": f"{p}:version", "logs": f"{p}:logs", "ota": f"{p}:ota",
            "heartbeat": f"{p}:heartbeat"}


def cmd_topic(connector: str, app: str) -> str:
    return f"flexcloud/{connector}/{app}/cmd"


def signing_string(cmd: dict) -> str:
    """flexCommunicator's compute_signature() input (flcpy/security/security.py)."""
    return (cmd["id"] + cmd["cmd"] + str(cmd["priority"]) + str(cmd["expiry"]) +
            json.dumps(cmd["params"], sort_keys=True) + str(cmd["timestamp"]) + cmd["nonce"])


def sign(key: bytes, cmd: dict) -> str:
    return hmac.new(key, signing_string(cmd).encode(), hashlib.sha256).hexdigest()


def verify(key: bytes, cmd: dict) -> bool:
    try:
        return hmac.compare_digest(sign(key, cmd), str(cmd["signature"]))
    except (KeyError, TypeError, AttributeError):
        return False


def compose_command(key: bytes, params: dict, *, expiry_seconds: int = 600, priority: int = 2,
                    now: int | None = None) -> dict:
    """A signed OTA_UPDATE command in flexCommunicator's compose_command() shape."""
    t = int(time.time() if now is None else now)
    cmd = {"id": str(uuid.uuid4()), "cmd": CMD_OTA, "priority": priority, "expiry": t + int(expiry_seconds),
           "params": params, "timestamp": t, "nonce": uuid.uuid4().hex}
    cmd["signature"] = sign(key, cmd)
    return cmd


def read_key_file(path: str | os.PathLike) -> bytes:
    """The node's flexCloud secret key: hex text, or the sketch's C array (`0xae, 0xc1, ...`)."""
    text = Path(path).read_text()
    if "0x" in text.lower():
        import re
        key = bytes(int(h, 16) for h in re.findall(r"0[xX]([0-9a-fA-F]{1,2})", text))
    else:
        key = bytes.fromhex("".join(text.split()))
    if len(key) < 16:
        raise ValueError(f"command key in {path} is {len(key)} bytes (at least 16 expected)")
    return key


def ota_header(path: str | os.PathLike, fqbn: str | None = None) -> dict:
    """Checks the Arduino_Portenta_OTA header; raises ValueError(reason)."""
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise ValueError(f"not an OTA image: {len(data)} bytes")
    length, crc = struct.unpack("<II", data[:8])
    if length != len(data) - 8:
        raise ValueError(f"not an OTA image: header length {length}, file has {len(data) - 8} bytes after it")
    if zlib.crc32(data[8:]) != crc:
        raise ValueError("OTA image CRC-32 mismatch (corrupt image)")
    magic = struct.unpack("<I", data[8:12])[0]
    if fqbn is not None:
        want = OTA_MAGIC.get(fqbn)
        if want is None:
            raise ValueError(f"no OTA support known for board {fqbn}")
        if magic != want:
            board = next((f for f, m in OTA_MAGIC.items() if m == magic), f"magic 0x{magic:08x}")
            raise ValueError(f"OTA image is for {board}, the board is {fqbn}")
    return {"length": length, "crc32": crc, "magic": magic, "size": len(data)}


def file_digest(path: str | os.PathLike) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# ---- one-time LAN server ------------------------------------------------------------------------------------------
class OtaServer:
    """Serves each image under /ota/<token>.ota: unguessable (192-bit token), one complete download per token,
    expiring, optionally only to the board's own address."""

    def __init__(self, host: str, port: int, public_url: str | None, cert: str | None = None, key: str | None = None):
        self.tokens: dict[str, dict] = {}
        self.served = 0
        self.lock = threading.Lock()
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"     # Content-Length + close, what Arduino_Portenta_OTA's download expects

            def log_message(self, *a):  # noqa: D401 - quiet
                pass

            def do_GET(self):
                srv._get(self)

        self.httpd = ThreadingHTTPServer((host, int(port)), Handler)
        self.httpd.daemon_threads = True
        self.scheme = "http"
        if cert and key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        base = public_url or f"{self.scheme}://{host}:{self.port}"
        self.public = base.replace("{port}", str(self.port)).rstrip("/")
        threading.Thread(target=self.httpd.serve_forever, name="arduino-ota-serve", daemon=True).start()

    def offer(self, path: str, size: int, ttl: float, address: str | None = None) -> tuple[str, str]:
        tok = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            for t in [t for t, v in self.tokens.items() if v["expires"] < now]:
                self.tokens.pop(t, None)
            self.tokens[tok] = {"path": path, "size": size, "expires": now + ttl, "address": address}
        return tok, f"{self.public}/ota/{tok}.ota"

    def revoke(self, tok: str | None) -> None:
        with self.lock:
            self.tokens.pop(tok or "", None)

    def _get(self, h: BaseHTTPRequestHandler) -> None:
        name = h.path.split("?", 1)[0]
        tok = name[len("/ota/"):-len(".ota")] if name.startswith("/ota/") and name.endswith(".ota") else ""
        with self.lock:
            ent = self.tokens.get(tok)
            ok = ent is not None and ent["expires"] >= time.time() and not ent.get("busy") and \
                (not ent["address"] or ent["address"] == h.client_address[0])
            if ok:
                ent["busy"] = True
        if not ok:
            h.send_error(404)
            return
        try:
            h.send_response(200)
            h.send_header("Content-Type", "application/octet-stream")
            h.send_header("Content-Length", str(ent["size"]))
            h.end_headers()
            with open(ent["path"], "rb") as f:
                for chunk in iter(lambda: f.read(1 << 14), b""):
                    h.wfile.write(chunk)
            h.wfile.flush()
        except OSError:
            with self.lock:
                ent["busy"] = False         # broken transfer: the node may retry within the TTL
            return
        with self.lock:
            self.tokens.pop(tok, None)      # one complete download per token
            self.served += 1

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---- driver -------------------------------------------------------------------------------------------------------
class ArduinoOtaDriver(Driver):
    profile = "arduino-ota"
    name = "portenta-ota"
    exclusive = True            # one firmware per board; maxDeployments = number of configured boards

    def __init__(self, device_id: str, data_dir: Path, cfg: dict | None = None):
        cfg = cfg or {}
        self.device = device_id
        self.data_dir = Path(data_dir)
        self.boards: dict[str, dict] = dict(cfg.get("boards") or {})
        self.max_deployments = max(1, len(self.boards))
        self.mqtt_cfg = dict(cfg.get("mqtt") or {})
        self.redis_cfg = dict(cfg.get("redis") or {})
        self.serve_cfg = dict(cfg.get("serve") or {})
        self.accept_timeout = float(cfg.get("acceptTimeoutSeconds", 20))
        self.update_timeout = float(cfg.get("updateTimeoutSeconds", 300))
        self.poll = float(cfg.get("pollSeconds", 0.5))
        self.wire_timeout = float(cfg.get("timeout", 2.0))
        self.healthy_status = list(cfg.get("healthyStatus") or ["RUNNING"])
        self.failed_status = list(cfg.get("failedStatus") or ["ERROR"])
        self.require_heartbeat = bool(cfg.get("requireHeartbeat", True))
        self.root = self.data_dir / "arduino-ota"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self._server: OtaServer | None = None
        self._redis: dict[str, RedisClient] = {}
        self._digests: dict = {}

    # ---- config helpers ----------------------------------------------------------------------------------------
    def _conn(self, name: str, kind: str) -> dict:
        base = self.mqtt_cfg if kind == "mqtt" else self.redis_cfg
        return {**base, **(self.boards[name].get(kind) or {})}

    @staticmethod
    def _secret(c: dict) -> str | None:
        p = c.get("passwordFile")
        if not p:
            return None
        try:
            return Path(p).read_text().strip() or None
        except OSError as e:
            raise DriverError(f"arduino-ota: cannot read {p} ({e.strerror})") from e

    def _key(self, name: str) -> bytes:
        p = self.boards[name].get("commandKeyFile")
        if not p:
            raise DriverError(f"arduino-ota: board {name} has no commandKeyFile")
        try:
            return read_key_file(p)
        except (OSError, ValueError) as e:
            raise DriverError(f"arduino-ota: board {name}: unusable command key file {p} ({e})") from e

    def _keys(self, name: str) -> dict:
        b = self.boards[name]
        return keys(b["connectorId"], b["applicationId"])

    def _rds(self, name: str) -> RedisClient:
        r = self._redis.get(name)
        if r is None:
            c = self._conn(name, "redis")
            r = RedisClient(c.get("host", "127.0.0.1"), int(c.get("port", 6379)), self._secret(c), c.get("db", 0),
                            timeout=self.wire_timeout)
            self._redis[name] = r
        return r

    def _board_name(self, params: dict | None) -> str | None:
        want = (params or {}).get("board")
        if want:
            return str(want) if str(want) in self.boards else None
        return next(iter(self.boards)) if len(self.boards) == 1 else None

    def _board_for(self, wl: Workload) -> str:
        name = self._board_name(wl.parameters)
        if name is None:
            raise DriverError(f"arduino-ota: no board {(wl.parameters or {}).get('board') or '(parameter board)'} "
                              f"on {self.device} (configured: {sorted(self.boards)})")
        return name

    @staticmethod
    def _fw(wl: Workload) -> str:
        return (wl.payload_annotations or {}).get(ANN_FIRMWARE) or wl.version

    # ---- state ---------------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        s = read_json(self.state_path, None) or {}
        s.setdefault("boards", {})
        return s

    def _save(self, s: dict) -> None:
        atomic_write_json(self.state_path, s)

    @staticmethod
    def _find(s: dict, dep_id: str) -> tuple[str | None, dict | None]:
        for name, b in s["boards"].items():
            if b.get("dep") == dep_id:
                return name, b
        return None, None

    # ---- observation ---------------------------------------------------------------------------------------------
    def _observe(self, name: str) -> dict:
        """The node's flexCloud keys; raises WireError when Redis is unreachable."""
        k, r = self._keys(name), self._rds(name)
        out = {"status": r.get(k["status"]), "version": r.get(k["version"])}
        for f in ("ota", "heartbeat"):
            raw = r.get(k[f])
            try:
                out[f] = json.loads(raw) if raw else None
            except ValueError:
                out[f] = None
            if not isinstance(out[f], dict):
                out[f] = None
        return out

    def _alive(self, obs: dict) -> bool:
        return obs.get("heartbeat") is not None or not self.require_heartbeat

    def _running(self, obs: dict, fw: str) -> bool:
        return obs.get("version") == fw and obs.get("status") in self.healthy_status and self._alive(obs)

    # ---- serving / command ---------------------------------------------------------------------------------------
    def server(self) -> OtaServer:
        if self._server is None:
            c = self.serve_cfg
            cert, key = c.get("certFile"), c.get("keyFile")
            if not (cert and key) and not c.get("allowPlainHttp"):
                raise DriverError("arduino-ota: serve needs certFile + keyFile (https) or allowPlainHttp: true (the "
                                  "image's sha256 travels in the signed command, the transfer itself is then "
                                  "unencrypted)")
            host = c.get("host", "0.0.0.0")
            if not c.get("publicUrl") and host in ("0.0.0.0", "::", ""):
                raise DriverError("arduino-ota: serve.publicUrl is needed when serving on all interfaces "
                                  "(the URL the boards download from)")
            try:
                self._server = OtaServer(host, int(c.get("port", 8470)), c.get("publicUrl"), cert, key)
            except (OSError, ssl.SSLError) as e:
                raise DriverError(f"arduino-ota: cannot serve images on {host}:{c.get('port', 8470)} ({e})") from e
        return self._server

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for r in self._redis.values():
            r.close()

    def _digest(self, path: str) -> tuple[str, int]:
        st = os.stat(path)
        k = (path, st.st_size, st.st_mtime_ns)
        if k not in self._digests:
            self._digests[k] = file_digest(path)
        return self._digests[k]

    def _send(self, name: str, wl: Workload) -> dict:
        b = self.boards[name]
        key = self._key(name)
        sha, size = self._digest(wl.payload_path)
        srv = self.server()
        tok, url = srv.offer(wl.payload_path, size, self.update_timeout + 60, b.get("address"))
        params = {"board": b["fqbn"], "sha256": sha, "size": size, "url": url, "version": self._fw(wl)}
        cmd = compose_command(key, params, expiry_seconds=int(max(60, self.accept_timeout * 3)))
        c = self._conn(name, "mqtt")
        m = MqttClient(c.get("host", "127.0.0.1"), int(c.get("port", 1883)),
                       f"flexengine-{self.device}-{secrets.token_hex(3)}", c.get("username"), self._secret(c),
                       timeout=self.wire_timeout)
        try:
            m.connect()
            m.publish(cmd_topic(b["connectorId"], b["applicationId"]), json.dumps(cmd, separators=(",", ":")), qos=1)
        except WireError as e:
            srv.revoke(tok)
            raise DriverError(f"arduino-ota: cannot send the OTA command to {b['applicationId']}: {e}") from e
        finally:
            m.close()
        return {"ref": cmd["id"], "token": tok, "digest": wl.digest, "version": wl.version, "fw": self._fw(wl),
                "phase": "sent", "sentAt": time.time()}

    def _wait(self, name: str, s: dict, t: dict) -> None:
        """Follow the node's `ota` key until the update ended; DriverError (fail fast) unless it booted `t['fw']`."""
        try:
            self._follow(name, s, t)
        except DriverError as e:
            t.update(phase="failed", reason=str(e))
            self._save(s)
            if self._server is not None:
                self._server.revoke(t.get("token"))
            raise

    def _follow(self, name: str, s: dict, t: dict) -> None:
        b = s["boards"][name]
        app = self.boards[name]["applicationId"]
        while True:
            now = time.time()
            if now - t["sentAt"] > self.update_timeout:
                raise DriverError(f"no answer from {app} within {self.update_timeout:g} s after the OTA command (last "
                                  f"state {t['phase']}): the board did not come back")
            try:
                obs = self._observe(name)
            except WireError:
                time.sleep(self.poll)      # Redis glitch during the update: keep following
                continue
            ota = obs["ota"] if (obs["ota"] or {}).get("ref") == t["ref"] else None
            if ota is None:
                if t["phase"] == "sent" and now - t["sentAt"] > self.accept_timeout:
                    raise DriverError(f"{app} did not accept the OTA command within {self.accept_timeout:g} s (node "
                                      "offline, wrong command key, or a flexNode-arduino without the remote OTA "
                                      "command)")
            else:
                state = ota.get("state")
                if state in OTA_STATES and state != t["phase"]:
                    t["phase"] = state
                    self._save(s)
                if state == "rejected":
                    raise DriverError(f"{app} rejected the OTA command: {ota.get('reason') or 'no reason given'}")
                if state == "failed":
                    raise DriverError(f"OTA failed on {app}, it keeps firmware "
                                      f"{ota.get('running') or obs.get('version')}: "
                                      f"{ota.get('reason') or 'no reason given'}")
                if state == "booted":
                    if ota.get("running") != t["fw"]:
                        raise DriverError(f"{app} restarted into firmware {ota.get('running')}, not {t['fw']}: the "
                                          "bootloader did not install the image")
                    b["installed"] = {"digest": t["digest"], "version": t["version"], "fw": t["fw"],
                                      "at": time.time()}
                    t["phase"] = "booted"
                    self._save(s)
                    self.server().revoke(t.get("token"))
                    return
            time.sleep(self.poll)

    # ---- SPI -----------------------------------------------------------------------------------------------------
    def platform(self) -> dict | None:
        fqbns = {b.get("fqbn") for b in self.boards.values()}
        return {ANN_BOARD: fqbns.pop()} if len(fqbns) == 1 and None not in fqbns else None

    def preflight(self, release, dep: dict) -> str | None:
        params = (dep.get("spec") or {}).get("parameters") or {}
        name = self._board_name(params)
        if name is None:
            return (f"board {params.get('board') or '(parameter board)'} is not configured on {self.device} "
                    f"(arduino-ota boards: {sorted(self.boards)})")
        fqbn = self.boards[name].get("fqbn")
        ann = release.payload_annotations or {}
        if not ann.get(ANN_BOARD):
            return "arduino-ota payload lacks the dev.flex.board annotation"
        if ann[ANN_BOARD] != fqbn:
            return f"firmware is for {ann[ANN_BOARD]}, board {name} is {fqbn}"
        if not ann.get(ANN_FIRMWARE):
            return "arduino-ota payload lacks the dev.flex.firmwareVersion annotation"
        try:
            ota_header(release.payload_path, fqbn)
        except (OSError, ValueError) as e:
            return f"not a usable OTA image: {e}"
        try:
            self._key(name)
        except DriverError as e:
            return str(e)
        return None

    def plan(self, current: Workload | None, desired: Workload | None) -> list[str]:
        if desired is None:
            return ["stop managing the board firmware (it keeps running)"]
        name = self._board_name(desired.parameters) or "?"
        b = self._load()["boards"].get(name) or {}
        inst = b.get("installed") or {}
        if inst.get("digest") == desired.digest:
            return [f"{self._fw(desired)} already runs on {name}: nothing to send"]
        app = (self.boards.get(name) or {}).get("applicationId", "?")
        return [f"serve the verified .ota of {desired.version} once under a one-time URL",
                f"send the signed OTA_UPDATE command to {app} on {name} (sha256 + size of the image)",
                f"wait until {app} restarted and reports firmware {self._fw(desired)} (no commit step: the Portenta "
                "bootloader has no confirm; a bad firmware is replaced by re-serving the previous release)"]

    def apply(self, current: Workload | None, desired: Workload) -> None:
        name = self._board_for(desired)
        s = self._load()
        b = s["boards"].setdefault(name, {"dep": None, "installed": None, "target": None})
        if b.get("dep") and b["dep"] != desired.dep_id:
            raise DriverError(f"one firmware per board: deployment {b['dep']} already owns board {name}")
        try:
            obs = self._observe(name)
        except WireError as e:
            raise DriverError(f"arduino-ota: flexCloud Redis of board {name} unreachable: {e}") from e
        fw, inst, t = self._fw(desired), b.get("installed") or {}, b.get("target") or {}
        b["dep"] = desired.dep_id
        if inst.get("digest") == desired.digest and obs.get("version") == fw:
            if t and t.get("digest") != desired.digest:
                b["target"] = None       # e.g. the rollback after a failed update: the old firmware still runs
                if self._server is not None:
                    self._server.revoke(t.get("token"))
            self._save(s)
            return
        if t.get("digest") == desired.digest and t.get("phase") in ("installing", "restarting") and \
                time.time() - t.get("sentAt", 0) < self.update_timeout:
            self._save(s)               # engine restarted during the update: follow it, no second command
            return self._wait(name, s, t)
        if not self._alive(obs):
            self._save(s)
            raise DriverError(f"board {name} ({self.boards[name]['applicationId']}) is offline: no heartbeat key")
        b["target"] = self._send(name, desired)
        self._save(s)
        self._wait(name, s, b["target"])

    def remove(self, dep_id: str) -> None:
        s = self._load()
        name, b = self._find(s, dep_id)
        if b is None:
            return
        if self._server is not None:
            self._server.revoke((b.get("target") or {}).get("token"))
        s["boards"].pop(name, None)
        self._save(s)

    def status(self, dep_id: str) -> Observed:
        s = self._load()
        name, b = self._find(s, dep_id)
        if b is None or name not in self.boards:
            return Observed(present=False)
        inst, t = b.get("installed") or {}, b.get("target") or {}
        ref = inst or t
        try:
            obs = self._observe(name)
        except WireError as e:
            return Observed(present=True, running=False, digest=ref.get("digest"), version=ref.get("version"),
                            detail=f"executor unreachable: {e}")
        if t and t.get("phase") not in FINAL:
            return Observed(present=True, running=False, digest=t["digest"], version=t["version"],
                            detail=f"{name}: OTA {t['phase']} ({t['fw']})")
        if not inst:
            return Observed(present=False, detail=f"{name} runs firmware {obs.get('version')}: no firmware of this "
                                                  "engine installed")
        v = obs.get("version")
        detail = f"{name}: firmware {v}, status {obs.get('status')}" + ("" if self._alive(obs) else ", no heartbeat")
        if v == inst["fw"]:
            return Observed(present=True, running=self._running(obs, inst["fw"]), digest=inst["digest"],
                            version=inst["version"], detail=detail)
        if v is None:
            return Observed(present=True, running=False, digest=inst["digest"], version=inst["version"],
                            detail=f"{name}: no version key yet")
        return Observed(present=False, detail=f"{name} runs firmware {v}, not {inst['fw']} that this engine installed")

    def list(self) -> dict[str, Observed]:
        s = self._load()
        return {b["dep"]: self.status(b["dep"]) for b in s["boards"].values() if b.get("dep")}

    def health(self, dep_id: str, probe: dict) -> bool:
        o = self.status(dep_id)
        return bool(o.present and o.running)

    def failed(self, dep_id: str) -> str | None:
        s = self._load()
        name, b = self._find(s, dep_id)
        if b is None or name not in self.boards:
            return None
        try:
            obs = self._observe(name)
        except WireError:
            return None
        t, inst = b.get("target") or {}, b.get("installed") or {}
        app = self.boards[name]["applicationId"]
        ota = obs["ota"] if t and (obs["ota"] or {}).get("ref") == t.get("ref") else None
        if ota and t.get("phase") not in FINAL:
            if ota.get("state") in ("failed", "rejected"):
                return f"OTA failed on {app}: {ota.get('reason') or ota.get('state')}"
            if ota.get("state") == "booted" and ota.get("running") != t.get("fw"):
                return f"{app} restarted into firmware {ota.get('running')}, not {t.get('fw')}"
        if inst and obs.get("version") == inst["fw"] and obs.get("status") in self.failed_status:
            return f"{app} reports status {obs['status']} on firmware {inst['fw']}"
        return None

    def capabilities(self) -> dict:
        frag = {"profile": self.profile, "driver": self.name, **self.placement(), "signedCommands": True,
                "commit": False}
        try:
            boards = {}
            for name, b in sorted(self.boards.items()):
                e = {"fqbn": b.get("fqbn"), "node": b.get("applicationId")}
                try:
                    obs = self._observe(name)
                    e.update(reachable=True, version=obs.get("version"), status=obs.get("status"),
                             online=obs.get("heartbeat") is not None)
                except WireError as err:
                    e.update(reachable=False, detail=str(err))
                boards[name] = e
            frag["boards"] = boards
            if self._server is not None:
                frag["serveUrl"] = self._server.public
        except Exception as e:  # noqa: BLE001 - capabilities never raise
            frag.update(reachable=False, detail=f"{type(e).__name__}: {e}")
        return frag

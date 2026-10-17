"""flexNode-arduino board simulator for the arduino-ota driver (EV-38), plus test firmware images and releases.

`BoardSim` plays one Portenta/Opta node running flexNode-arduino WITH the proposed remote-OTA patch
(flexengine/proposals/flexnode-arduino-remote-ota/): it talks to a real MQTT broker and a real Redis (the
in-process ones of flexengine2.testing.miniwire, or eclipse-mosquitto / redis containers) exactly as the patched
library would:

  boot          flexCloud keys: status CLOUDLINK_OK -> INITIALIZED; the sketch's set_application_version(<fw>)
                (patched: marks a pending update `booted` in the `ota` key); status RUNNING (healthy firmware) or
                ERROR (set_application_error); heartbeat key every `heartbeat_s` with a TTL of 3 periods; MQTT
                subscription to flexcloud/<cid>/<id>/cmd
  OTA_UPDATE    HMAC-SHA256 signature (flexCommunicator format, node key) -> duplicate id -> expiry -> board FQBN ->
                not busy -> `ota` accepted + status OTA_IN_PROGRESS, MQTT disconnected (START_OTA) -> download over
                HTTP -> size + sha256 (`verifying`) -> header length / CRC-32 / board magic and the body
                ("decompress", Arduino_Portenta_OTA) -> `installing` -> `restarting` -> offline for `restart_s` ->
                boot of the new firmware. A failed step: `ota` failed with the reason, status RUNNING again on the
                old firmware (AFTER_OTA), MQTT reconnected
  faults        `fault = "download"` (the download fails), `"flip"` (one byte flipped in transit: sha256 mismatch),
                `"stall"` (accepted, never finishes the download); firmware flags in the image: `healthy=False`
                (boots, reports ERROR), `boots=False` (never comes back), `corrupt=True` (header fine, the body does
                not decompress); `remote_ota=False` = the unpatched 0.1.0 library (never subscribes to commands)
  out of band   reflash(version) = a USB upload (drift), power_off() / power_on()

Test firmware images keep the real .ota header ([length][crc32][board magic][8 bytes]) around a JSON body that
describes the simulated sketch (no LZSS); the driver's preflight checks the header as on hardware.
"""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import threading
import time
import urllib.request
import uuid
import zlib
from pathlib import Path

from flexengine2.drivers.arduino_ota import (ANN_BOARD, ANN_FIRMWARE, CMD_OTA, MT_PAYLOAD_ARDUINO_OTA, OTA_MAGIC,
                                             cmd_topic, keys, verify)
from flexengine2.drivers.arduino_wire import MqttClient, RedisClient, WireError

FW_MAGIC = b"FLEXSIM-FW "
PORTENTA = "arduino:mbed_portenta:envie_m7"
OPTA = "arduino:mbed_opta:opta"


# ---- firmware images ----------------------------------------------------------------------------------------------
def firmware_body(version: str, *, healthy: bool = True, boots: bool = True, corrupt: bool = False,
                  remote_ota: bool = True, pad: int = 0) -> bytes:
    meta = {"version": version, "healthy": healthy, "boots": boots, "corrupt": corrupt, "remoteOta": remote_ota}
    return FW_MAGIC + json.dumps(meta, sort_keys=True).encode() + b"\n" + b"\xa5" * pad


def ota_image(body: bytes, fqbn: str = PORTENTA) -> bytes:
    """The Arduino_Portenta_OTA container (as make_ota.py writes it) around `body`."""
    inner = OTA_MAGIC[fqbn].to_bytes(4, "little") + bytes([0, 0, 0, 0, 0, 0, 0, 0x40]) + body
    return len(inner).to_bytes(4, "little") + zlib.crc32(inner).to_bytes(4, "little") + inner


def write_image(out: Path, version: str, *, fqbn: str = PORTENTA, **flags) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ota_image(firmware_body(version, **flags), fqbn))
    return out


def parse_image(data: bytes, fqbn: str) -> dict:
    """What the board's Arduino_Portenta_OTA decompress() + bootloader would make of the file: the sketch
    metadata, or ValueError(reason)."""
    if len(data) < 20:
        raise ValueError(f"decompress() failed with error code -1 ({len(data)} bytes)")
    length, crc = struct.unpack("<II", data[:8])
    if length != len(data) - 8 or zlib.crc32(data[8:]) != crc:
        raise ValueError("decompress() failed with error code -3 (header length / CRC)")
    if struct.unpack("<I", data[8:12])[0] != OTA_MAGIC.get(fqbn):
        raise ValueError("decompress() failed with error code -2 (wrong board magic)")
    body = data[20:]
    if not body.startswith(FW_MAGIC):
        raise ValueError("decompress() failed with error code -4 (LZSS stream)")
    meta = json.loads(body[len(FW_MAGIC):].split(b"\n", 1)[0])
    if meta.get("corrupt"):
        raise ValueError("decompress() failed with error code -4 (LZSS stream)")
    return meta


# ---- releases -----------------------------------------------------------------------------------------------------
def descriptor(module: str, version: str, health: dict | None = None) -> dict:
    return {"apiVersion": "flex.dev/v1alpha1", "kind": "FlexNodeDescriptor", "module": module,
            "moduleVersion": version, "releaseVersion": version, "interfaceHash": None,
            "profiles": ["arduino-ota"],
            "health": health or {"type": "arduino-ota", "periodSeconds": 0.2, "successThreshold": 2,
                                 "timeoutSeconds": 10},
            "safetyClass": "none", "safetyClassSource": "release tool (board firmware)"}


def make_arduino_release(layout, release_key, image: Path, *, module: str = "board_fw", version: str,
                         fqbn: str = PORTENTA, firmware_version: str | None = None, health: dict | None = None,
                         sign: bool = True) -> dict:
    """Wrap a .ota image as a signed flexNode release. Returns the info item a desired state needs."""
    from flexengine2.testing.release import publish_release, sbom_stub, sign_release

    repo = f"flexnodes/{module}"
    ann = {"dev.flex.profile": "arduino-ota", ANN_BOARD: fqbn, ANN_FIRMWARE: firmware_version or version}
    mdesc = publish_release(layout, repo, descriptor(module, version, health),
                            [(MT_PAYLOAD_ARDUINO_OTA, Path(image), ann)], sbom_stub(module, version, "arduino-ota"))
    if sign:
        sign_release(layout, release_key, repo, mdesc, version)
    return {"repository": repo, "digest": mdesc["digest"], "size": mdesc["size"], "version": version,
            "profile": "arduino-ota", "interfaceHash": None, "safetyClass": "none"}


# ---- the board ----------------------------------------------------------------------------------------------------
class BoardSim:
    def __init__(self, *, connector: str, app_id: str, key: bytes, mqtt: tuple[str, int], redis: tuple[str, int],
                 fqbn: str = PORTENTA, redis_db: int = 15, redis_password: str | None = None,
                 mqtt_user: str | None = None, mqtt_password: str | None = None, version: str = "0.0.1-factory",
                 heartbeat_s: float = 0.3, restart_s: float = 0.3, check_sha: bool = True, clock: bool = True):
        self.cid, self.app, self.key, self.fqbn = connector, app_id, key, fqbn
        self.mqtt_addr, self.redis_addr = mqtt, redis
        self.redis_db, self.redis_password = redis_db, redis_password
        self.mqtt_user, self.mqtt_password = mqtt_user, mqtt_password
        self.heartbeat_s, self.restart_s, self.check_sha, self.clock = heartbeat_s, restart_s, check_sha, clock
        self.k = keys(connector, app_id)
        self.topic = cmd_topic(connector, app_id)
        self.firmware = {"version": version, "healthy": True, "boots": True, "remoteOta": True}
        self.fault: str | None = None
        self.downloads = 0                # complete image downloads (the contract suite's "transfers")
        self.commands: list[dict] = []    # every parsed command, verified or not
        self.boots = 0
        self.busy = False
        self.seen: list[str] = []
        self.boot_id = None
        self._gen = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._mqtt: MqttClient | None = None
        self.on = False

    # -- lifecycle --
    def start(self) -> "BoardSim":
        self.power_on()
        return self

    def power_on(self) -> None:
        with self._lock:
            self._gen += 1
            gen, self.on = self._gen, True
            self._stop = threading.Event()
        threading.Thread(target=self._run, args=(gen, self._stop), name=f"board-{self.app}", daemon=True).start()

    def power_off(self) -> None:
        with self._lock:
            self.on = False
            self._gen += 1
            self._stop.set()
        self._drop_mqtt()

    stop = power_off

    def reflash(self, version: str, **flags) -> None:
        """A USB upload of another sketch (out of band): the board restarts into it."""
        self.power_off()
        self.firmware = {"version": version, "healthy": True, "boots": True, "remoteOta": True, **flags}
        self.power_on()

    def wait_ready(self, timeout: float = 10) -> None:
        end = time.time() + timeout
        r = self._redis()
        while time.time() < end:
            try:
                if r.get(self.k["version"]) == self.firmware["version"] and r.get(self.k["heartbeat"]):
                    return
            except WireError:
                pass
            time.sleep(0.05)
        raise TimeoutError(f"board {self.app} not ready")

    # -- the library --
    def _redis(self) -> RedisClient:
        return RedisClient(self.redis_addr[0], self.redis_addr[1], self.redis_password, self.redis_db, timeout=2)

    def _drop_mqtt(self) -> None:
        m, self._mqtt = self._mqtt, None
        if m is not None:
            m.close()

    def _log(self, r: RedisClient, level: str, msg: str) -> None:
        try:
            r.rpush(self.k["logs"], f"{self.app} - {level} - {msg}")
        except WireError:
            pass

    def _ota_state(self, r: RedisClient, **st) -> None:
        r.set(self.k["ota"], json.dumps(st, sort_keys=True))

    def _alive(self, gen: int) -> bool:
        return self.on and gen == self._gen

    def _run(self, gen: int, stop: threading.Event) -> None:
        fw = dict(self.firmware)
        if not fw.get("boots", True):
            return                          # the image never comes up: no keys, no heartbeat
        self.boots += 1
        self.boot_id = uuid.uuid4().hex[:16]
        r = self._redis()
        t0 = time.time()
        started = False
        last_hb = 0.0
        while not stop.is_set() and self._alive(gen):
            try:
                if not started:
                    # begin(): flexCloud.setup() -> CLOUDLINK_OK, then INITIALIZED
                    r.set(self.k["status"], "CLOUDLINK_OK")
                    r.set(self.k["status"], "INITIALIZED")
                    # sketch: set_application_version(); patched library marks a pending update booted
                    r.set(self.k["version"], fw["version"])
                    raw = r.get(self.k["ota"])
                    st = json.loads(raw) if raw else {}
                    if st.get("state") == "restarting":
                        self._ota_state(r, **{**st, "state": "booted", "running": fw["version"],
                                              "bootId": self.boot_id})
                    self._log(r, "INFO", "flexNode-v0.2.0-proposed")
                    r.set(self.k["status"], "RUNNING" if fw.get("healthy", True) else "ERROR")
                    started = True
                if time.time() - last_hb >= self.heartbeat_s:
                    hb = {"bootId": self.boot_id, "uptime": int(time.time() - t0), "version": fw["version"],
                          "status": r.get(self.k["status"])}
                    r.set(self.k["heartbeat"], json.dumps(hb, sort_keys=True), ex=max(1, int(self.heartbeat_s * 3 + 0.999)))
                    last_hb = time.time()
                if fw.get("remoteOta", True) and not self.busy and (self._mqtt is None or self._mqtt.closed.is_set()):
                    self._connect_mqtt()
            except WireError:
                r.close()                     # spin(): retried at the next interval
            stop.wait(min(0.05, self.heartbeat_s / 4))
        self._drop_mqtt()

    def _connect_mqtt(self) -> None:
        self._drop_mqtt()
        m = MqttClient(self.mqtt_addr[0], self.mqtt_addr[1], self.app, self.mqtt_user, self.mqtt_password,
                       keepalive=10, timeout=2)
        try:
            m.connect()
            m.subscribe(self.topic)
        except WireError:
            m.close()
            return
        m.start(self._on_message)
        self._mqtt = m

    def _on_message(self, topic: str, payload: bytes) -> None:
        if topic != self.topic:
            return
        r = self._redis()
        try:
            cmd = json.loads(payload)
            if not isinstance(cmd, dict):
                raise ValueError("not an object")
        except ValueError:
            self._log(r, "WARNING", "command: not JSON")
            return
        self.commands.append(cmd)
        if not verify(self.key, cmd):
            self._log(r, "WARNING", "OTA_UPDATE rejected: bad signature")
            return
        if cmd["id"] in self.seen:
            self._log(r, "WARNING", "command ignored: already received")
            return
        self.seen = (self.seen + [cmd["id"]])[-8:]
        p = cmd.get("params") or {}
        ref = cmd["id"]
        running = self.firmware["version"]
        if cmd.get("cmd") != CMD_OTA:
            self._log(r, "WARNING", f"unknown command {cmd.get('cmd')}")
            return
        why = None
        if self.clock and int(cmd.get("expiry", 0)) < time.time():
            why = "command expired"
        elif p.get("board") != self.fqbn:
            why = f"image is for {p.get('board')}, this node is {self.fqbn}"
        elif self.busy:
            why = "an update is already in progress"
        if why:
            self._ota_state(r, ref=ref, state="rejected", reason=why, version=p.get("version"), running=running)
            self._log(r, "WARNING", f"OTA_UPDATE rejected: {why}")
            return
        self.busy = True
        self._ota_state(r, ref=ref, state="accepted", version=p.get("version"), running=running)
        r.set(self.k["status"], "OTA_IN_PROGRESS")
        self._log(r, "INFO", f"OTA_UPDATE accepted: {p.get('version')}")
        threading.Thread(target=self._ota, args=(ref, p, self._gen), daemon=True).start()

    def _ota(self, ref: str, p: dict, gen: int) -> None:
        r = self._redis()
        running = self.firmware["version"]
        self._drop_mqtt()                        # START_OTA: mqtt_disconnect()

        def fail(reason: str):
            self._log(r, "ERROR", reason)
            self._log(r, "ERROR", "OTA update failed, continuing with the current firmware")
            self._ota_state(r, ref=ref, state="failed", reason=reason, version=p.get("version"), running=running)
            r.set(self.k["status"], "RUNNING" if self.firmware.get("healthy", True) else "ERROR")   # AFTER_OTA
            self.busy = False

        try:
            self._ota_state(r, ref=ref, state="downloading", version=p.get("version"), running=running)
            self._log(r, "INFO", f"Starting download to QSPI from {p.get('url')}")
            if self.fault == "stall":
                return
            if self.fault == "download":
                return fail("download failed with error code -3")
            try:
                with urllib.request.urlopen(p["url"], timeout=10) as resp:
                    data = resp.read()
            except OSError as e:
                return fail(f"download failed with error code -3 ({e})")
            self.downloads += 1
            if self.fault == "flip" and data:
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            self._log(r, "INFO", f"{len(data)} bytes stored.")
            self._ota_state(r, ref=ref, state="verifying", version=p.get("version"), running=running)
            if self.check_sha:
                if len(data) != int(p.get("size", -1)):
                    return fail(f"size {len(data)} != {p.get('size')}")
                if hashlib.sha256(data).hexdigest() != p.get("sha256"):
                    return fail("sha256 mismatch: image corrupt or not the one the command names")
            try:
                meta = parse_image(data, self.fqbn)
            except ValueError as e:
                return fail(str(e))
            self._ota_state(r, ref=ref, state="installing", version=p.get("version"), running=running)
            self._ota_state(r, ref=ref, state="restarting", version=p.get("version"), running=running)
            self._log(r, "INFO", "Restarting the board ...")
        except WireError:
            self.busy = False
            return
        if gen != self._gen:
            return
        # ota.reset(): offline, the bootloader installs the image, the new sketch boots
        self.power_off()
        time.sleep(self.restart_s)
        self.firmware = {"version": meta.get("version"), "healthy": meta.get("healthy", True),
                         "boots": meta.get("boots", True), "remoteOta": meta.get("remoteOta", True)}
        self.busy = False
        self.power_on()


# ---- nightly tier: real eclipse-mosquitto + redis containers ------------------------------------------------------
MOSQUITTO_IMAGE = "eclipse-mosquitto:2.0.22"
REDIS_IMAGE = "redis:7.4-alpine"


class Containers:
    """A real MQTT broker and Redis in Docker, published on 127.0.0.1 (marker `docker`)."""

    def __init__(self, tag: str):
        self.tag = tag
        self.names: list[str] = []
        self.mqtt_port = self.redis_port = 0

    @staticmethod
    def available() -> bool:
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _run(self, name: str, image: str, port: int, *cmd: str) -> int:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        p = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1::{port}", image, *cmd],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(f"docker run {image}: {p.stderr.strip()}")
        self.names.append(name)
        out = subprocess.run(["docker", "port", name, str(port)], capture_output=True, text=True, timeout=30).stdout
        return int(out.split()[0].rsplit(":", 1)[1])

    def start(self) -> "Containers":
        self.mqtt_port = self._run(f"fe-mqtt-{self.tag}", MOSQUITTO_IMAGE, 1883, "sh", "-c",
                                   "printf 'listener 1883\\nallow_anonymous true\\n' > /tmp/m.conf && "
                                   "exec mosquitto -c /tmp/m.conf")
        self.redis_port = self._run(f"fe-redis-{self.tag}", REDIS_IMAGE, 6379)
        end = time.time() + 30
        while time.time() < end:
            try:
                RedisClient("127.0.0.1", self.redis_port, timeout=1).ping()
                MqttClient("127.0.0.1", self.mqtt_port, f"probe-{self.tag}", timeout=1).connect().close()
                return self
            except WireError:
                time.sleep(0.3)
        self.stop()
        raise RuntimeError("broker / redis containers did not come up")

    def stop(self) -> None:
        for n in self.names:
            subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)
        self.names = []


# ---- a lab: broker + Redis + simulated boards + the driver config that reaches them -------------------------------
class ArduinoLab:
    """`real=False`: in-process MiniBroker / MiniRedis (push tier); `real=True`: mosquitto + redis containers."""

    def __init__(self, root: Path, *, real: bool = False, boards: tuple[str, ...] = ("p1",), fqbn: str = PORTENTA,
                 connector: str = "flexIA@lab-1fd420", redis_db: int = 15, **sim_kw):
        self.root = Path(root)
        self.real, self.board_names, self.fqbn, self.connector, self.redis_db = real, boards, fqbn, connector, redis_db
        self.sim_kw = sim_kw
        self.sims: dict[str, BoardSim] = {}
        self.keys: dict[str, bytes] = {}
        self.containers: Containers | None = None
        self.broker = self.redis = None

    def start(self) -> "ArduinoLab":
        self.root.mkdir(parents=True, exist_ok=True)
        if self.real:
            self.containers = Containers(uuid.uuid4().hex[:8]).start()
            self.mqtt_port, self.redis_port = self.containers.mqtt_port, self.containers.redis_port
        else:
            from flexengine2.testing.miniwire import MiniBroker, MiniRedis
            self.broker, self.redis = MiniBroker().start(), MiniRedis().start()
            self.mqtt_port, self.redis_port = self.broker.port, self.redis.port
        for i, name in enumerate(self.board_names):
            key = hashlib.sha256(f"node-key-{name}-{self.root}".encode()).digest()
            self.keys[name] = key
            kf = self.root / f"{name}.key"
            kf.write_text(", ".join(f"0x{b:02x}" for b in key) + "\n")     # as in the sketch's configuration.h
            self.sims[name] = BoardSim(connector=self.connector, app_id=f"OTA_{i + 1:02d}", key=key, fqbn=self.fqbn,
                                       mqtt=("127.0.0.1", self.mqtt_port), redis=("127.0.0.1", self.redis_port),
                                       redis_db=self.redis_db, **self.sim_kw).start()
        for s in self.sims.values():
            s.wait_ready()
        return self

    def sim(self, name: str | None = None) -> BoardSim:
        return self.sims[name or self.board_names[0]]

    def driver_cfg(self, *, mqtt_port: int | None = None, redis_port: int | None = None, **kw) -> dict:
        return {"boards": {n: {"fqbn": self.fqbn, "connectorId": self.connector, "applicationId": s.app,
                               "commandKeyFile": str(self.root / f"{n}.key")} for n, s in self.sims.items()},
                "mqtt": {"host": "127.0.0.1", "port": mqtt_port or self.mqtt_port},
                "redis": {"host": "127.0.0.1", "port": redis_port or self.redis_port, "db": self.redis_db},
                "serve": {"host": "127.0.0.1", "port": 0, "allowPlainHttp": True},
                "pollSeconds": 0.05, "acceptTimeoutSeconds": 3, "updateTimeoutSeconds": 15, "timeout": 1.0, **kw}

    def image(self, out_dir: Path, version: str, **flags) -> Path:
        tag = "-".join(f"{k}{int(v) if isinstance(v, bool) else v}" for k, v in sorted(flags.items()))
        return write_image(Path(out_dir) / f"fw-{version}{'-' + tag if tag else ''}.ota", version, fqbn=self.fqbn,
                           **flags)

    def stop(self) -> None:
        for s in self.sims.values():
            s.power_off()
        if self.containers:
            self.containers.stop()
        for srv in (self.broker, self.redis):
            if srv is not None:
                srv.stop()

# Proposed upstream change: remote OTA command for flexNode-arduino (for flexEngineV2 EV-38)

**Status: PROPOSED, not applied.** flexNode-arduino is read-only for this project (rule S7: no change to the flex*
repositories without the lead's go). `remote-ota.patch` is a unified diff against flexNode-arduino `main` at
`b83b5fa` ("Release 0.1.0: P0 complete"); it applies cleanly there (`patch -p1 --dry-run`) and has not been pushed
anywhere. To apply after approval, in a flexNode-arduino checkout: `git apply remote-ota.patch` (or `patch -p1`).

## Why

flexNode-arduino 0.1.0 can update itself over Arduino_Portenta_OTA (`otaManager`, `trigger_ota()`, QC-03), but only
from a URL compiled into the sketch, started by the sketch itself. The flexEngineV2 driver `arduino-ota`
(`flexengine/src/flexengine2/drivers/arduino_ota.py`) deploys board firmware like any other release: the engine
verifies the signed release, serves the `.ota` image on the LAN and must be able to tell the node "install this
image now", then see which firmware came up. The patch adds exactly that and nothing else.

## Protocol (what the patch implements and the driver speaks)

**Command channel.** MQTT (the node's existing PubSubClient connection, `cm_MQTT_config`), topic

    flexcloud/<connectorID>/<applicationID>/cmd

(`appDescriptor_config.connectorID` / `.id`). The driver publishes with QoS 1, not retained. MQTT rather than
flexCommunicator's Redis channel `<cid>:<id>:cmd`: the vendored Arduino Redis client's subscription mode blocks
(`startSubscribing()`) and would need a third TCP connection; the MQTT connection is already non-blocking, reconnects
on its own and resubscribes every registered topic.

**Message** — flexCommunicator's `compose_command()` shape (flcpy `commands/composer.py`), `cmd = "OTA_UPDATE"`:

```json
{"id": "6f1c2c1e-1d0e-4c55-9b3e-0d7f0b6f8a51", "cmd": "OTA_UPDATE", "priority": 2,
 "expiry": 1792235460, "timestamp": 1792235400, "nonce": "3b0e0a8f6f7c4c8f9d1e2a3b4c5d6e7f",
 "params": {"board": "arduino:mbed_portenta:envie_m7",
            "sha256": "f8b0585eb91f58c007a5634362c9f90d8543822c113f702523bc7b73408a9392",
            "size": 412377,
            "url": "http://192.168.1.50:8470/ota/kq3V0n9mX2Lr8sT1uY4wZ6aB5cD7eF9g.ota",
            "version": "0.2.0"},
 "signature": "<64 hex>"}
```

`signature` = lower-case hex HMAC-SHA256 with the node's `appDescriptor_config.flexCloud_secret_key` (32 bytes) over

    id + cmd + str(priority) + str(expiry) + json.dumps(params, sort_keys=True) + str(timestamp) + nonce

which is flcpy's `compute_signature()` input. The node rebuilds the `json.dumps` text from exactly these five params
(`{"board": "...", "sha256": "...", "size": N, "url": "...", "version": "..."}`, keys sorted, `", "` / `": "`
separators); a command with other params, or with characters `json.dumps` would escape, does not verify. `url` is
a one-time, unguessable path on the engine's own server (192-bit token, one complete download, expires with the
update timeout); `sha256` and `size` are those of the `.ota` file the engine verified.

**Node checks**, in this order (`src/commands/otaCommand.cpp`, `FlexNode::handle_command`):

| Check | Failure |
|---|---|
| JSON, `cmd == "OTA_UPDATE"`, the five params well formed (`sha256` 64 lower-case hex) | logged (`OTA_UPDATE rejected: malformed command`), nothing written |
| signature (constant-time compare) | logged (`OTA_UPDATE rejected: bad signature`), nothing written: an unauthenticated message never touches the flexCloud keys |
| `id` not among the last 8 executed | logged (`already received`) |
| `expiry` — only when the board's clock is set (`time() > 1700000000`; NTP arrives in P2) | `ota` = rejected `command expired` |
| `ota_config.OTA_enabled` | rejected `OTA disabled in ota_config` |
| `params.board` == the FQBN the library is compiled for (`FLEXNODE_FQBN`) | rejected `image is for <board>, this node is <fqbn>` |
| no update in progress | rejected `an update is already in progress` |

Then: `ota` = accepted, status `OTA_IN_PROGRESS`, `set_ota_url(url)`, `expect(sha256, size)`, `trigger_ota()`.
`spin()` runs the existing START_OTA → DO_OTA → AFTER_OTA path; `otaManager::ota_update` reports each step and, new,
checks the downloaded file's size and SHA-256 (mbedtls, read back from `/fs/UPDATE.BIN.LZSS`) before
`decompress()` (which checks the header CRC and board magic as before).

**Progress key** (flexCloud Redis, SET): `<connectorID>:applications:<applicationID>:ota`

```json
{"ref": "<command id>", "state": "accepted|downloading|verifying|installing|restarting|booted|failed|rejected",
 "version": "<target version>", "running": "<version running now>", "reason": "<failed/rejected only>",
 "bootId": "<booted only>"}
```

A failed step writes `failed` with the reason (download error code, `size N != M`, `sha256 mismatch ...`,
`Arduino_Portenta_OTA_QSPI::decompress() failed with error code N`, `ota.update() failed ...`); AFTER_OTA then sets
status `RUNNING` again on the unchanged firmware, as today. `restarting` is written right before `ota.reset()`. The
first `set_application_version()` after the restart marks a pending `restarting` entry `booted` with
`running` = the version of the firmware that came up — the new one, or the old one when the bootloader did not
install the image.

**Heartbeat key** (SET + EXPIRE `3 × FLEXNODE_HEARTBEAT_S`, written every `FLEXNODE_HEARTBEAT_S` = 5 s from `spin()`):
`<connectorID>:applications:<applicationID>:heartbeat` = `{"bootId": "<16 hex, new at every boot>", "uptime": s,
"version": "...", "status": "RUNNING"}`. It tells the engine a board is alive (the `status` / `version` keys keep
their last value forever) and when it rebooted.

Existing keys are unchanged (`status`, `version`, `logs`).

## Patch content

| File | Change |
|---|---|
| `src/commands/otaCommand.{h,cpp}` (new) | parse + authenticate `OTA_UPDATE` (ArduinoJson, mbedtls HMAC), duplicate ids, expiry, `sha256_file_hex()`, `FLEXNODE_FQBN` |
| `src/flexNode.{h,cpp}` | subscribe to the command topic in `begin()`; `callback()` hands the command topic to `handle_command()` before the 512-byte data document; `ota` progress via `report_ota()`; booted marker in `set_application_version()`; heartbeat in `spin()`; boot id |
| `src/ota/otaManager.{h,cpp}` | `expect(sha256, size)`, size + SHA-256 check after the download, progress callback `on_state()`; error paths go through `fail()` (same log texts) |
| `src/flexCloud/flexCloudManager.{h,cpp}` | `set_ota_state()`, `ota_state()`, `heartbeat()`, getters for the last version / status |
| `examples/features/otaRemote/` (new) | a sketch that only reports its version; updates come from the engine |
| `README.adoc` | section "Remote OTA" |

A sketch-triggered OTA (`examples/features/ota`, QC-03) works as before: no `ref`, nothing written to the `ota` key,
no size/sha256 expectation.

## Security notes

* **No signature check on the board.** Arduino_Portenta_OTA and the Portenta bootloader check only the `.ota` header
  (length, CRC-32, board magic). Authenticity comes from the engine: it verifies the release (Ed25519 release
  signature over the OCI manifest, so over the image digest) and the node executes only commands signed with its
  key, which name the image's sha256 and size. Whoever holds the node key (it is compiled into the sketch) or has
  USB access can still install anything.
* **LAN serving.** Plain http only with the driver's lab flag `serve.allowPlainHttp: true` (decision DA-4);
  integrity holds through the signed sha256, confidentiality of the firmware does not. https works when the board's
  Arduino_Portenta_OTA trusts the server's CA (`serve.certFile` / `keyFile`; untested on the board). One-time,
  192-bit paths, optional per-board source address (`boards.<n>.address`).
* **Replay.** Duplicate ids are refused while the board runs; after a reboot an old command could be replayed, but
  its one-time URL is spent, so the download fails and nothing changes. `expiry` is enforced once NTP time exists.
* **MQTT / Redis in clear text**, as all flexNode-arduino traffic today (PubSubClient / Redis client over plain TCP).
* **No rollback on the board.** The bootloader has no test/confirm step: a firmware that boots but is unhealthy
  stays installed until the engine re-serves the previous release (the reconciler's rollback). A firmware that never
  starts the library (never comes back) needs a USB re-flash.

## Verification so far

* `hostcheck/`: `src/commands/otaCommand.cpp` compiled for the PC (`g++ -std=c++14 -Wall -Wextra`, ArduinoJson
  6.21.5, system mbedtls 2.28, a 30-line `String` shim) verifies commands signed by the Python driver and refuses
  tampered, extra-param, expired and wrong-key commands; `sha256_file_hex()` equals Python's hashlib. Run:
  `PYTHONPATH=flexengine/src sh hostcheck/run.sh <flexNode-arduino checkout>` (the checkout is only read; needs g++,
  libmbedcrypto 2.28, network for ArduinoJson).
* `patch -p1 --dry-run` on `b83b5fa`: clean.
* Board build (2026-10-17, arduino-cli 1.3.1, mbed cores 4.6.0, ArduinoJson 6.21.5, PubSubClient, Arduino_Portenta_OTA
  1.2.1): the patched library builds `examples/features/otaRemote` for `arduino:mbed_portenta:envie_m7` and
  `arduino:mbed_opta:opta` (Opta: 487 KB flash / 24 %, 70.7 KB RAM / 13 %) and the unchanged `examples/features/ota`
  for the H7, with no warnings in the changed files. The download file `/fs/UPDATE.BIN.LZSS` and the header checks of
  `decompress()` (length, CRC-32, board magic) were read from Arduino_Portenta_OTA 1.2.1 (`decompress/utility.cpp`).
* Not run on hardware. The engine side is covered by the flexEngineV2 contract suite, unit and integration tests
  against `flexengine2/testing/arduino_sim.py`, which implements this protocol over a real MQTT broker and Redis.

## Desk runbook (after approval; extends HW-06)

Portenta H7 (or Machine Control / Opta) with the OTA bootloader and a QSPI partition (as QC-03), the patched library,
`examples/features/otaRemote` built twice with `make_ota.py` (FIRMWARE_VERSION 0.1.0 uploaded over USB, 0.2.0 and a
deliberately unhealthy 0.3.0 as `.ota` releases), Mosquitto and the flexCloud Redis on the bench, the engine with
`drivers.arduino-ota` (board `connectorId`/`applicationId`, `commandKeyFile` = the sketch's key, `serve.publicUrl`
reachable from the board). Steps: deploy 0.2.0 → `ota` key accepted … booted, version 0.2.0, Installed; deploy 0.3.0
(sets ERROR) → Failed fail-fast, the engine re-serves 0.2.0; corrupt the image in transit (proxy flipping a byte, or
a release whose LZSS body is truncated) → `ota` failed, the board keeps 0.2.0, Failed; power the board off during the
download → "did not come back", the board comes back on 0.2.0.

## Open points for the lead

1. Key form: flcpy signs with the *string* of its secret (`SHARED_SECRET.encode()`, by default the Python `repr` of
   the bytes); this patch uses the 32 raw key bytes the sketch already holds. P4 (signed PING / UPDATE_TRANSPORT)
   should settle one form for all libraries.
2. Topic name `flexcloud/<cid>/<id>/cmd` vs the Redis channel `<cid>:<id>:cmd` of flexCommunicator.
3. ESP32 boards (P5) would reuse the command and keys with `HTTPUpdate` / `esp_https_ota` behind `otaManager`.

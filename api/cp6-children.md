# CP-6 Child-device interfaces, v1alpha1

The exact messages the engine's child drivers use today, from the code. One section per child type; the stand-in
that conforms is named per section. Evidence: [V] = read from the code and exercised by the driver suites
(P3 on QEMU, P4 fake CLI, D1 stand-ins); [I] = not checked.

## 1. MCU (flexEngine-MCU base image) — `drivers/mcu/{link,driver,flxm}.py`

**Transport.** SMP serial framing (`smp.packet.encode`, line length 127, base64 lines with start delimiter) over a
byte pipe: TCP to QEMU `-serial tcp:` (SIL) or a serial-to-TCP bridge (lab). One pipe per MCU, opened per call or per
`session()`, so other SMP clients can take turns; a response is matched by sequence number [V].

**SMP header** (8 bytes, `!BBHHBB`): `(version 1 << 3) | op`, flags 0, payload length, group, seq, command id. Payload
CBOR. op 0 (read) for `info`/`status`, 2 (write) for the rest.

**Group 64 `flexmod`** [V]:

| Cmd | Id | Request | Response fields read |
|---|---|---|---|
| info | 0 | `{}` | `arch` (armv7m\|armv8m.main), `float_abi` (soft\|hard), `abi_major`, `abi_minor`, `caps` (bitmask), `sockets`, `slot_size`, `llext_heap`, `image` (base version, `x.y.z+n`), `confirmed`, `board` |
| status | 1 | `{}` | `sockets[]`: `sock`, `dep`, `state` (empty\|testing\|confirmed), `active`, `confirmed`, `slots[]{sha, valid}`, `healthy`, `hb`, `steps`, `errors`, `version`, `params_crc`, `fault_reverts`, `fault_age_ms`, `last_reason`, `last_rc`, `auto_reverts`; relay: `running`, `last_topic`, `pubs`, `last_msg` |
| upload | 2 | `{sock, off, data ≤ 768 B}` + `{len, sha}` at `off == 0` | `rc`, `off` (resume point), `reason` |
| activate | 3 | `{sock, slot, dep, params "k=v;…", test: true, win?}` (`win` = module test window + 3000 ms) | `rc`, `state`, `switch_us`, `load_us`, `verify_us`, `reason` |
| confirm | 4 | `{sock}` | `rc`, `reason` |
| revert | 5 | `{sock}` | `rc` |
| remove | 6 | `{sock}` | – |
| inject | 7 | `{topic u32, data ≤ 64 B XCDR2}` (base images after 2026-10-16) | `rc`, `delivered`, `reason` |

An answer with `err`, or a non-zero integer `rc` without `reason`, is an SMP error (FE-MCU-008); `rc != 0` with a
`reason` is a refusal the driver turns into a `DriverError` text (FE-MCU-005/006/007).

**Standard groups** (typed messages of the `smp` package) [V]: `img_mgmt` ImageStatesRead, ImageUploadWrite
(chunks of 768 B, image 0, `len`+`sha` in the first), ImageStatesWrite(`hash`, confirm=false) = mark test,
ImageStatesWrite(confirm=true) = confirm; `os_mgmt` Reset (no answer expected), Echo. Firmware flow: upload to slot 1
→ test → reset → wait for `flexmod info` (≤ 60 s) → health → confirm; rollback = reset without confirm (MCUboot swap
back). MCUboot image check: little-endian magic `0x96F3B83D` at offset 0.

**FLXM v1 module container** (`flxm.py`, mirror of `zephyr/base/include/flex_mcu_abi.h`) [V]:

```
[header 160 B][LLEXT ELF][sig TLV: type u16 = 1, len u16 = 64][ECDSA P-256 r‖s over SHA-256(header ‖ ELF)]
header (<, little endian):
  0 magic u32 0x4D584C46 "FLXM" | 4 hdr_size u16 160 | 6 fmt_version u16 1 | 8 abi_major u16 | 10 abi_minor u16
 12 float_abi u8 (1 soft, 2 hard) | 13 arch u8 (1 armv7m, 2 armv8m.main) | 14 flags u16 | 16 caps_required u32
 20 elf_size u32 | 24 ram_estimate u32 | 28 step_period_ms u32 | 32 name [24] | 56 version [16]
 72 iface_hash [32] (FLEX01) | 104 elf_sha256 [32] | 136 test_window_ms u32 | 140 reserved [20]
caps bits: pwm 0, localbus 1, heap 2, fpu 3, zenoh 4
```

Preflight (FE-MCU-PRE-*): parse, ELF hash, signature with `modulePublicKey` (hex X‖Y), ABI major equal and minor ≤
base, arch/float ABI equal, required caps offered, interface hash = descriptor and = `parameters._interfaceHash`,
undefined ELF symbols ⊆ {memcpy, memset, memcmp, strlen}, size ≤ slot, RAM estimate ≤ heap/2.
Stand-in: the P3 base image on QEMU mps2/an385 (`probes/p3-mcu-hotload`).

## 2. App Lab (Arduino UNO Q) — `drivers/applab/{driver,ard,rpc}.py`

**CLI subset** (`arduino-app-cli … --format json`; remote mode wraps it in `ssh -o BatchMode=yes <host>`) [V]:

| Call | Used for | JSON read |
|---|---|---|
| `version` | capabilities | whole object |
| `app ps --all` | status, foreign-app check | `apps[]{id, release_id, name, status}` (running\|starting\|failed\|…) |
| `app list --releases` | installed? | `apps[]{id, release_id}` |
| `app install <rel>.ard --yes` | install | `path` (app dir; parameters go to `<path>/data/flex_params.json`) |
| `app start release:<rel>` / `app stop release:<rel>` / `app stop <alias>` | switch, rollback, takeover | – |
| `app destroy release:<rel>` | remove, GC (keep 2) | – |
| `app logs release:<rel> --tail 200` | health via logs: line `FLEX_CONTRACT_OK <hash>` | text |

Failure: exit ≠ 0; the error text is `error` of the last stderr line if it is JSON (FE-APL-001).
`flex_params.json` = parameters split as `{instance: {name: value}, "*": {bare: value}}`.

**`.ard` archive**: tar.gz rooted in one folder `<releaseName>/` with `release.yaml` `{name, target, schema ≤ 1,
release_label, created_at}`, `src/python/flex_bridge.py` (`CONTRACT_HASH = "FLEX01_…"`), `src/sketch/flex_bridge.h`
(`CONTRACT_HASH[] = "FLEX01_…"`), `src/flex/contract.json` `{contractHash}`; checked against the layer annotations
`dev.flex.releaseName`, `dev.flex.contractHash` and the configured board (FE-APL-ARD-*, FE-APL-005…007).

**Router RPC** (health, default): MessagePack-RPC over the Unix socket `/var/run/arduino-router.sock`; request
`[0, msgid, method, params]`, response `[1, msgid, error, result]`, notify `[2, method, params]`. Methods:
`flex.status.mcu` → `[contractHash, verified, enabled]`, `flex.status.mpu` → `[contractHash, verified]`. Healthy =
app `running` and both report the descriptor's contract hash and verified (and enabled on the MCU side) [V].
Stand-in: P4 fake CLI + simboard (`probes/p4-applab-target`).

## 3. PLC verify (flexBridge-PLC) — `drivers/plc/verify.py`

Nothing is downloaded; the driver reads the running interface hash and compares it with `flex-plc.json.ifaceHash`
(= descriptor `interfaceHash`). `flex-plc.json` (in the payload tar.gz): `{target, via: ads|modbus|shm, ifaceHash,
instance?, amsPort?, bridge?, carriers?, hello?{read[], write[]}, unitId?, port?, registers?{table, address, count}}` [V].

| via | Read | Hash bytes |
|---|---|---|
| ads (CX, TwinCAT) | pyads: handle of `<instance>#Hello` (default `MAIN.fbBridge#Hello`, port 851) via `ADSIGRP_SYM_HNDBYNAME 0xF003`, then `read_write` on `ADSIGRP_SYM_VALBYHND 0xF005` with an all-zero write buffer (default 36 B: aGpHash[32] + nGpEpoch u32) | read buffer (default) `ret u16 @0` (flexmodel.bridge STATUS index; BUSY = retry), `aRtHash[32] @2`, `nRtEpoch u32 @34`; layout from `hello.read` if given. The zero GP hash never matches, so the probe changes no FB state |
| modbus (Opta) | pymodbus `read_holding_registers(address 256, count 16, unit 1)`, port 502 | 16 × u16 big-endian = 32 bytes |
| shm (P10 sim RT host) | first 96 B of `/dev/shm/<bridge>` | magic u32 LE `0x31425846` ("FXB1") at 0, hash = bytes 64…96 |

Result `FLEX01_<hex>`. Different hash → fail-fast (FE-PLC-010); unreachable → "not healthy yet". Stand-ins:
`demo/edge` `sim.ads_plc`, `sim.modbus_plc`, `demo/sil/standins/cx_plc.py` (D1); CLI: `flexengine plc-read`.

## 4. MCU relay (SMP ⇄ Zenoh) — `gateway/relay.py`

[V] from code; the relay runs in the gateway's engine process and shares the SMP link.

- **MCU → Zenoh**: poll `flexmod status` at ≤ 10 Hz; for every socket with `running` and an advanced `pubs` counter,
  decode `last_msg` (XCDR2, ≤ 64 B) as the type whose topic id = `last_topic`, re-encode XCDR1, publish on the
  rmw_zenoh key of that port with encoding `application/cdr;<pkg/Type>`. Only the last message per poll is seen.
- **Topic id** = little-endian u32 of the first 4 bytes of the type's FLEX01 hash
  (`flexmodel.codegen.nodes.mcu_module.topic_id`).
- **Key layout** = `flexmodel.ros2.node_mapping` (rmw_zenoh_cpp): `<domain>/<node>/<topic>/<dds type>/RIHS01_<hash>`,
  e.g. `0/gripper_station/state/uc2::msg::dds_::GripperState_/RIHS01_<hash>`; liveliness tokens as rmw_zenoh expects;
  node ids `100 + 10·generation + n`.
- **Zenoh → MCU**: subscriptions on the input ports' keys and ROS services for rpc server ports; a sample is
  transcoded to XCDR2 and sent with `flexmod inject {topic, data}`; a service request is answered with a receipt
  `{seq, accepted = delivered > 0, reason "relayed" | "not delivered"}` (fields present in the response type).
- **Model generations**: a changed model adds a mapping generation; older generations keep serving topics the new
  one does not know until the new module runs.
Stand-in: QEMU base image + `demo/interop/router.py` (SIL).

# Plan: flexEngine 2 on Windows (host test machine)

*Status: plan, nothing implemented. Written 2026-10-27 against 2.0.0rc1 (`b55ccdd`).*

**Goal.** A Windows 10/11 x64 PC can do what a Mac does in [macos.md](macos.md): be a **host test machine** that runs
`flexengine` as a device host with native (`process`) and container (Docker Desktop) deployments, with or without an
MCU child over a serial port, with or without the MCU ⇄ Zenoh relay. Production devices stay Linux. The
connection points, the wire format and the reconciler do not change. Platform differences go into the local API
transport, the identity file checks and the `process` driver, the same way the macOS differences did (`ps` in place
of `/proc`, the sun_path limit).

**Not in scope:** `native` with `runtime: systemd`, `os-rauc`, `applab` on the board itself (it is a Linux board),
and the Linux service install (`packaging/install.sh`). ADS (`pyads`) is checked in W0 and only kept if it works
without a source build.

## 1. What stops it today

Measured on the 2.0.0rc1 sources and the PyPI index (wheels for `win_amd64`, Python 3.12):

| # | Where | What happens on Windows | Size |
|---|---|---|---|
| B1 | `local/api.py:35` `import grp` at module level | `ImportError`: `flexengine run` (it serves the local API) and every CLI command that uses the API fail at import | small |
| B2 | local API transport: `AF_UNIX` (`local/api.py`, CLI client), `os.chown` / `grp` for `local.group` | CPython on Windows has no `socket.AF_UNIX`. The read-only `local.tcp` bind exists, but the write endpoints (`POST /v1/reconcile`, `POST /v1/bundle`) and the CLI need a local transport | medium, **decision D-W1** |
| B3 | `identity/keys.py:29-48` (owner and mode checks of the device key and its directory), `local/doctor.py:98-102,172`, `config/load.py:410` (token mode) | `os.geteuid` does not exist (`AttributeError` at the first key load, so no engine starts). `st_mode` shows only the read-only bit, so the "0600, not group/other accessible" rule cannot be checked; `os.chmod(0o600)` does not restrict access | medium, **decision D-W2** |
| B4 | `drivers/process.py:179,203,242-261`, `drivers/buildstep.py:42,64` | `start_new_session`, `os.killpg`, `SIGKILL`, `os.getuid` are POSIX. Re-adoption after an engine restart uses `/proc` or `ps` | medium |
| B5 | 26 `read_text()` / `write_text()` / `open()` calls without `encoding=` (mostly `config/load.py`, `config/refdoc.py`, `local/cli.py`, `obs/otel.py`, `identity/crypto.py`) | the default encoding is cp1252, not UTF-8: a config or descriptor with non-ASCII text reads wrong or fails | small, mechanical |
| B6 | `util.atomic_write_bytes` (`os.replace`) | `os.replace` fails with `PermissionError` while another process (antivirus, indexer, the CLI) has the target open | small (bounded retry) |
| B7 | `drivers/container.py:574` `os.geteuid()` in the namespace HTTP probe | `AttributeError` when a container has no reachable IP. Docker Desktop on Windows, as on macOS, does not route container IPs: health type `http` does not work, `exec` / `process` do | small (guard), same docs rule as macOS |
| B8 | MCU link: socat bridge (`mcu.smp {host, port}`) | no socat on Windows. The serial form `mcu.smp.tty` (R5, pyserial, `COM3`) is needed | medium (also removes socat on macOS and Linux) |
| B9 | signals: `host.py:396`, `core/reconciler.py:1487` (SIGTERM / SIGINT) | Ctrl+C works. A service manager stops a process with a console control event or `TerminateProcess`, not SIGTERM, so the graceful stop (finish the running gate, save state) needs a Windows stop hook | small |
| B10 | dependencies | no `win_amd64` wheel for `crcmod` (the `mcu` extra, through `smp`) or `pyads==3.5.0`. Everything else has wheels: cryptography, PyYAML, jsonschema, smpclient, cbor2, pyelftools, msgpack, pymodbus, eclipse-zenoh, OpenTelemetry | small |
| B11 | tooling: `run_tests.sh` (bash), `hwtest/` runner, `packaging/install.sh` | no CI on Windows. The suite assumes bash, `/tmp`-style paths and POSIX fakes (`tests/contract/spi_harness/fakes`) | medium |

Paths are not a blocker: the engine takes every path from `engine.yaml`, and `${VAR}` expansion works with
`%USERPROFILE%`-style variables written as `${USERPROFILE}`. The defaults that name `/etc/flexengine` and
`/run/flexengine` only apply when no config path is given.

## 2. Decisions to take

| Id | Question | Options | Proposal |
|---|---|---|---|
| D-W1 | local API transport on Windows (B2) | (a) named pipe `\\.\pipe\flexengine-<dataDir hash>` with an owner-only DACL; (b) loopback TCP with a write token file next to the read-only one; (c) AF_UNIX through a third-party shim | **(b) for W2**: it reuses the existing `local.tcp` code and token check, so it is an additive CP-7 change (new optional key, e.g. `local.tcp.writeTokenFile`). Named pipes later if a security review asks for them |
| D-W2 | file-permission rule for keys and tokens (B3) | (a) check the DACL (`pywin32` / `ctypes` `GetNamedSecurityInfo`): owner-only or SYSTEM/Administrators; (b) trust the user profile directory and skip the check on Windows, with a doctor warning | **(a)**: the same promise as on POSIX ("only the engine's user can read the key"). `ctypes` avoids a `pywin32` dependency. `fixPermissions` writes an owner-only DACL |
| D-W3 | how the engine runs in the background | (a) Task Scheduler at logon (no extra binary); (b) a Windows service through WinSW or NSSM; (c) a `pywin32` service class | **(a) for the test host**, the equivalent of the macOS launchd agent; (b) only if a lab PC must run the engine without a login |
| D-W4 | supported Python and how to install | PyPI wheel (pure Python, already `py3-none-any`) plus `--prefer-binary`; or an offline bundle like Linux | **PyPI / checkout only**, Python 3.11 to 3.13 from python.org. No bundle until a Windows device is a target |
| D-W5 | is Windows CI required (in `ci-ok`)? | yes from W1, or only once W4 is green | **non-required job from W0, required from W4** |

## 3. Phases

Each phase is one `fe-<n>/windows-<topic>` branch and PR, with a CHANGES.md line. Within 2.x every contract
change is additive and gets a row in "Contract changes since the freeze".

| Phase | Content | Done when |
|---|---|---|
| **W0 measure** | CI job `unit (windows-2022, py3.13)` with `continue-on-error`, the suite run through `python -m pytest` (not bash) on the unit layer; record which tests fail and why. Check `crcmod` (pure-Python fallback without MSVC?) and `pyads` (with the TwinCAT ADS DLL) installs | a list of failing tests, each mapped to B1 to B11 or a new item |
| **W1 import and CLI** | B1 (lazy `grp`), B5 (`encoding="utf-8"` everywhere, plus a ruff rule or a test that greps for it), B7 guard, `os.geteuid` guards. `flexengine version`, `config check`, `doctor --offline` work | the unit layer passes on Windows except the tests of B2 to B4, which are marked `posix` and skip with a reason |
| **W2 local API** | B2 per D-W1, the CLI picks the transport per platform; the CP-7 spec and freeze baseline get the additive key | `flexengine run` + `status` / `deployments` / `logs` / `bundle import` / `POST /v1/reconcile` work on Windows; the `api` layer passes there |
| **W3 identity and files** | B3 per D-W2, B6 retry in `atomic_write_bytes`; `doctor` reports the DACL; FE-ONB / key reason texts stay the same | the `security` layer passes on Windows; a key readable by other users is refused, as on Linux |
| **W4 process driver** | B4: Job Objects (`CREATE_NEW_PROCESS_GROUP`, terminate the job tree) and re-adoption by pid + creation time (`ctypes` `GetProcessTimes`), the same contract as the `ps` path of macOS. `buildstep` the same | the `contract` layer's `process` cases and the crash-resume unit tests pass on Windows; Windows CI becomes required (D-W5) |
| **W5 serial MCU link** | B8: `mcu.smp.tty` (R5) with pyserial, `COM3` and `/dev/cu.*` / `/dev/ttyUSB0`; the TCP form stays. Helps all three platforms | an MCU child on `tty:` passes the MCU contract cases against QEMU's pty and the desk runbook EV-HW-04 on a Windows PC |
| **W6 run and document** | B9 stop hook (console control handler calls the same shutdown as SIGTERM), D-W3 Task Scheduler recipe, `docs/windows.md` with setups A, B, C like macos.md (and its doc test), README / CHANGES. The relay (setup C) needs only `flexmodel` and `eclipse-zenoh`, which have Windows wheels | the smoke test of the guide runs on a Windows 11 PC and the bench owner runs setups A and B on the desk |

Rough size: W1 and W3 are a few days each; W2 and W4 about a week each; W5 depends on R5 and is shared with the
MCU work; W6 is a few days plus a bench session.

## 4. Risks

- **Docker Desktop health checks:** as on macOS, container releases for these hosts must use `exec` or `process`
  health. If teams want `http`, the container driver needs a probe through `docker exec` (a driver change that
  would help macOS too).
- **Antivirus and file locks** around the artefact store (many small writes and renames): B6 covers renames; W0
  should run the suite with Defender on to see what else breaks.
- **Long paths:** the artefact store nests digests; keep `dataDir` short (e.g. `C:\flex\data`) or enable long paths.
  W1 adds a `config check` warning for `dataDir` longer than about 100 characters on Windows.
- **The relay and `flexmodel`** have not been tried on Windows; `flexmodel`'s code generation runs at relay start.
  W0 should import it and generate the UC1 types once.

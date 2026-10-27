# flexEngine 2 on macOS: a host test machine

This guide sets up a Mac (Apple Silicon or Intel, macOS 13 or later) as a **host test machine**: it runs `flexengine`
as a device host, takes signed desired states from flexCloud (or the local stub), and installs releases on the Mac
itself. An MCU board (Portenta H7 and the other flexEngine-MCU boards) can hang off the Mac as a child device over a
USB-UART adapter, with or without the MCU ⇄ Zenoh relay.

Pick the setup you need; §1 to §3 apply to all three:

| Setup | What the Mac runs | Extras | Section |
|---|---|---|---|
| **A. Host without MCU** | native (`process`) and container (Docker Desktop) deployments | none (add `otel`, `applab` as needed) | [§4](#4-setup-a-host-without-mcu) |
| **B. Host + MCU, no relay** | A, plus an MCU child over SMP (`mcu-module`, `mcu-firmware`) | `mcu` | [§5](#5-setup-b-host-with-an-mcu-child-no-relay) |
| **C. Host + MCU + relay** | B, plus the MCU ⇄ Zenoh relay (MCU topics on Zenoh / ROS 2) | `mcu,relay` + `flexmodel` | [§6](#6-setup-c-host-with-an-mcu-child-and-the-relay) |

The Mac is a test and development host, not a production device: there is no service user, no systemd and no
`packaging/install.sh` service install (that is the Linux bundle, [packaging/README.md](../packaging/README.md)). For
running in the background, §8 sets up a launchd agent. Windows is not supported yet; the plan is in
[windows-plan.md](windows-plan.md).

## 0. What works on macOS

| Part | macOS | Notes |
|---|---|---|
| Engine, reconciler, local API, CLI | yes | Unix socket `<dataDir>/api.sock` (no `/run/flexengine` on a Mac). The CI job `unit (macos-14, py3.13)` runs the unit tests on every PR |
| `native`, `runtime: process` | yes | the native runtime of a Mac; workloads run as your user |
| `native`, `runtime: systemd` | no | no systemd on macOS |
| `container` (`docker`, `podman`, `compose`) | yes, with limits | Docker Desktop, OrbStack, Colima or a Podman machine. **Health type `http` does not work**: the engine probes the container's IP, which these VM-based runtimes do not route to the Mac. Releases for a Mac host use health type `exec` or `process` (compose: the services' own healthchecks) |
| `mcu-module`, `mcu-firmware` (MCU child) | yes | over SMP on TCP: a socat bridge to the USB-UART (§5) or QEMU's serial socket. The serial form `mcu.smp.tty` is not implemented yet (R5) |
| MCU ⇄ Zenoh relay | yes | needs `flexmodel` from the framework repository (not on PyPI) and the `relay` extra (§6) |
| `applab` over SSH | expected to work | `ssh` from the Mac to the UNO Q; not covered by a macOS CI job |
| `arduino-ota` | expected to work | needs the MQTT broker and Redis of the bench; not covered by a macOS CI job |
| `plc-verify` | Modbus only | `pyads` (ADS) has no macOS wheel and is not supported on a Mac; Modbus needs `pip install pymodbus==3.11.3` (do not install the whole `plc` extra) |
| `os-rauc` | no | Linux A/B OS update only |

## 1. Prerequisites

- **Python 3.11, 3.12 or 3.13.** The `/usr/bin/python3` of the Xcode Command Line Tools is 3.9, too old. Use
  Homebrew (`brew install python@3.13`) or the python.org installer. Check: `python3.13 --version`.
- **Xcode Command Line Tools** (`xcode-select --install`) for setups B and C: the `mcu` extra pulls `crcmod`, which
  has no wheel and is built at install. On an Intel Mac `cbor2` is built too.
- **Docker Desktop** (or OrbStack, Colima, Podman) only if you run container deployments. The engine calls the
  `docker` / `podman` CLI, which must be on `PATH`.
- **socat** for setups B and C with a real board: `brew install socat`.
- A **flexCloud** to connect to: its URL, its CA file for `https://`, the cloud's public key (`cloudPublicKey`), the
  release signing key(s) (`releasePublicKeys`), and a one-time onboarding token per device. Without a flexCloud, §7
  runs the whole flow against the local stub.

## 2. Install

Everything lives in one directory, `~/flexengine` in this guide:

| Path | What |
|---|---|
| `~/flexengine/venv` | the engine's virtual environment |
| `~/flexengine/engine.yaml` | the configuration |
| `~/flexengine/tokens/` | onboarding tokens (directory 0700, files 0600; the engine deletes a token after onboarding) |
| `~/flexengine/data/` | `dataDir`: state, artefact store, device keys, `api.sock`. Never edit it by hand |
| `~/flexengine/logs/` | the engine log when it runs under launchd (§8) |

```bash
mkdir -p ~/flexengine/tokens ~/flexengine/logs && chmod 700 ~/flexengine/tokens
python3.13 -m venv ~/flexengine/venv
EXTRAS=""            # setup A; "mcu" for B; "mcu,relay" for C; add ",otel" or ",applab" as needed
```

**From PyPI** (once 2.0.0 is published; until then a plain `pip install flexEngine` gives 0.6.x, so always name the
version):

```bash
~/flexengine/venv/bin/pip install --prefer-binary "flexEngine[$EXTRAS]>=2.0.0"
```

**From a checkout** (works today, also for pre-releases and branches):

```bash
git clone https://github.com/BertVanAcker/flexEngine ~/src/flexEngine
~/flexengine/venv/bin/pip install --prefer-binary "$HOME/src/flexEngine[$EXTRAS]"
```

Then check it:

```bash
~/flexengine/venv/bin/flexengine version          # 2.0.0 (or the checkout's version)
```

`--prefer-binary` makes pip take a wheel over a newer source release. That matters on an Intel Mac, where the newest
`cryptography` has no Intel wheel (the last with a `universal2` wheel is 48.0.1). For the exact versions of the Linux
bundle on an Apple Silicon Mac, add `-c ~/src/flexEngine/packaging/requirements-lock.txt` (pip constraints). Do not
use the lock on an Intel Mac: it pins `cryptography` 50.0.2, which pip would then build from source.

Put the venv on your `PATH` and name the config once, so the commands below are short (add both lines to
`~/.zshrc`):

```bash
export PATH="$HOME/flexengine/venv/bin:$PATH"
export FLEXENGINE_CONFIG="$HOME/flexengine/engine.yaml"   # read by status, doctor, logs, enroll, ... (not by run)
```

## 3. The parts every setup shares

`engine.yaml` expands `${VAR}` from the environment, so `${HOME}` keeps the file free of your user name. `~` is
**not** expanded; write `${HOME}`. Unknown keys are errors; every key is in
[config-reference.md](config-reference.md).

- `cloud`: the flexCloud API and its keys, as on any device. `https://` needs `caFile` (or `allowSystemTrust: true`);
  plain `http://` needs `allowPlainHttp: true` (lab and local stub only).
- `dataDir: ${HOME}/flexengine/data`. The local API socket defaults to `<dataDir>/api.sock`. A Unix socket path may
  be at most 103 bytes on macOS; under `~/flexengine` that is never a problem. `config check` warns when it is.
- No `local.group`: the socket belongs to your user, and only you use the CLI.
- `devices[].drivers.native: {runtime: process}`: the native runtime on a Mac.

After every edit:

```bash
flexengine config check "$FLEXENGINE_CONFIG" --resolve     # --resolve also reads the token and key files
```

### Enroll and run (every setup)

1. Create a one-time onboarding token per device in flexCloud (`POST /api/v1/admin/onboarding-tokens`), one for the
   host and one per MCU child.
2. Store each token where the config expects it:

   ```bash
   flexengine enroll --token-file token-mac.txt --device mac-host
   flexengine enroll --token-file token-portenta.txt --device mac-host.portenta     # setups B and C
   ```

3. Run the engine in the foreground (Ctrl+C stops it cleanly):

   ```bash
   flexengine run --config "$FLEXENGINE_CONFIG"
   ```

   At its first start the engine creates the device keys (0600), onboards with the tokens and deletes them, sends
   its capabilities and polls the desired state every `pollInterval` seconds. The log goes to the terminal as JSON
   lines.

4. In a second terminal:

   ```bash
   flexengine status        # ONBOARDED yes, ONLINE yes per device
   flexengine doctor        # config, data dir, keys, local API, cloud, each executor
   ```

## 4. Setup A: host without MCU

```yaml
# engine.yaml: setup A, a Mac host with native and container deployments
cloud:
  apiUrl: https://flexcloud.lab:8700
  caFile: ${HOME}/flexengine/lab-ca.pem              # the only CA the engine trusts for apiUrl
  cloudPublicKey: <cloud-public-key>                 # desired-state signer (base64 Ed25519)
  releasePublicKeys: [<release-public-key>]          # release signer(s) (base64 Ed25519)
  pollInterval: 1.0
dataDir: ${HOME}/flexengine/data
devices:
  - deviceId: mac-host
    onboardingTokenFile: ${HOME}/flexengine/tokens/mac-host
    safeStateFile: ${HOME}/flexengine/data/safe-state.json
    drivers:
      native: {runtime: process}
      container:                                     # remove this block without Docker
        runtime: docker
        runtimes: [compose]
        allowedRunOptions:
          ports: ["8080"]
          volumes: [data]
```

- **Native releases** (payload with `app.py` or `unit.json`) run as processes of your user, each in its own
  session, like a systemd unit: they keep running when the engine stops, and the next start adopts them again (by pid
  and the start time `ps` reports). Stop or remove them with a plan (run state `stopped`, or leave the deployment out
  of the desired state), not by stopping the engine.
- **Container releases** need health type `exec` or `process` on a Mac (see §0). A release with the default `http`
  health check fails its health gate here (FE-REC-004, FE-HG-001) and is rolled back.
- **Safety-relevant releases** wait for the safe state. For tests on the desk, write it once:
  `echo '{"machine_parked": true, "operator_confirmed": true}' > ~/flexengine/data/safe-state.json`.
- For the desk runbooks' read-only API, add `local: {tcp: {bind: "127.0.0.1:8781", tokenFile:
  ${HOME}/flexengine/tokens/api-ro}}` (the token file holds a random string, mode 0600).

## 5. Setup B: host with an MCU child, no relay

The MCU board is a child device of the Mac: flexCloud sees it as its own device, and the Mac's engine talks to it over
SMP. Install with `EXTRAS=mcu` (§2).

### The serial link

Connect the board's SMP UART (on the Portenta H7: the HAT Carrier's 40-pin UART, uart4) to a USB-UART adapter. On a Mac
the adapter shows up as `/dev/cu.usbserial-*` (FTDI, CP210x) or `/dev/cu.wchusbserial*` (CH340); use the `cu.`
device, not `tty.`, which waits for carrier detect. The board's own USB port (`/dev/cu.usbmodem*`) is only the
console.

The engine reaches SMP over TCP (`mcu.smp {host, port}`). Until the serial form `mcu.smp.tty` lands (R5), socat
bridges the UART to `127.0.0.1:5555`. Start it before the engine and keep it running:

```bash
ls /dev/cu.*                                         # find the adapter
socat TCP-LISTEN:5555,bind=127.0.0.1,reuseaddr,fork FILE:/dev/cu.usbserial-XXXX,b115200,raw,echo=0
```

Only one SMP client may use the link at a time: stop the engine before you use `mcumgr` or a runbook's `smp_probe`
against the same port. Without a board, QEMU's `mps2/an385` with the flexEngine-MCU base image serves the same TCP
port (`-serial tcp:127.0.0.1:5555,server,nowait`); the image comes from the framework repository (probe P3).

### Config

The Mac becomes a `gateway`, and the board is a second device with `parent:` and `mcu:`:

```yaml
# engine.yaml: setup B, a Mac host with a Portenta child over SMP (no relay)
cloud:
  apiUrl: https://flexcloud.lab:8700
  caFile: ${HOME}/flexengine/lab-ca.pem
  cloudPublicKey: <cloud-public-key>
  releasePublicKeys: [<release-public-key>]
  pollInterval: 1.0
dataDir: ${HOME}/flexengine/data
devices:
  - deviceId: mac-host
    role: gateway
    onboardingTokenFile: ${HOME}/flexengine/tokens/mac-host
    safeStateFile: ${HOME}/flexengine/data/safe-state.json
    drivers:
      native: {runtime: process}
  - deviceId: mac-host.portenta                      # the MCU child, its own device in flexCloud
    parent: mac-host
    modelDevice: portenta
    onboardingTokenFile: ${HOME}/flexengine/tokens/mac-host.portenta
    mcu:
      smp: {host: 127.0.0.1, port: 5555}             # the socat bridge (or QEMU)
      modulePublicKey: <module-public-key>           # raw P-256 key (128 hex chars) that signs your FLXM modules
```

`modulePublicKey` must be a real P-256 public key (the X and Y coordinates, 64 bytes as hex): the engine checks
every module's signature against it before upload, and does not start with a value that is not a point on the curve.

Enroll both devices (§3) and start the engine. Checks:

- `flexengine status` lists `mac-host` (gateway) and `mac-host.portenta` (child-device), both onboarded.
- `flexengine doctor` has an `smp` line for the child: `ok` when the bridge and board answer, `FAIL ... Connection
  refused` when socat is not running.
- Without a board the engine still starts and onboards the child; it waits up to about 10 s for the SMP link at
  start, so the local API comes up a few seconds later than in setup A.

## 6. Setup C: host with an MCU child and the relay

The relay runs in the engine process next to the MCU child and shares its SMP link. It publishes the MCU modules'
messages on their rmw_zenoh keys (so `ros2 topic echo` works on the Mac) and injects Zenoh samples and ROS service
requests into the MCU's local bus.

### Install

1. Install the engine with `EXTRAS=mcu,relay` (§2). This adds the Zenoh Python binding (`eclipse-zenoh`, which has
   macOS wheels).
2. Install `flexmodel`. It is not on PyPI; it comes from the framework repository (decision FE-D3):

   ```bash
   git clone https://github.com/BertVanAcker/modular-interoperable-framework ~/src/modular-interoperable-framework
   ~/flexengine/venv/bin/pip install -e ~/src/modular-interoperable-framework/flexmodel
   ~/flexengine/venv/bin/python -c "import flexmodel, zenoh; print('relay deps ok')"
   ```

   Run that last check. `config check` does not see a missing `flexmodel`, and `flexengine run` then stops at start
   with `ModuleNotFoundError: No module named 'flexmodel'`.

3. A Zenoh endpoint for the relay to connect to. This is either the router of your ROS 2 setup (`ros2 run
   rmw_zenoh_cpp rmw_zenohd`) or a plain `zenohd`
   (`brew tap eclipse-zenoh/homebrew-zenoh && brew install zenoh`, then `zenohd`). Both listen on `tcp/…:7447` by
   default. Without a router, set `listen:` instead of `connect:` and let the subscribers connect to the relay.
4. The flexModel workspace the MCU modules were generated from, and its deployment id. The relay decodes the MCU
   messages with the model's types and maps them with the model's ROS 2 node mapping.

### Config

Setup B plus the model and a `relay` block on the MCU child:

```yaml
# engine.yaml: setup C, a Mac host with a Portenta child and the MCU <-> Zenoh relay
cloud:
  apiUrl: https://flexcloud.lab:8700
  caFile: ${HOME}/flexengine/lab-ca.pem
  cloudPublicKey: <cloud-public-key>
  releasePublicKeys: [<release-public-key>]
  pollInterval: 1.0
dataDir: ${HOME}/flexengine/data
model: ${HOME}/src/modular-interoperable-framework/examples/models/demo-pick   # flexModel workspace
deployment: d_pick_lab                               # deployment id in that model
devices:
  - deviceId: mac-host
    role: gateway
    onboardingTokenFile: ${HOME}/flexengine/tokens/mac-host
    safeStateFile: ${HOME}/flexengine/data/safe-state.json
    drivers:
      native: {runtime: process}
  - deviceId: mac-host.portenta
    parent: mac-host
    modelDevice: portenta                            # this board's device name in the model
    onboardingTokenFile: ${HOME}/flexengine/tokens/mac-host.portenta
    mcu:
      smp: {host: 127.0.0.1, port: 5555}
      modulePublicKey: <module-public-key>
    relay:
      enabled: true
      connect: tcp/127.0.0.1:7447                    # zenohd or rmw_zenohd; or listen: tcp/0.0.0.0:7447
      mode: peer
      maxRateHz: 10                                  # MCU -> Zenoh sampling rate (capped at 10 Hz)
```

Checks after the start: `flexengine status` and `doctor` as in setup B; `ros2 topic list` (or a Zenoh subscriber on `**`) shows
the MCU modules' topics once a module is deployed and publishing. The relay samples each module's last message at
`maxRateHz`, so a faster topic is seen at that rate.

Two commands talk to the relay path directly, without ROS (they use the SMP link, so stop the engine first):
`flexengine relay-status --smp 127.0.0.1:5555 --model ... --work ...` and `flexengine relay-inject ...` (see
`flexengine relay-inject --help`).

## 7. Smoke test without flexCloud

The reference flexCloud stub and the desk tool `hwtest/deskctl.py` run the whole flow on the Mac: onboarding, a
signed native release, a plan, the install and its health gate. You need the checkout from §2 (for `deskctl.py` and
the vendored UC1 model). Use a separate directory so the test does not touch your real config:

```bash
cd ~/src/flexEngine
T=~/flexengine-smoke && mkdir -p $T/tokens && chmod 700 $T/tokens
echo admin-secret > $T/admin.token && chmod 600 $T/admin.token
PY=~/flexengine/venv/bin/python

# 1. the flexCloud stub (plain HTTP on 127.0.0.1:8700); leave it running
$PY -m flexengine2.testing.fakecloud --data $T/cloud --port 8700 > $T/cloud.log 2>&1 &
sleep 2 && CLOUD_KEY=$(sed -n 's/.*cloud key //p' $T/cloud.log)

# 2. a signed native release (the UC1 stand-in app); the release key is created on the first publish
cat > $T/catalog.yaml <<EOF
model: $HOME/src/flexEngine/tests/vectors/framework/models/uc1
releaseKey: $T/release.key
work: $T/work
releases:
  dl_v1: {module: detection_logger, version: 1.0.0, driver: native, build: {kind: native}}
EOF
DESK="$PY hwtest/deskctl.py --cloud http://127.0.0.1:8700 --admin-token-file $T/admin.token --catalog $T/catalog.yaml"
$DESK publish dl_v1
RELEASE_KEY=$($PY -c "from flexengine2.identity import crypto; print(crypto.public_b64(crypto.load_private('$T/release.key')))")

# 3. the engine config, and the device's onboarding token
cat > $T/engine.yaml <<EOF
cloud:
  apiUrl: http://127.0.0.1:8700
  allowPlainHttp: true
  cloudPublicKey: $CLOUD_KEY
  releasePublicKeys: [$RELEASE_KEY]
  pollInterval: 1.0
dataDir: $T/data
devices:
  - deviceId: mac-smoke
    onboardingTokenFile: $T/tokens/mac-smoke
    drivers:
      native: {runtime: process}
EOF
$DESK token mac-smoke --out $T/tokens/mac-smoke
~/flexengine/venv/bin/flexengine config check $T/engine.yaml --resolve

# 4. the engine, then a plan that installs the release
~/flexengine/venv/bin/flexengine run --config $T/engine.yaml > $T/engine.log 2>&1 &
$DESK wait-device mac-smoke --capabilities
$DESK deploy mac-smoke logger=dl_v1
sleep 8 && ~/flexengine/venv/bin/flexengine status --config $T/engine.yaml    # mac-smoke: MANIFEST 1, 1 installed
```

Clean up: remove the deployment first (the native workload outlives the engine, §4), then stop the engine and the
stub:

```bash
$DESK deploy mac-smoke --remove logger && sleep 5
pkill -f "flexengine run --config $T/engine.yaml"; pkill -f flexengine2.testing.fakecloud
rm -rf ~/flexengine-smoke
```

## 8. Run in the background (launchd)

A launchd agent starts the engine at login and restarts it if it exits with an error. Paths in a plist must be
absolute and launchd starts agents with a minimal `PATH`, so the agent below is written with your `$HOME` filled in
and a `PATH` that finds Homebrew's and Docker's binaries:

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/dev.flex.flexengine.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.flex.flexengine</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/flexengine/venv/bin/flexengine</string>
    <string>run</string>
    <string>--config</string>
    <string>$HOME/flexengine/engine.yaml</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>40</integer>
  <key>StandardOutPath</key><string>$HOME/flexengine/logs/engine.log</string>
  <key>StandardErrorPath</key><string>$HOME/flexengine/logs/engine.log</string>
</dict>
</plist>
EOF
plutil -lint ~/Library/LaunchAgents/dev.flex.flexengine.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.flex.flexengine.plist
```

| To | Run |
|---|---|
| see whether it runs | `launchctl print gui/$(id -u)/dev.flex.flexengine \| grep -E 'state\|pid'`, `flexengine status` |
| follow the log | `tail -f ~/flexengine/logs/engine.log` |
| restart (after a config change) | `launchctl kickstart -k gui/$(id -u)/dev.flex.flexengine` |
| stop and remove the agent | `launchctl bootout gui/$(id -u)/dev.flex.flexengine` |

`ExitTimeOut` (40 s) is longer than the engine's `shutdownGrace` (30 s): on a stop, launchd sends SIGTERM, and a running
install or health gate may finish before the engine saves its state and exits. For setups B and C, run the socat
bridge the same way: a second agent (label `dev.flex.smp-bridge`) whose `ProgramArguments` are the socat command of
§5, with `KeepAlive` set to `true`.

The agent runs only while you are logged in. That is enough for a desk test machine. For a Mac that must run the
engine without a login, the same plist goes to `/Library/LaunchDaemons` (as root, with `UserName` set to your user).

## 9. Upgrade and uninstall

- **Upgrade:** `~/flexengine/venv/bin/pip install --prefer-binary --upgrade "flexEngine[$EXTRAS]>=2.0.0"` (or
  `git pull` in the checkout and the install command of §2 again), then restart the engine. Config, tokens and
  `data/` are kept. The engine continues from its accepted `manifestVersion`.
- **Uninstall:** stop the engine (and `launchctl bootout` the agent), then `rm -rf ~/flexengine/venv`. Remove
  `~/flexengine` too if the device is retired; the device keys in `data/` are its identity in flexCloud.

## 10. Troubleshooting on a Mac

| Symptom | Cause | Fix |
|---|---|---|
| `pip` builds `cryptography` (Rust) or fails on it | Intel Mac with the lock file, or without `--prefer-binary` | drop `-c requirements-lock.txt`, add `--prefer-binary` |
| `pip` fails on `crcmod` or `cbor2` | no compiler for the source build | `xcode-select --install`, then install again |
| `requires-python >=3.11` / pip finds only 0.6.x | the system Python 3.9, or no version named | use `python3.13 -m venv`; name `>=2.0.0` |
| container deployment `Failed`, FE-REC-004 / FE-HG-001 timeout | `http` health check against a container IP the Mac cannot reach | release the image with health type `exec` or `process` |
| container deployment fails with `docker: executable not found` in `failure` (`flexengine deployments`) | under launchd, `PATH` without Docker's binaries | add the directory of `which docker` to the agent's `PATH` |
| `doctor`: `smp ... Connection refused` | socat bridge not running, or the wrong `/dev/cu.*` | start socat (§5); unplug and plug the adapter and check `ls /dev/cu.*` |
| SMP timeouts with socat running | the UART baud rate or wiring, or a second SMP client on the port | `b115200` must match the board; one client at a time |
| `ModuleNotFoundError: No module named 'flexmodel'` at start | setup C without `flexmodel` in the venv | §6 step 2 |
| engine log `FE-LOC-001`, CLI says `offline` | the socket path is longer than 103 bytes (a long `dataDir`) | set `local.socket` to a shorter path, e.g. `${HOME}/flexengine/api.sock` |
| under launchd, connections to LAN hosts fail with `No route to host` while they work from Terminal | macOS 15 Local Network privacy | allow the Python binary in System Settings › Privacy & Security › Local Network, or run the engine from Terminal |
| key or token refused: "group/other accessible" | a file copied with the wrong mode | `chmod 600` the file, `chmod 700` its directory |

The general reason codes and what to do about them are in the [README, §8](../README.md#8-troubleshooting-by-reason-code).

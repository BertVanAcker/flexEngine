# Desk runbooks for flexEngineV2 (EV-14, EV-15)

The executable procedures for the engine on the Tier-1 hardware (plan §8): YAML runbooks in the flexNode-arduino
format, run by the project lead's runner and browser GUI, with the flex step types of `flexsteps.py`. The web-page
runbooks (`docs/lab/runbooks.json`) stay the record of acceptance: an acceptance item gets the path of the run's
`hwtest-results/<date>_<id>/result.json`.

| Runbook | Hardware | Simulated desk of the dry run (`simdesk.py` profile) |
|---|---|---|
| EV-HW-01 install + enrol | LattePanda | `install`: `packaging/install.sh --root` with a wheel of this checkout + the offline wheelhouse |
| EV-HW-02 containers | LattePanda (+ iGPU) | `lp`: Docker and Podman devices, `/dev/zero` as the mapped device |
| EV-HW-03 offline + bundle | LattePanda | `lp`: NetShim uplink cut, bundle "USB stick" directory |
| EV-HW-04 Portenta modules | LattePanda + Portenta H7 | `mcu`: QEMU mps2/an385 MCUboot + base image, relay with a Zenoh endpoint |
| EV-HW-05 MCU base image | LattePanda + Portenta H7 | `mcu`: QEMU power cut during the test boot |
| EV-HW-06 UNO Q App Lab | UNO Q (+ LattePanda) | `applab`: P4 board simulator + fake `arduino-app-cli`, remote (fake ssh) and board-local devices |
| EV-HW-07 plc-verify | LattePanda + Opta + CX | `plc`: pyads `Hello` and pymodbus stand-ins of `demo/edge/sim` |
| EV-HW-08 security | LattePanda | `lp`: tampered blob in the stub's registry, rogue CA |
| EV-HW-09 crash + power | LattePanda | `lp`: supervised engine (Restart=on-failure), power cut kills engine and workloads |
| EV-HW-10 observability | LattePanda (+ Mac) | `lp`: Jaeger query API over the engine's span file |
| EV-HW-11 RAUC A/B | LattePanda eMMC | `rauc`: fake `rauc` + boot simulator, the machine reboots when asked |
| EV-HW-12 four buttons (FE-4) | LattePanda | `fe4`: one device with process + systemd and docker + docker compose; systemd, docker and compose are the contract suite's fake CLIs (`tests/contract/spi_harness/fakes`) on the engine's PATH, the vendored UC1 model (no framework checkout) |

The flexNode-arduino boards' own remote-OTA runbooks (QC-04, QC-05) live upstream with the library
(flexNode-arduino PR #16, `tests/hardware/runbooks/`); the engine side of them is the arduino-ota driver.

## Runner and GUI: pinned, fetched, unmodified

`upstream.lock.json` pins flexNode-arduino `684bc02` (`porting-strategy`: `tools/hwtest/runbook.py`) and `0bfb8c7`
(`claude/runbook-execution-gui-n1awvw`: `gui.py`, `hwgui/`; the GUI is not on `porting-strategy` yet and runs on the
runner above) with the sha256 of every file. `upstream.py ensure` fills `.upstream/` (git-ignored) from a local
checkout (`$FLEXNODE_ARDUINO_REPO`, read with `git archive`) or a shallow fetch of the two commits, and refuses any
file that differs from the pin. Nothing is vendored (the tools are LGPL-2.1, used as fetched). `run.py` and `gui.py`
import the pinned modules, register the flex step types into the runner's `ACTIONS` table (which the GUI uses too)
and call the upstream `main()` (plan §8.2 option (c), DL-7). Moving the pin: edit the commits, then
`python hwtest/upstream.py pin --repo <checkout>`.

```bash
python3 -m venv .venv-hwtest && .venv-hwtest/bin/pip install -r hwtest/requirements.txt
.venv-hwtest/bin/python hwtest/run.py --check-all                       # format check of every runbook
.venv-hwtest/bin/python hwtest/run.py hwtest/runbooks/EV-HW-02_container.yaml
.venv-hwtest/bin/python hwtest/gui.py                                   # http://127.0.0.1:8765
.venv-hwtest/bin/python hwtest/dryrun.py --all                          # every runbook vs. simulation
```

## EV-HW-12: the four buttons

Push, Run all, Stop all, Run one, Stop one as the control plane drives them: each press is a plan with the FE-4 keys
(`deskctl.py deploy ... --state-all staged --runtime DEP=RT --after DEP=A --build DEP=ID`, then `deskctl.py state
DEVICE running|stopped [DEP ...]`, which plans the current desired state again with another run state). The releases
are `fb_*` in `catalogs/lab-d2.yaml`: build kinds `compose` (the stand-in image in a compose project, built with
Docker on the runner PC) and `native-source` (a source payload the engine builds on the device; `fail: true` exits 2,
FE-BLD-001), next to the `container` / `native` kinds. The device needs `drivers.container: {runtime: docker,
runtimes: [compose]}`, `drivers.native: {runtime: systemd, runtimes: [process], unitDir: ...}` and its safe state set
(the container modules of UC1 are safety-relevant). Build events are device events, not deployment statuses, so the
runbook reads them from the engine log (`build.started` / `build.succeeded` / `build.failed`) and the FE-BLD code from
the device's `history.jsonl` (over ssh). In the dry run (`dryrun.py EV-HW-12`, ~80 s) the states, ordering, health
gates and events are the engine's own; what only the LattePanda shows is dockerd, docker compose and systemd.

## Bench facts the runbook defaults use

From `demo/lab/inventory.example.yaml`: LattePanda `lab@lattepanda.lab` (192.168.50.20; control plane, flexCloud
TLS 9700, link shim control 8691, zenohd 7447, Jaeger 127.0.0.1:16686), CX 192.168.50.30 (AMS port 851), Opta
192.168.50.40 (Modbus 502, hash registers 256..271), UNO Q `arduino@unoq.local`, Mac 192.168.50.10. The Portenta's
SMP runs on the HAT Carrier's 40-pin UART (uart4) through a USB-UART adapter: `/dev/ttyUSB0` on the LattePanda,
bridged by socat to 127.0.0.1:5555 (the engine's `mcu.smp {host, port}`; `{tty}` is not implemented yet);
`/dev/ttyACM0` is the USB CDC console only. Engine read-only API: `local.tcp` bind `:8781` with a token file
(DA-7: one per desk, `~/.flexhwtest/fe-api.token` on the runner). Releases: `catalogs/lab-d2.yaml` (RECORD the paths
of the lab keys, Portenta images and RAUC bundles).

## Step types

`http_expect`, `cmd_json_expect`, `fe_status_expect`, `fe_history_mark` / `fe_history_expect`, `zenoh_expect`,
`smp_probe`, `ads_probe`, `modbus_expect`, `ssh_run`, `journal_mark` / `journal_expect`, `power_cycle`, plus the
runner's `write_file` (extended: `json`, `mode`, `host`, `sudo`) and `manual` (in a dry run: its `sim:` command and
`sim_answer`). Parameters and matchers: docstring of `flexsteps.py`. Tests: `tests/hwtest` (plan §7.2
layer 9; the dry runs run when that directory is selected).

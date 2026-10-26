# EV-HW-06 UNO Q App Lab deploy, update and rollback (remote mode and board-local engine)

* started: 2026-10-17T18:21:53
* result: 30 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-06-qcbqn939/secrets/admin.token`
* app: `rb-unoq`
* app_local: `rb-unoq-board`
* appcli: `env ARDUINO_APP_CLI__DATA_DIR=/tmp/rb-06-qcbqn939/appcli ARDUINO_APP_CLI__APPS_DIR=/tmp/rb-06-qcbqn939/ArduinoApps FAKE_APPCLI_BOARD=unoq FAKE_APPCLI_ROUTER=/tmp/rb-06-qcbqn939/board/arduino-router.sock FAKE_APPCLI_UART=/tmp/rb-06-qcbqn939/board/ttyHS1 FAKE_APPCLI_SIM_DIR=/tmp/rb-06-qcbqn939/board/sim FAKE_APPCLI_PYTHON=/home/user/modular-interoperable-framework/probes/p4-applab-target/.venv/bin/python /home/user/modular-interoperable-framework/probes/p4-applab-target/tools/fakebin/arduino-app-cli`
* appcli_board: `env ARDUINO_APP_CLI__DATA_DIR=/tmp/rb-06-qcbqn939/appcli ARDUINO_APP_CLI__APPS_DIR=/tmp/rb-06-qcbqn939/ArduinoApps FAKE_APPCLI_BOARD=unoq FAKE_APPCLI_ROUTER=/tmp/rb-06-qcbqn939/board/arduino-router.sock FAKE_APPCLI_UART=/tmp/rb-06-qcbqn939/board/ttyHS1 FAKE_APPCLI_SIM_DIR=/tmp/rb-06-qcbqn939/board/sim FAKE_APPCLI_PYTHON=/home/user/modular-interoperable-framework/probes/p4-applab-target/.venv/bin/python /home/user/modular-interoperable-framework/probes/p4-applab-target/tools/fakebin/arduino-app-cli`
* catalog: `/tmp/rb-06-qcbqn939/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-06-qcbqn939/cloud/oci`
* cloud_port: `42801`
* cloud_url: `http://127.0.0.1:40477`
* dep: `greenhouse`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:40477 --ca '' --admin-token-file /tmp/rb-06-qcbqn939/secrets/admin.token --catalog /tmp/rb-06-qcbqn939/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-06-qcbqn939/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:52055`
* fe_api_board: `http://127.0.0.1:52055`
* fe_api_board_token_file: `/tmp/rb-06-qcbqn939/secrets/fe-api.token`
* fe_api_port: `52055`
* fe_api_token_file: `/tmp/rb-06-qcbqn939/secrets/fe-api.token`
* fe_config: `/tmp/rb-06-qcbqn939/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-06-qcbqn939/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* journal_source: `file:/tmp/rb-06-qcbqn939/lp/engine.log`
* lab_ca: `/tmp/rb-06-qcbqn939/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-06-qcbqn939 power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182153_EV-HW-06`
* rogue_ca: `/tmp/rb-06-qcbqn939/pki/rogue-ca.crt`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-06-qcbqn939`
* sim_dir: `/tmp/rb-06-qcbqn939`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-06-qcbqn939 svc`
* target: `sim`
* unoq: `local`
* unoq_board: `local`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-06-qcbqn939 uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-06-qcbqn939 uplink up`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:42801 reachable |
| engine-up | PASS | rb-unoq: manifestVersion 0, onboarded true |
| appcli-version | PASS | exit 0, 2 field(s) match |
| exclusive | PASS | GET http://127.0.0.1:52055/v1/capabilities/rb-unoq -> 200, 2 field(s) match |
| deploy-10 | PASS | exit 0 |
| v10-installed | PASS | rb-unoq/greenhouse: Installed 1.0.0, health healthy |
| v10-running | PASS | exit 0, 1 field(s) match |
| deploy-11 | PASS | exit 0 |
| v11-installed | PASS | rb-unoq/greenhouse: Installed 1.1.0, health healthy |
| v11-running | PASS | exit 0, 2 field(s) match |
| deploy-12 | PASS | exit 0 |
| v12-failed | PASS | rb-unoq/greenhouse: Failed 1.2.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast true |
| v12-detect | PASS | exit 0, 2 field(s) match |
| v11-back | PASS | exit 0, 1 field(s) match |
| deploy-13 | PASS | exit 0 |
| v13-refused | PASS | rb-unoq/greenhouse: Failed 1.3.0, reasonCode FE-REC-003, rolledBackTo 1.1.0, failFast true |
| v13-not-installed | PASS | exit 0, 1 field(s) match |
| two-apps | PASS | exit 0, 2 field(s) match |
| remove | PASS | exit 0 |
| removed | PASS | rb-unoq/greenhouse: absent  |
| board-empty | PASS | exit 0, 1 field(s) match |
| board-local-start | PASS | dry run: simulated (exit 0), done |
| local-up | PASS | rb-unoq-board: manifestVersion 0, onboarded true |
| local-11 | PASS | exit 0 |
| local-11-installed | PASS | rb-unoq-board/greenhouse: Installed 1.1.0, health healthy |
| local-12 | PASS | exit 0 |
| local-12-failed | PASS | rb-unoq-board/greenhouse: Failed 1.2.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast true |
| local-detect | PASS | exit 0, 1 field(s) match |
| local-remove | PASS | exit 0 |
| local-removed | PASS | exit 0, 1 field(s) match |

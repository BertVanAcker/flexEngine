# EV-HW-07 plc-verify on the Opta (Modbus) and the CX (ADS) with the hold gate

* started: 2026-10-17T18:22:19
* result: 28 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-07-qf5uec7r/secrets/admin.token`
* ads_local_net_id: `127.0.0.1.1.3`
* catalog: `/tmp/rb-07-qf5uec7r/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-07-qf5uec7r/cloud/oci`
* cloud_port: `37777`
* cloud_url: `http://127.0.0.1:43505`
* cx1: `FLEX01_28ebc981b006b003f74eebf40e4feef77cc4c71201fbcdd1f65232fd4789fb72`
* cx2: `FLEX01_f4574d6f05c5ad86322215f088f9b59b72d02b12a3768f24cdf5976313e360a6`
* cx_ip: `127.0.0.1`
* cx_net_id: `127.0.0.1.1.1`
* cx_port: `851`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:43505 --ca '' --admin-token-file /tmp/rb-07-qf5uec7r/secrets/admin.token --catalog /tmp/rb-07-qf5uec7r/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-07-qf5uec7r/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:50459`
* fe_api_port: `50459`
* fe_api_token_file: `/tmp/rb-07-qf5uec7r/secrets/fe-api.token`
* fe_config: `/tmp/rb-07-qf5uec7r/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-07-qf5uec7r/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* journal_source: `file:/tmp/rb-07-qf5uec7r/lp/engine.log`
* lab_ca: `/tmp/rb-07-qf5uec7r/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* operator: `dry-run`
* opta1: `FLEX01_005b80047412e05440a960da612a76b329162426fa3e77c280150c47906c00d1`
* opta_host: `127.0.0.1`
* opta_port: `42841`
* opta_unit: `1`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-07-qf5uec7r power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182219_EV-HW-07`
* rogue_ca: `/tmp/rb-07-qf5uec7r/pki/rogue-ca.crt`
* safe_state_file: `/tmp/rb-07-qf5uec7r/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-07-qf5uec7r`
* sim_dir: `/tmp/rb-07-qf5uec7r`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-07-qf5uec7r svc`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-07-qf5uec7r uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-07-qf5uec7r uplink up`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:37777 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| capabilities | PASS | GET http://127.0.0.1:50459/v1/capabilities/rb-panda -> 200, 3 field(s) match |
| publish | PASS | exit 0, 1 field(s) match; saved cx1=FLEX01_28ebc981b006b003f74eebf40e4feef77cc4c71201fbcdd1f65232fd4789fb72, opta1=FLEX01_005b80047412e05440a960da612a76b329162426fa3e77c280150c47906c00d1 |
| opta-hash | PASS | Modbus 127.0.0.1:42841 holding [256, 16] = FLEX01_005b80047412e05440a960da612a76b329162426fa3e77c280150c47906c00d1 |
| cx-hello | PASS | ADS 127.0.0.1.1.1:851: Hello FLEX01_28ebc981b006b003... (Hello -> HASH_MISMATCH) |
| deploy-v1 | PASS | exit 0 |
| cx-installed | PASS | rb-panda/cx-bridge: Installed 1.0.0, health healthy |
| opta-installed | PASS | rb-panda/opta-stack: Installed 1.0.0, health healthy |
| cell-installed | PASS | rb-panda/cell: Installed 1.0.0, health healthy |
| verify-only | PASS | exit 0, 1 field(s) match |
| publish-11 | PASS | exit 0, 1 field(s) match; saved cx2=FLEX01_f4574d6f05c5ad86322215f088f9b59b72d02b12a3768f24cdf5976313e360a6 |
| deploy-11 | PASS | exit 0 |
| old-program | PASS | dry run: simulated (cx runs the program with interface hash FLEX01_28ebc981b006b003f74eebf40e4feef77cc4c71201fbcdd1f65232fd4789fb72), answered yes |
| cx-failed | PASS | rb-panda/cx-bridge: Failed 1.1.0, reasonCode FE-REC-004, rolledBackTo 1.0.0, failFast true |
| cell-held | PASS | rb-panda/cell: Pending 1.1.0, reasonCode FE-GATE-003 |
| cell-not-touched | PASS | exit 0, 1 field(s) match |
| provision | PASS | dry run: simulated (cx runs the program with interface hash FLEX01_f4574d6f05c5ad86322215f088f9b59b72d02b12a3768f24cdf5976313e360a6), answered yes |
| cx-hello-11 | PASS | ADS 127.0.0.1.1.1:851: Hello FLEX01_f4574d6f05c5ad86... (Hello -> HASH_MISMATCH) |
| rerelease | PASS | exit 0, 1 field(s) match |
| deploy-111 | PASS | exit 0 |
| cx-111-installed | PASS | rb-panda/cx-bridge: Installed 1.1.1, health healthy |
| cell-released | PASS | rb-panda/cell: Installed 1.1.0, health healthy |
| opta-drift | PASS | dry run: simulated (opta runs the program with interface hash FLEX01_0000000000000000000000000000000000000000000000000000000000000000), done |
| opta-unhealthy | PASS | rb-panda/opta-stack: Installed 1.0.0, health unhealthy |
| opta-restore | PASS | dry run: simulated (opta runs the program with interface hash FLEX01_005b80047412e05440a960da612a76b329162426fa3e77c280150c47906c00d1), done |
| opta-healthy | PASS | rb-panda/opta-stack: Installed 1.0.0, health healthy |
| opta-hash-again | PASS | Modbus 127.0.0.1:42841 holding [256, 16] = FLEX01_005b80047412e05440a960da612a76b329162426fa3e77c280150c47906c00d1 |

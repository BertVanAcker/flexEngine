# EV-HW-12 Four buttons on the LattePanda (Push, Run, Stop, Run one, Stop all; on-device build)

* started: 2026-10-27T00:14:23
* result: 51 passed, 0 failed, 0 skipped
* project commit: `v0.6.0-95-gae54c3f-dirty`
* runner (flexNode-arduino) commit: `v0.6.0-95-gae54c3f-dirty`
* admin_token_file: `/tmp/rb-12-8qcal_qe/secrets/admin.token`
* catalog: `/tmp/rb-12-8qcal_qe/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-12-8qcal_qe/cloud/oci`
* cloud_port: `41461`
* cloud_url: `http://127.0.0.1:42635`
* desk: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/deskctl.py --cloud http://127.0.0.1:42635 --ca '' --admin-token-file /tmp/rb-12-8qcal_qe/secrets/admin.token --catalog /tmp/rb-12-8qcal_qe/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-12-8qcal_qe/lp/engine.yaml /tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:35199`
* fe_api_port: `35199`
* fe_api_token_file: `/tmp/rb-12-8qcal_qe/secrets/fe-api.token`
* fe_config: `/tmp/rb-12-8qcal_qe/lp/engine.yaml`
* fe_data: `/tmp/rb-12-8qcal_qe/lp/data`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-12-8qcal_qe/lp/engine.yaml /tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* fe_sudo: ``
* flexengine: `/home/user/flexengine-bench`
* gp: `rb-panda`
* hwtest: `/home/user/flexengine-bench/hwtest`
* journal_source: `file:/tmp/rb-12-8qcal_qe/lp/engine.log`
* lab_ca: `/tmp/rb-12-8qcal_qe/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* operator: `dry-run`
* power_cut: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/simdesk.py ctl --dir /tmp/rb-12-8qcal_qe power-cycle --down 3`
* project_dir: `/home/user/flexengine-bench`
* python: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python`
* repo: `/home/user/modular-interoperable-framework`
* results_dir: `/home/user/flexengine-bench/hwtest/hwtest-results/20261027-001423_EV-HW-12`
* rogue_ca: `/tmp/rb-12-8qcal_qe/pki/rogue-ca.crt`
* rt: `/tmp/rb-12-8qcal_qe/bin/docker`
* run_all_s: `2.305`
* safe_state_file: `/tmp/rb-12-8qcal_qe/lp/safe-state.json`
* sim: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/simdesk.py ctl --dir /tmp/rb-12-8qcal_qe`
* sim_dir: `/tmp/rb-12-8qcal_qe`
* svc: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/simdesk.py ctl --dir /tmp/rb-12-8qcal_qe svc`
* sysctl: `/tmp/rb-12-8qcal_qe/bin/systemctl`
* target: `sim`
* unit_dir: `/tmp/rb-12-8qcal_qe/fakert/units`
* uplink_down: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/simdesk.py ctl --dir /tmp/rb-12-8qcal_qe uplink down`
* uplink_up: `/tmp/claude-0/-home-user/86682e99-a3a9-5cb0-8965-3a04bdbf4e94/scratchpad/fe5venv/bin/python /home/user/flexengine-bench/hwtest/simdesk.py ctl --dir /tmp/rb-12-8qcal_qe uplink up`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:41461 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| capabilities | PASS | exit 0, 2 field(s) match |
| capabilities-compose | PASS | exit 0, 2 field(s) match |
| capabilities-native | PASS | exit 0, 1 field(s) match |
| publish | PASS | exit 0, 1 field(s) match |
| push | PASS | exit 0 |
| push-staged | PASS | exit 0, 8 field(s) match |
| push-ctr-idle | PASS | exit 0, 2 field(s) match |
| push-cam-idle | PASS | exit 0, 1 field(s) match |
| push-unit-idle | PASS | exit 0, matched 'ActiveState=inactive' |
| mark-run | PASS | mark run at deviceTime 1793060065.772 (8 record(s) before it) |
| run-all | PASS | exit 0, 1 field(s) match |
| run-all-installed | PASS | exit 0, 9 field(s) match |
| run-order | PASS | proc Installed 4.0.0 -> sysd Installed 4.0.0 -> ctr Installed 4.0.0 -> cam Installed 4.0.0; 2.31 s from the first to the last record |
| run-ctr | PASS | exit 0, 2 field(s) match |
| run-unit | PASS | exit 0, matched 'ActiveState=active' |
| mark-stop | PASS | mark stop at deviceTime 1793060070.356 (16 record(s) before it) |
| stop-all | PASS | exit 0, 1 field(s) match |
| stop-all-stopped | PASS | exit 0, 6 field(s) match |
| stop-order | PASS | cam Stopped 4.0.0 -> ctr Stopped 4.0.0 -> sysd Stopped 4.0.0 -> proc Stopped 4.0.0; 0.75 s from the first to the last record |
| kept-ctr | PASS | exit 0, 2 field(s) match |
| kept-cam | PASS | exit 0, 1 field(s) match |
| kept-unit | PASS | exit 0, matched 'ActiveState=inactive' |
| stays-stopped | PASS | waited 10s |
| still-stopped | PASS | rb-panda/ctr: Stopped 4.0.0 |
| run-one | PASS | exit 0, 2 field(s) match |
| run-one-only | PASS | exit 0, 5 field(s) match |
| stop-one | PASS | exit 0, 1 field(s) match |
| stop-one-stopped | PASS | rb-panda/ctr: Stopped 4.0.0 |
| gate-push | PASS | exit 0 |
| gate-staged | PASS | exit 0, 2 field(s) match |
| gate-run | PASS | exit 0, 1 field(s) match |
| gate-failed | PASS | rb-panda/gate: Failed 4.1.0, reasonCode FE-REC-004, failFast false |
| gate-others | PASS | exit 0, 3 field(s) match |
| mark-build | PASS | mark build at byte 5862 of /tmp/rb-12-8qcal_qe/lp/engine.log |
| build-push | PASS | exit 0 |
| build-ok | PASS | 1 line(s) matching {'event': 'build.succeeded', 'dep': 'bld', 'buildId': 'b-1'}: {"ts":"2026-10-27T00:14:58.029Z","level":"info","device":"rb-panda","event":"build.succeeded","dep":"bld","msg":"BuildSucceeded bld b-1","buildId":"b-1"} |
| build-staged | PASS | rb-panda/bld: Staged 4.2.0 |
| build-run | PASS | exit 0, 1 field(s) match |
| build-installed | PASS | rb-panda/bld: Installed 4.2.0, health healthy |
| mark-build-bad | PASS | mark buildbad at byte 7746 of /tmp/rb-12-8qcal_qe/lp/engine.log |
| build-bad | PASS | exit 0 |
| build-failed | PASS | 1 line(s) matching exited with status 2: {"ts":"2026-10-27T00:15:01.788Z","level":"warning","device":"rb-panda","event":"build.failed","dep":"bld","msg":"BuildFailed bld b-2: build failed: build command exited with status 2","buildId":"b-2"} |
| build-failed-code | PASS | exit 0, 2 field(s) match |
| build-kept | PASS | exit 0, 3 field(s) match |
| build-unit | PASS | exit 0, matched 'ActiveState=active' |
| remove | PASS | exit 0 |
| removed | PASS | exit 0, 5 field(s) match |
| no-container | PASS | exit 0, matched '0\n' |
| no-unit | PASS | exit 0, matched '0\n' |

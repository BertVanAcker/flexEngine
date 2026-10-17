# EV-HW-05 Portenta H7 full base image update through MCUboot

* started: 2026-10-17T18:19:27
* result: 27 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-05-61rqnvb3/secrets/admin.token`
* base_version: `1.0.0`
* catalog: `/tmp/rb-05-61rqnvb3/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-05-61rqnvb3/cloud/oci`
* cloud_port: `36501`
* cloud_url: `http://127.0.0.1:44597`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:44597 --ca '' --admin-token-file /tmp/rb-05-61rqnvb3/secrets/admin.token --catalog /tmp/rb-05-61rqnvb3/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-05-61rqnvb3/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:59095`
* fe_api_port: `59095`
* fe_api_token_file: `/tmp/rb-05-61rqnvb3/secrets/fe-api.token`
* fe_config: `/tmp/rb-05-61rqnvb3/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-05-61rqnvb3/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* fw_update_s: `17.600`
* gp: `rb-panda`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* journal_source: `file:/tmp/rb-05-61rqnvb3/lp/engine.log`
* lab_ca: `/tmp/rb-05-61rqnvb3/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* mcu: `rb-panda.portenta`
* model_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/examples/models/demo-pick`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-05-61rqnvb3 power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-181927_EV-HW-05`
* rogue_ca: `/tmp/rb-05-61rqnvb3/pki/rogue-ca.crt`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-05-61rqnvb3`
* sim_dir: `/tmp/rb-05-61rqnvb3`
* smp_host: `127.0.0.1`
* smp_port: `38663`
* smp_tty: ``
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-05-61rqnvb3 svc`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-05-61rqnvb3 uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-05-61rqnvb3 uplink up`
* zenoh_connect: `tcp/127.0.0.1:46519`
* zenoh_mode: `peer`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:36501 reachable |
| child-up | PASS | rb-panda.portenta: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| modules | PASS | exit 0 |
| modules-installed | PASS | rb-panda.portenta/gripper: Installed 1.0.0, health healthy |
| engine-stop-1 | PASS | exit 0 |
| base-before | PASS | SMP flexmod_info on 127.0.0.1:38663: {"abi_major": 1, "abi_minor": 0, "arch": "armv7m", "base": "1.0.0", "board": "mps2/an385", "caps": 3, "confirmed": tr... |
| engine-start-1 | PASS | exit 0 |
| mark-11 | PASS | mark fw11 at deviceTime 1792261192.550 (4 record(s) before it) |
| deploy-11 | PASS | exit 0 |
| fw-11-installed | PASS | rb-panda.portenta/base-image: Installed 1.1.0, health healthy |
| fw-11-time | PASS | base-image Installing 1.1.0 -> base-image Installed 1.1.0; 17.60 s from the first to the last record |
| modules-survive | PASS | rb-panda.portenta/servo: Installed 2.0.0, health healthy |
| deploy-12 | PASS | exit 0 |
| fw-12-failed | PASS | rb-panda.portenta/base-image: Failed 1.2.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast false |
| engine-stop-2 | PASS | exit 0 |
| base-reverted | PASS | SMP flexmod_info on 127.0.0.1:38663: {"abi_major": 1, "abi_minor": 0, "arch": "armv7m", "base": "1.1.0", "board": "mps2/an385", "caps": 3, "confirmed": tr... |
| modules-after-revert | PASS | SMP flexmod_status on 127.0.0.1:38663: {"boots": 1, "sockets": [{"activations": 0, "active": 0, "auto_reverts": 0, "confirmed": 0, "dep": "servo", "errors":... |
| engine-start-2 | PASS | exit 0 |
| deploy-10 | PASS | exit 0 |
| power-cut | PASS | power cycled (simulated) |
| power-cut-result | PASS | rb-panda.portenta/base-image: Failed 1.0.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast false |
| engine-stop-3 | PASS | exit 0 |
| base-after-cut | PASS | SMP flexmod_info on 127.0.0.1:38663: {"abi_major": 1, "abi_minor": 0, "arch": "armv7m", "base": "1.1.0", "board": "mps2/an385", "caps": 3, "confirmed": tr... |
| engine-start-3 | PASS | exit 0 |
| back-to-11 | PASS | exit 0 |
| converged | PASS | exit 0, 2 field(s) match |

# EV-HW-02 Container deploy, update, rollback (Docker and Podman) on the LattePanda

* started: 2026-10-17T18:17:35
* result: 34 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-02-jxodt28w/secrets/admin.token`
* catalog: `/tmp/rb-02-jxodt28w/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-02-jxodt28w/cloud/oci`
* cloud_port: `40659`
* cloud_sh: `local`
* cloud_url: `http://127.0.0.1:42449`
* dep: `pg`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:42449 --ca '' --admin-token-file /tmp/rb-02-jxodt28w/secrets/admin.token --catalog /tmp/rb-02-jxodt28w/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-02-jxodt28w/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:50485`
* fe_api_port: `50485`
* fe_api_token_file: `/tmp/rb-02-jxodt28w/secrets/fe-api.token`
* fe_config: `/tmp/rb-02-jxodt28w/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-02-jxodt28w/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* gp_podman: `rb-panda-podman`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* igpu_device: `/dev/zero`
* jaeger_url: `http://127.0.0.1:35537`
* journal_source: `file:/tmp/rb-02-jxodt28w/lp/engine.log`
* lab_ca: `/tmp/rb-02-jxodt28w/pki/ca.crt`
* lab_ca_lp: `/tmp/rb-02-jxodt28w/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_up_host: `127.0.0.1`
* lp_up_port: `50485`
* lp_usb: `/tmp/rb-02-jxodt28w/usb`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-02-jxodt28w power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-181735_EV-HW-02`
* rogue_ca: `/tmp/rb-02-jxodt28w/pki/rogue-openssl-ca.crt`
* rollback_s: `9.851`
* rt: `docker`
* rt_podman: `podman`
* safe_state_file: `/tmp/rb-02-jxodt28w/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-02-jxodt28w`
* sim_dir: `/tmp/rb-02-jxodt28w`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-02-jxodt28w svc`
* tamper: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py tamper --layout /tmp/rb-02-jxodt28w/cloud/oci`
* target: `sim`
* update_s: `1.171`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-02-jxodt28w uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-02-jxodt28w uplink up`
* usb_dir: `/tmp/rb-02-jxodt28w/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:40659 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| engine-up-podman | PASS | rb-panda-podman: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| deploy-v1 | PASS | exit 0 |
| v1-installed | PASS | rb-panda/pg: Installed 1.0.0, health healthy |
| v1-running | PASS | exit 0, matched '1.0.0 true' |
| mark-update | PASS | mark update at deviceTime 1792261060.368 (2 record(s) before it) |
| deploy-v2 | PASS | exit 0 |
| v2-installed | PASS | rb-panda/pg: Installed 1.1.0, health healthy |
| update-time | PASS | pg Installing 1.1.0 -> pg Installed 1.1.0; 1.17 s from the first to the last record |
| mark-bad | PASS | mark bad at deviceTime 1792261062.781 (4 record(s) before it) |
| deploy-bad | PASS | exit 0 |
| bad-failed | PASS | rb-panda/pg: Failed 1.2.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast false |
| rollback-time | PASS | pg Installing 1.2.0 -> pg Failed 1.2.0; 9.85 s from the first to the last record |
| v2-running-after-rollback | PASS | exit 0, matched '1.1.0 true' |
| igpu-after-rollback | PASS | exit 0, 1 field(s) match |
| safe-state-off | PASS | wrote /tmp/rb-02-jxodt28w/lp/safe-state.json |
| deploy-safety | PASS | exit 0 |
| safety-pending | PASS | rb-panda/pg: Pending 1.3.0, reasonCode FE-GATE-001 |
| still-v2 | PASS | exit 0, matched '1.1.0 true' |
| safe-state-on | PASS | wrote /tmp/rb-02-jxodt28w/lp/safe-state.json |
| safety-installed | PASS | rb-panda/pg: Installed 1.3.0, health healthy |
| podman-v1 | PASS | exit 0 |
| podman-v1-installed | PASS | rb-panda-podman/pg: Installed 1.0.0, health healthy |
| podman-v2 | PASS | exit 0 |
| podman-v2-installed | PASS | rb-panda-podman/pg: Installed 1.1.0, health healthy |
| podman-bad | PASS | exit 0 |
| podman-bad-failed | PASS | rb-panda-podman/pg: Failed 1.2.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast false |
| podman-running | PASS | exit 0, matched '1.1.0 true' |
| remove | PASS | exit 0 |
| removed | PASS | rb-panda/pg: absent  |
| removed-podman | PASS | rb-panda-podman/pg: absent  |
| no-container | PASS | exit 0, matched '0' |

# EV-HW-09 Crash and power-cycle recovery on the LattePanda

* started: 2026-10-17T18:23:02
* result: 27 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-09-9zboypob/secrets/admin.token`
* catalog: `/tmp/rb-09-9zboypob/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-09-9zboypob/cloud/oci`
* cloud_port: `44197`
* cloud_sh: `local`
* cloud_url: `http://127.0.0.1:34229`
* dep: `pg`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:34229 --ca '' --admin-token-file /tmp/rb-09-9zboypob/secrets/admin.token --catalog /tmp/rb-09-9zboypob/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-09-9zboypob/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:44193`
* fe_api_port: `44193`
* fe_api_token_file: `/tmp/rb-09-9zboypob/secrets/fe-api.token`
* fe_config: `/tmp/rb-09-9zboypob/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-09-9zboypob/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* gp_podman: `rb-panda-podman`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* igpu_device: `/dev/zero`
* jaeger_url: `http://127.0.0.1:44735`
* journal_source: `file:/tmp/rb-09-9zboypob/lp/engine.log`
* lab_ca: `/tmp/rb-09-9zboypob/pki/ca.crt`
* lab_ca_lp: `/tmp/rb-09-9zboypob/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_up_host: `127.0.0.1`
* lp_up_port: `44193`
* lp_usb: `/tmp/rb-09-9zboypob/usb`
* mv_before: `2`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-09-9zboypob power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182302_EV-HW-09`
* rogue_ca: `/tmp/rb-09-9zboypob/pki/rogue-openssl-ca.crt`
* rt: `docker`
* rt_podman: `podman`
* safe_state_file: `/tmp/rb-09-9zboypob/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-09-9zboypob`
* sim_dir: `/tmp/rb-09-9zboypob`
* started: `2026-10-17T18:23:05.511171323Z`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-09-9zboypob svc`
* tamper: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py tamper --layout /tmp/rb-09-9zboypob/cloud/oci`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-09-9zboypob uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-09-9zboypob uplink up`
* usb_dir: `/tmp/rb-09-9zboypob/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:44197 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| deploy-v1 | PASS | exit 0 |
| v1-installed | PASS | rb-panda/pg: Installed 1.0.0, health healthy |
| container-started | PASS | exit 0, matched 'started=2026-10-17T18:23:05.511171323Z' |
| kill-steady | PASS | exit 0 |
| back-steady | PASS | rb-panda/pg: Installed 1.0.0, health healthy |
| same-container | PASS | exit 0, matched 'started=2026-10-17T18:23:05.511171323Z running=true' |
| no-duplicate | PASS | exit 0, 1 field(s) match |
| mark-gate | PASS | mark gate at deviceTime 1792261386.265 (2 record(s) before it) |
| deploy-v2 | PASS | exit 0 |
| in-gate | PASS | pg Installing 1.1.0; 0.00 s from the first to the last record |
| kill-gate | PASS | exit 0 |
| gate-resumed | PASS | rb-panda/pg: Installed 1.1.0, health healthy |
| gate-once | PASS | exit 0, 1 field(s) match |
| one-container | PASS | exit 0, matched '1' |
| state-before | PASS | rb-panda: manifestVersion 2, onboarded true; saved mv_before=2 |
| mark-power | PASS | mark power at deviceTime 1792261393.379 (5 record(s) before it) |
| deploy-v1-again | PASS | exit 0 |
| in-update | PASS | pg Installing 1.0.0; 0.00 s from the first to the last record |
| power-cut | PASS | power cycled (simulated); 127.0.0.1:44193 back after 4.9 s |
| after-boot | PASS | rb-panda/pg: Installed 1.0.0, health healthy |
| container-after-boot | PASS | exit 0, matched '1.0.0 true' |
| state-survives | PASS | rb-panda: manifestVersion 3, onboarded true |
| cleanup | PASS | exit 0 |
| removed | PASS | rb-panda/pg: absent  |

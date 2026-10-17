# EV-HW-11 RAUC A/B OS update with a health-gated commit on the LattePanda (alpha.2)

* started: 2026-10-17T18:23:33
* result: 25 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-11-mob5ac51/secrets/admin.token`
* catalog: `/tmp/rb-11-mob5ac51/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-11-mob5ac51/cloud/oci`
* cloud_port: `36321`
* cloud_url: `http://127.0.0.1:33789`
* dep: `os`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:33789 --ca '' --admin-token-file /tmp/rb-11-mob5ac51/secrets/admin.token --catalog /tmp/rb-11-mob5ac51/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-11-mob5ac51/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:34207`
* fe_api_port: `34207`
* fe_api_token_file: `/tmp/rb-11-mob5ac51/secrets/fe-api.token`
* fe_config: `/tmp/rb-11-mob5ac51/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-11-mob5ac51/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* journal_source: `file:/tmp/rb-11-mob5ac51/lp/engine.log`
* lab_ca: `/tmp/rb-11-mob5ac51/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* mv11: `1`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-11-mob5ac51 power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* rauc: `/tmp/rb-11-mob5ac51/ab/bin/rauc`
* rauc_host: `local`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182333_EV-HW-11`
* rogue_ca: `/tmp/rb-11-mob5ac51/pki/rogue-ca.crt`
* shared_dir: `/tmp/rb-11-mob5ac51/lp`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-11-mob5ac51`
* sim_dir: `/tmp/rb-11-mob5ac51`
* slot0: `A`
* slot11: `B`
* slot12: `A`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-11-mob5ac51 svc`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-11-mob5ac51 uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-11-mob5ac51 uplink up`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:36321 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| slot-before | PASS | exit 0, 1 field(s) match; saved slot0=A |
| deploy-11 | PASS | exit 0 |
| v11-installed | PASS | rb-panda/os: Installed 1.1.0, health healthy |
| v11-slot | PASS | exit 0, 3 field(s) match; saved slot11=B |
| v11-state | PASS | rb-panda: manifestVersion 1, onboarded true; saved mv11=1 |
| deploy-noboot | PASS | exit 0 |
| noboot-failed | PASS | rb-panda/os: Failed 2.0.0, reasonCode FE-REC-003, rolledBackTo 1.1.0, failFast true |
| noboot-slot | PASS | exit 0, 2 field(s) match |
| deploy-badhealth | PASS | exit 0 |
| badhealth-failed | PASS | rb-panda/os: Failed 2.1.0, reasonCode FE-REC-004, rolledBackTo 1.1.0, failFast false |
| badhealth-slot | PASS | exit 0, 3 field(s) match |
| prepare-cut-install | PASS | dry run: simulated (boot simulator: {'installDelay': 4}), done |
| deploy-12 | PASS | exit 0 |
| cut-install | PASS | power cycled (simulated) |
| v12-converged | PASS | rb-panda/os: Installed 1.2.0, health healthy |
| v12-slot | PASS | exit 0, 2 field(s) match; saved slot12=A |
| deploy-13 | PASS | exit 0 |
| cut-before-good | PASS | power cycled (simulated) |
| v13-converged | PASS | rb-panda/os: Installed 1.3.0, health healthy |
| v13-slot | PASS | exit 0, 3 field(s) match |
| state-survives | PASS | rb-panda: manifestVersion 5, onboarded true |
| cloud-converged | PASS | exit 0, 2 field(s) match |

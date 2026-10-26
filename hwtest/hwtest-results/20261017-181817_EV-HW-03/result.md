# EV-HW-03 Offline convergence and offline bundle on the LattePanda

* started: 2026-10-17T18:18:17
* result: 26 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-03-ifxpzyoy/secrets/admin.token`
* catalog: `/tmp/rb-03-ifxpzyoy/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-03-ifxpzyoy/cloud/oci`
* cloud_port: `33249`
* cloud_sh: `local`
* cloud_url: `http://127.0.0.1:45551`
* dep: `dl`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:45551 --ca '' --admin-token-file /tmp/rb-03-ifxpzyoy/secrets/admin.token --catalog /tmp/rb-03-ifxpzyoy/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-03-ifxpzyoy/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:34633`
* fe_api_port: `34633`
* fe_api_token_file: `/tmp/rb-03-ifxpzyoy/secrets/fe-api.token`
* fe_config: `/tmp/rb-03-ifxpzyoy/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-03-ifxpzyoy/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* gp_podman: `rb-panda-podman`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* igpu_device: `/dev/zero`
* jaeger_url: `http://127.0.0.1:38879`
* journal_source: `file:/tmp/rb-03-ifxpzyoy/lp/engine.log`
* lab_ca: `/tmp/rb-03-ifxpzyoy/pki/ca.crt`
* lab_ca_lp: `/tmp/rb-03-ifxpzyoy/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_up_host: `127.0.0.1`
* lp_up_port: `34633`
* lp_usb: `/tmp/rb-03-ifxpzyoy/usb`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-03-ifxpzyoy power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-181817_EV-HW-03`
* rogue_ca: `/tmp/rb-03-ifxpzyoy/pki/rogue-openssl-ca.crt`
* rt: `docker`
* rt_podman: `podman`
* safe_state_file: `/tmp/rb-03-ifxpzyoy/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-03-ifxpzyoy`
* sim_dir: `/tmp/rb-03-ifxpzyoy`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-03-ifxpzyoy svc`
* tamper: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py tamper --layout /tmp/rb-03-ifxpzyoy/cloud/oci`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-03-ifxpzyoy uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-03-ifxpzyoy uplink up`
* usb_dir: `/tmp/rb-03-ifxpzyoy/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:33249 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| deploy-v1 | PASS | exit 0 |
| v1-installed | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| uplink-down | PASS | exit 0 |
| deploy-v2-offline | PASS | exit 0 |
| offline-reported | PASS | rb-panda: manifestVersion 1, onboarded true |
| keeps-v1 | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| offline-hold | PASS | waited 10s |
| uplink-up | PASS | exit 0 |
| v2-converged | PASS | rb-panda/dl: Installed 1.1.0, health healthy |
| v2-one-apply | PASS | exit 0, 1 field(s) match |
| cloud-converged | PASS | exit 0, 1 field(s) match |
| uplink-down-2 | PASS | exit 0 |
| deploy-v3 | PASS | exit 0 |
| bundle-export | PASS | exit 0, 1 field(s) match |
| usb | PASS | dry run: simulated (exit 0), answered yes |
| bundle-import | PASS | exit 0, 2 field(s) match |
| v3-installed-offline | PASS | rb-panda/dl: Installed 2.0.0, health healthy |
| uplink-up-2 | PASS | exit 0 |
| cloud-converged-2 | PASS | exit 0, 2 field(s) match |
| v3-one-apply | PASS | exit 0, 1 field(s) match |
| outbox-empty | PASS | rb-panda: manifestVersion 3, onboarded true |
| cleanup | PASS | exit 0 |
| removed | PASS | rb-panda/dl: absent  |

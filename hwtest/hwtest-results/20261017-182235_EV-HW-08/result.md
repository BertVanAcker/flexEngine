# EV-HW-08 Security on the LattePanda: tampered, unsigned, unknown key, downgrade, wrong CA

* started: 2026-10-17T18:22:35
* result: 36 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-08-n8y0iyal/secrets/admin.token`
* catalog: `/tmp/rb-08-n8y0iyal/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-08-n8y0iyal/cloud/oci`
* cloud_port: `37863`
* cloud_sh: `local`
* cloud_url: `http://127.0.0.1:41573`
* dep: `dl`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:41573 --ca '' --admin-token-file /tmp/rb-08-n8y0iyal/secrets/admin.token --catalog /tmp/rb-08-n8y0iyal/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-08-n8y0iyal/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:42833`
* fe_api_port: `42833`
* fe_api_token_file: `/tmp/rb-08-n8y0iyal/secrets/fe-api.token`
* fe_config: `/tmp/rb-08-n8y0iyal/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-08-n8y0iyal/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* gp_podman: `rb-panda-podman`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* igpu_device: `/dev/zero`
* jaeger_url: `http://127.0.0.1:45891`
* journal_source: `file:/tmp/rb-08-n8y0iyal/lp/engine.log`
* lab_ca: `/tmp/rb-08-n8y0iyal/pki/ca.crt`
* lab_ca_lp: `/tmp/rb-08-n8y0iyal/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_up_host: `127.0.0.1`
* lp_up_port: `42833`
* lp_usb: `/tmp/rb-08-n8y0iyal/usb`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-08-n8y0iyal power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182235_EV-HW-08`
* rogue_ca: `/tmp/rb-08-n8y0iyal/pki/rogue-openssl-ca.crt`
* rt: `docker`
* rt_podman: `podman`
* safe_state_file: `/tmp/rb-08-n8y0iyal/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-08-n8y0iyal`
* sim_dir: `/tmp/rb-08-n8y0iyal`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-08-n8y0iyal svc`
* tamper: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py tamper --layout /tmp/rb-08-n8y0iyal/cloud/oci`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-08-n8y0iyal uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-08-n8y0iyal uplink up`
* usb_dir: `/tmp/rb-08-n8y0iyal/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:37863 reachable |
| engine-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| publish | PASS | exit 0, 1 field(s) match |
| tamper | PASS | exit 0, matched '"tampered": true' |
| deploy-good | PASS | exit 0 |
| good-installed | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| old-bundle | PASS | exit 0, 1 field(s) match |
| unsigned | PASS | exit 0 |
| unsigned-refused | PASS | rb-panda/dl: Failed 3.0.0, reasonCode FE-REC-002 |
| rogue | PASS | exit 0 |
| rogue-refused | PASS | rb-panda/dl: Failed 3.1.0, reasonCode FE-REC-002 |
| tampered | PASS | exit 0 |
| tampered-refused | PASS | rb-panda/dl: Failed 3.2.0, reasonCode FE-REC-002 |
| rejects-logged | PASS | exit 0, 1 field(s) match |
| no-bad-apply | PASS | exit 0, 1 field(s) match |
| good-once | PASS | exit 0, 1 field(s) match |
| back-to-good | PASS | exit 0 |
| good-again | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| still-once | PASS | exit 0, 1 field(s) match |
| downgrade-copy | PASS | exit 0 |
| downgrade | PASS | exit 1, matched '422 manifestVersion 1 is older than the accepted 5 (downgrade)' |
| downgrade-refused | PASS | rb-panda: manifestVersion 5, onboarded true |
| downgrade-kept | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| rogue-ca | PASS | exit 0, matched 'made' |
| wrong-ca-mark | PASS | mark wrongca at byte 3954 of /tmp/rb-08-n8y0iyal/lp/engine.log |
| wrong-ca | PASS | dry run: simulated (engine.yaml: cloud.caFile=/tmp/rb-08-n8y0iyal/pki/rogue-openssl-ca.crt; restarted), done |
| wrong-ca-logged | PASS | 3 line(s) matching (?i)certificate verify failed\|CERTIFICATE_VERIFY_FAILED\|verif: {"ts":"2026-10-17T18:22:47.985Z","level":"warning","device":"rb-panda-podman","event":"offline","msg":"offline (GET /api/v1/devices/rb-panda-podman/deployments: TLS verification failed: unable to get  |
| wrong-ca-offline | PASS | rb-panda: manifestVersion 5, onboarded true |
| wrong-ca-deploy | PASS | exit 0 |
| wrong-ca-hold | PASS | waited 8s |
| wrong-ca-kept | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| right-ca | PASS | dry run: simulated (engine.yaml: cloud.caFile=/tmp/rb-08-n8y0iyal/pki/ca.crt; restarted), done |
| right-ca-online | PASS | rb-panda: manifestVersion 6, onboarded true |
| right-ca-v2 | PASS | rb-panda/dl: Installed 1.1.0, health healthy |
| cleanup | PASS | exit 0 |
| removed | PASS | rb-panda/dl: absent  |

# EV-HW-01 Install and enrol flexEngineV2 on the LattePanda

* started: 2026-10-17T18:16:58
* result: 24 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-01-d75et34q/secrets/admin.token`
* catalog: `/tmp/rb-01-d75et34q/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-01-d75et34q/cloud/oci`
* cloud_port: `42881`
* cloud_url: `http://127.0.0.1:38891`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:38891 --ca '' --admin-token-file /tmp/rb-01-d75et34q/secrets/admin.token --catalog /tmp/rb-01-d75et34q/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-01-d75et34q/root/etc/flexengine/engine.yaml PYTHONPATH= /tmp/rb-01-d75et34q/root/opt/flexengine/bin/flexengine`
* fe_api: `http://127.0.0.1:49089`
* fe_api_token_file: `/tmp/rb-01-d75et34q/secrets/fe-api.token`
* fe_config: `/tmp/rb-01-d75et34q/root/etc/flexengine/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-01-d75et34q/root/etc/flexengine/engine.yaml PYTHONPATH= /tmp/rb-01-d75et34q/root/opt/flexengine/bin/flexengine enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* install: `bash /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/packaging/install.sh --root /tmp/rb-01-d75et34q/root --wheel /tmp/rb-01-d75et34q/dist/flexengine_v2-2.0.0a1-py3-none-any.whl --wheelhouse /home/user/modular-interoperable-framework/flexengine/dist/wheelhouse --device-id rb-panda --extras mcu,plc --python /usr/bin/python3`
* journal_source: `file:/tmp/rb-01-d75et34q/lp/engine.log`
* lab_ca: `/tmp/rb-01-d75et34q/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_sudo: ``
* lp_up_port: `49089`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-01-d75et34q power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-181658_EV-HW-01`
* rogue_ca: `/tmp/rb-01-d75et34q/pki/rogue-ca.crt`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-01-d75et34q`
* sim_dir: `/tmp/rb-01-d75et34q`
* suite: `cd /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine && /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp /tmp/rb-01-d75et34q/pt tests/unit/test_config.py tests/unit/test_cli.py tests/unit/test_local_api.py`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-01-d75et34q svc`
* target: `sim`
* token_path: `(hidden)`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-01-d75et34q uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-01-d75et34q uplink up`
* usb_dir: `/tmp/rb-01-d75et34q/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:42881 reachable |
| install | PASS | exit 0, matched 'changes: 11' |
| version | PASS | exit 0, matched '2.0.0' |
| configure | PASS | dry run: simulated (wrote /tmp/rb-01-d75et34q/root/etc/flexengine/engine.yaml (cloud via TLS 37773, local.tcp 127.0.0.1:49089)), done |
| config-check | PASS | exit 0, 2 field(s) match |
| token | PASS | exit 0, 1 field(s) match |
| enroll | PASS | exit 0 |
| config-resolve | PASS | exit 0, 1 field(s) match |
| journal-mark | PASS | mark start at byte 0 of /tmp/rb-01-d75et34q/lp/engine.log |
| start | PASS | exit 0 |
| onboarded-log | PASS | 1 line(s) matching {'event': 'onboarded', 'device': 'rb-panda'}: {"ts":"2026-10-17T18:17:05.537Z","level":"info","device":"rb-panda","event":"onboarded","msg":"onboarded"} |
| status-onboarded | PASS | rb-panda: manifestVersion 0, onboarded true |
| token-deleted | PASS | exit 0, matched 'token gone' |
| capabilities-api | PASS | GET http://127.0.0.1:49089/v1/capabilities/rb-panda -> 200, 1 field(s) match |
| capabilities-cloud | PASS | exit 0, 2 field(s) match |
| doctor | PASS | exit 0, 1 field(s) match |
| first-release | PASS | exit 0 |
| first-installed | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| suite | PASS | exit 0, matched '462 passed' |
| reinstall | PASS | exit 0, matched 'changes: 0' |
| config-kept | PASS | exit 0, 1 field(s) match |
| still-running | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| cleanup | PASS | exit 0 |
| removed | PASS | rb-panda/dl: absent  |

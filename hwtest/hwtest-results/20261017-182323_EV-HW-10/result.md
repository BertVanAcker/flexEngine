# EV-HW-10 Observability and status of flexEngineV2 on the LattePanda

* started: 2026-10-17T18:23:23
* result: 25 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-10-ciu_xvra/secrets/admin.token`
* catalog: `/tmp/rb-10-ciu_xvra/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-10-ciu_xvra/cloud/oci`
* cloud_port: `46787`
* cloud_sh: `local`
* cloud_url: `http://127.0.0.1:34097`
* dep: `dl`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:34097 --ca '' --admin-token-file /tmp/rb-10-ciu_xvra/secrets/admin.token --catalog /tmp/rb-10-ciu_xvra/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-10-ciu_xvra/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:60267`
* fe_api_port: `60267`
* fe_api_token_file: `/tmp/rb-10-ciu_xvra/secrets/fe-api.token`
* fe_config: `/tmp/rb-10-ciu_xvra/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-10-ciu_xvra/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* gp_podman: `rb-panda-podman`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* igpu_device: `/dev/zero`
* jaeger_url: `http://127.0.0.1:46083`
* journal_source: `file:/tmp/rb-10-ciu_xvra/lp/engine.log`
* lab_ca: `/tmp/rb-10-ciu_xvra/pki/ca.crt`
* lab_ca_lp: `/tmp/rb-10-ciu_xvra/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* lp_up_host: `127.0.0.1`
* lp_up_port: `60267`
* lp_usb: `/tmp/rb-10-ciu_xvra/usb`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-10-ciu_xvra power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-182323_EV-HW-10`
* rogue_ca: `/tmp/rb-10-ciu_xvra/pki/rogue-openssl-ca.crt`
* rt: `docker`
* rt_podman: `podman`
* safe_state_file: `/tmp/rb-10-ciu_xvra/lp/safe-state.json`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-10-ciu_xvra`
* sim_dir: `/tmp/rb-10-ciu_xvra`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-10-ciu_xvra svc`
* tamper: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py tamper --layout /tmp/rb-10-ciu_xvra/cloud/oci`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-10-ciu_xvra uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-10-ciu_xvra uplink up`
* usb_dir: `/tmp/rb-10-ciu_xvra/usb`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:46787 reachable |
| health | PASS | GET http://127.0.0.1:60267/v1/health -> 200, 2 field(s) match |
| publish | PASS | exit 0, 1 field(s) match |
| deploy-v1 | PASS | exit 0 |
| v1-installed | PASS | rb-panda/dl: Installed 1.0.0, health healthy |
| status-doc | PASS | GET http://127.0.0.1:60267/v1/status -> 200, 6 field(s) match |
| deployment-doc | PASS | GET http://127.0.0.1:60267/v1/devices/rb-panda/deployments/dl -> 200, 3 field(s) match |
| capabilities | PASS | GET http://127.0.0.1:60267/v1/capabilities/rb-panda -> 200, 1 field(s) match |
| metrics | PASS | GET http://127.0.0.1:60267/v1/metrics -> 200, 1 field(s) match |
| no-token | PASS | GET http://127.0.0.1:60267/v1/status -> 401 |
| no-write | PASS | POST http://127.0.0.1:60267/v1/reconcile -> 403 |
| journal-mark | PASS | mark update at byte 2017 of /tmp/rb-10-ciu_xvra/lp/engine.log |
| mark-update | PASS | mark update at deviceTime 1792261406.526 (2 record(s) before it) |
| deploy-v2 | PASS | exit 0 |
| v2-installed | PASS | rb-panda/dl: Installed 1.1.0, health healthy |
| history | PASS | dl Installing 1.1.0 -> dl Installed 1.1.0; 0.98 s from the first to the last record |
| history-delivered | PASS | GET http://127.0.0.1:60267/v1/history?device=rb-panda -> 200, 1 field(s) match |
| log-structured | PASS | 1 line(s) matching {'event': 'action.apply', 'device': 'rb-panda', 'dep': 'dl', 'version': '1.1.0', 'level': 'info', 'ts': {'regex': 'Z$'}}: {"ts":"2026-10-17T18:23:28.144Z","level":"info","device":"rb-panda","event":"action.apply","dep":"dl","version":"1.1.0","msg":"apply dl 1.1.0","digest":"sha256:3ed8517aa0f0f0d353e74d951f77217d65bd0f26 |
| log-correlation | PASS | GET http://127.0.0.1:60267/v1/history?device=rb-panda -> 200, 1 field(s) match |
| jaeger | PASS | GET http://127.0.0.1:46083/api/traces?service=flex-engine-rb-panda&operation=engine.install%20dl&lookback=1h&limit=20 -> 200, 1 field(s) match |
| jaeger-gate | PASS | GET http://127.0.0.1:46083/api/traces?service=flex-engine-rb-panda&operation=engine.health-gate&lookback=1h&limit=20 -> 200, 1 field(s) match |
| cli-status | PASS | exit 0, 2 field(s) match |
| console-fleet | PASS | exit 0, 2 field(s) match |
| cleanup | PASS | exit 0 |
| removed | PASS | rb-panda/dl: absent  |

# EV-HW-04 Portenta H7 module hot-load and rollback through the LattePanda gateway

* started: 2026-10-17T18:18:46
* result: 27 passed, 0 failed, 0 skipped
* project commit: `52e7172-dirty`
* runner (flexNode-arduino) commit: `52e7172-dirty`
* admin_token_file: `/tmp/rb-04-v7x_m7i9/secrets/admin.token`
* base_version: `1.0.0`
* catalog: `/tmp/rb-04-v7x_m7i9/catalog.yaml`
* cloud_ca: ``
* cloud_host: `127.0.0.1`
* cloud_layout: `/tmp/rb-04-v7x_m7i9/cloud/oci`
* cloud_port: `43739`
* cloud_url: `http://127.0.0.1:43123`
* desk: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/deskctl.py --cloud http://127.0.0.1:43123 --ca '' --admin-token-file /tmp/rb-04-v7x_m7i9/secrets/admin.token --catalog /tmp/rb-04-v7x_m7i9/catalog.yaml`
* dry_run: `1`
* fe: `env FLEXENGINE_CONFIG=/tmp/rb-04-v7x_m7i9/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli`
* fe_api: `http://127.0.0.1:59265`
* fe_api_port: `59265`
* fe_api_token_file: `/tmp/rb-04-v7x_m7i9/secrets/fe-api.token`
* fe_config: `/tmp/rb-04-v7x_m7i9/lp/engine.yaml`
* fe_enroll: `env FLEXENGINE_CONFIG=/tmp/rb-04-v7x_m7i9/lp/engine.yaml /home/user/modular-interoperable-framework/flexengine/.venv/bin/python -m flexengine2.local.cli enroll`
* fe_host: `local`
* flexengine: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine`
* gp: `rb-panda`
* hwtest: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest`
* journal_source: `file:/tmp/rb-04-v7x_m7i9/lp/engine.log`
* lab_ca: `/tmp/rb-04-v7x_m7i9/pki/ca.crt`
* local_ip: `192.0.2.2`
* lp: `local`
* lp_sh: `sh -c`
* mcu: `rb-panda.portenta`
* model_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/examples/models/demo-pick`
* operator: `dry-run`
* power_cut: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-04-v7x_m7i9 power-cycle --down 3`
* project_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* python: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python`
* repo: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks`
* results_dir: `/home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/hwtest-results/20261017-181846_EV-HW-04`
* rogue_ca: `/tmp/rb-04-v7x_m7i9/pki/rogue-ca.crt`
* sim: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-04-v7x_m7i9`
* sim_dir: `/tmp/rb-04-v7x_m7i9`
* smp_host: `127.0.0.1`
* smp_port: `48761`
* smp_tty: ``
* stall_s: `4.287`
* svc: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-04-v7x_m7i9 svc`
* target: `sim`
* uplink_down: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-04-v7x_m7i9 uplink down`
* uplink_up: `/home/user/modular-interoperable-framework/flexengine/.venv/bin/python /home/user/modular-interoperable-framework/.claude/worktrees/runbooks/flexengine/hwtest/simdesk.py ctl --dir /tmp/rb-04-v7x_m7i9 uplink up`
* zenoh_connect: `tcp/127.0.0.1:42783`
* zenoh_mode: `peer`

| Step | Result | Details |
|---|---|---|
| stack | PASS | 127.0.0.1:43739 reachable |
| gateway-up | PASS | rb-panda: manifestVersion 0, onboarded true |
| child-up | PASS | rb-panda.portenta: manifestVersion 0, onboarded true |
| engine-stop-1 | PASS | exit 0 |
| base-confirmed | PASS | SMP image_list on 127.0.0.1:48761: {"images": [{"active": true, "bootable": true, "confirmed": true, "hash": "def2d69eec9f1ee22646354ea55576f5da8503be8f... |
| base-info | PASS | SMP flexmod_info on 127.0.0.1:48761: {"abi_major": 1, "abi_minor": 0, "arch": "armv7m", "base": "1.0.0", "board": "mps2/an385", "caps": 3, "confirmed": tr... |
| engine-start-1 | PASS | exit 0 |
| publish | PASS | exit 0, 1 field(s) match |
| deploy | PASS | exit 0 |
| servo-installed | PASS | rb-panda.portenta/servo: Installed 2.0.0, health healthy |
| gripper-installed | PASS | rb-panda.portenta/gripper: Installed 1.0.0, health healthy |
| relay | PASS | 0/gripper_station/state/uc2::msg::dds_::GripperState_/RIHS01_5c58c07f5c930fdda5bf98ef617a5852553b701ccea33900c10fa55cf34a42db: {"faults": [], "force_n": 0.0, "object_detected": false, "phase": 0, "seq": 116, "stamp_ns": 26483000000, "width_mm":... (2 sample(s)) |
| mark-stall | PASS | mark stall at deviceTime 1792261151.775 (4 record(s) before it) |
| deploy-stall | PASS | exit 0 |
| stall-failed | PASS | rb-panda.portenta/servo: Failed 3.0.0, reasonCode FE-REC-004, rolledBackTo 2.0.0, failFast true |
| stall-time | PASS | servo Installing 3.0.0 -> servo Failed 3.0.0; 4.29 s from the first to the last record |
| no-reupload | PASS | exit 0, 2 field(s) match |
| rollback-logged | PASS | exit 0, 1 field(s) match |
| deploy-abi2 | PASS | exit 0 |
| abi2-refused | PASS | rb-panda.portenta/servo: Failed 4.0.0-abi2, reasonCode FE-REC-002 |
| abi2-no-apply | PASS | exit 0, 1 field(s) match |
| engine-stop-2 | PASS | exit 0 |
| sockets | PASS | SMP flexmod_status on 127.0.0.1:48761: {"boots": 1, "sockets": [{"activations": 2, "active": 0, "auto_reverts": 1, "confirmed": 0, "dep": "servo", "errors":... |
| engine-start-2 | PASS | exit 0 |
| back-to-2 | PASS | exit 0 |
| servo-2-again | PASS | rb-panda.portenta/servo: Installed 2.0.0, health healthy |
| still-one-apply | PASS | exit 0, 1 field(s) match |

# flexEngineV2 coverage, alpha (plan §7.3)

Line coverage of layers 1–6 together, measured on 2026-10-17 (`results/coverage-alpha.json` has the details and how
it was measured). Engine subprocesses are included; code that runs inside containers is not.

| Group | Lines | Branches | Target |
|---|---|---|---|
| core (core/, artefacts/, identity/, cloud/, config/, contracts, util, host) | 94.4 % | 89.8 % | ≥ 90 % lines |
| core/reconciler.py | 95.8 % | 92.1 % | ≥ 80 % branches |
| artefacts/store.py (release verification) | 90.9 % | 82.3 % | ≥ 80 % branches |
| driver container | 94.7 % | 86.2 % | ≥ 80 % lines |
| driver native-process | 87.6 % | 83.9 % | ≥ 80 % lines |
| driver native-systemd | 88.3 % | 80.5 % | ≥ 80 % lines |
| driver applab | 90.7 % | 81.2 % | ≥ 80 % lines |
| driver mcu | 90.9 % | 75.3 % | ≥ 80 % lines |
| driver plc-verify | 93.9 % | 81.0 % | ≥ 80 % lines |
| driver os-rauc | 86.1 % | 82.4 % | ≥ 80 % lines |
| driver arduino-ota | 89.3 % | 80.7 % | ≥ 80 % lines |
| services (local/, obs/, gateway/; no target) | 83.9 % | 77.9 % | – |
| overall (without testing/) | 89.6 % | 83.0 % | ≥ 85 % lines |

All targets are met. Two drivers were below 80 % in the first measurement: applab 68.7 % (its
integration teardown killed the engines with SIGKILL, so their coverage data was lost; the teardown now stops them
with SIGTERM) and os-rauc 77.2 % (new tests: `tests/unit/test_os_rauc_paths.py`, device-side
health checks, a slot marked bad, removal of a never committed OS, mark-good once per boot, failed and missing
reboots). `core/health.py` and `artefacts/verify.py` of the plan live inside `core/reconciler.py` and
`artefacts/store.py`. Lowest services files: `obs/instrument.py`, `obs/otel.py` (OTel needs `FLEX_OTEL=1`),
`local/doctor.py`, `local/sdnotify.py`.

Reproduce: `flexengine/run_tests.sh coverage` (layers 1–6 including the nightly contract tier; needs docker, podman
and the P3/P4 builds for the full figure).

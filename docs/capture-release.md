# Worked example: a capture (data-collection) release

A data-collection workload is an ordinary native release: a payload that samples a source and writes what it saw, a
signed descriptor that tells the engine how to check and restart it, and a deployment that sets its parameters. Nothing
in the engine is specific to it. This page walks through the example in
[`examples/capture_release/`](examples/capture_release/); flexClone's capture node (`flexclone edge-package`, request
UE-C1) has the same shape. [`tests/integration/examples/test_capture_release.py`](../tests/integration/examples/test_capture_release.py)
checks every document against the contracts and runs the release on a real engine with the process driver.

| File | What |
|---|---|
| `payload/app.py` | the workload: samples one source, writes rotating JSON Lines files, keeps a retention budget, serves `/healthz`. Standard library only |
| `payload/collector.json` | the source and the defaults; part of the payload, so read-only on the device and covered by the release signature |
| `descriptor.json` | the release descriptor (`descriptor.v1alpha1`): profile `native`, `http` health, restart policy `always`, the parameters it understands, informational `resources` |
| `desired-state.json` | a CP-1 desired state with one `ApplicationDeployment` on `x-flex-driver: native`, `x-flex-runtime: systemd`, with rotation and retention as deployment parameters |

## 1. The payload: what the workload gets from the engine

A native workload runs with only the engine's variables, never the engine's environment
([CP-5](../api/cp5-driver-spi.md#native-both-drivers-own-python-dependencies-and-platform-layers-ev-36-ev-37)):

| Variable | Use in the example |
|---|---|
| `FLEX_HEALTH_PORT` | the port of `/healthz` on 127.0.0.1; the engine picks it, the descriptor says `"port": "${FLEX_HEALTH_PORT}"` |
| `FLEX_DATA_DIR` | the writable directory `<dataDir>/native/<dep>/data`, kept across updates and rollbacks (both native runtimes, process and systemd); captures go to `$FLEX_DATA_DIR/captures/` |
| `FLEX_PARAM_<NAME>` | each deployment parameter, upper-cased: `FLEX_PARAM_ROTATE_S`, `FLEX_PARAM_KEEP_FILES`, ... |
| `FLEX_UNIT` | the engine's marker of the unit (not used by the workload) |

The unpacked payload tree is the working directory and is read-only by contract: the example reads
`collector.json` from it and writes nothing there (the test compares the tree before and after). Its behaviour:

- **Sampling**: every `sample_period_s` one record `{"t": <unix time>, "value": ...}` from the source
  (`file`: the first line of a file opened read-only, `/proc/loadavg` by default; `synthetic`: a counter and a sine).
- **Rotation**: the open file is `<prefix>-<UTC start>.jsonl.part`; after `rotate_s` it is fsynced and renamed to
  `.jsonl`, so a complete file never changes again and a collector can pick it up. A file left open by a crash is
  completed at the next start.
- **Retention**: after each rotation at most `keep_files` complete files and `max_mb` MiB in total; the oldest go.
- **Health**: `GET /healthz` answers 200 while samples arrive (the last one younger than `stale_s`) and the capture
  directory is writable, 503 otherwise. The engine's health gate uses it at install, so a release that cannot reach
  its source is rolled back (FE-REC-004) instead of recording nothing.
- **Stop**: the engine stops a workload with SIGTERM; the example completes its open file and exits 0.

## 2. The descriptor: what the engine enforces

The descriptor is part of the signed release, so the control plane cannot widen it:

```json
"health": {"type": "http", "port": "${FLEX_HEALTH_PORT}", "path": "/healthz", "periodSeconds": 0.5,
           "successThreshold": 2, "timeoutSeconds": 30},
"restartPolicy": {"policy": "always", "maxRetries": 5, "restartSec": 2},
"safetyClass": "none",
```

- `health`: the gate passes after two good probes within 30 s; on systemd and the process driver the probe goes to
  `127.0.0.1:$FLEX_HEALTH_PORT`.
- `restartPolicy`: `always` restarts the collector also after a clean exit; more than 5 failures within 60 s stop the
  restarts (start limit) and the deployment is reported failed.
- `safetyClass: none`: a read-only collector has no actuation path; the deployment's `x-flex-safetyClass` must say
  the same.
- `parameters`: the names the payload understands (informational today: the engine does not compare them with the
  deployment's parameters).
- `resources` (`cpu_cores`, `memory_mb`, here also `disk_mb` for the retention budget): informational, nothing is
  reserved or limited ([bundle.v1alpha1.md §2](../api/bundle.v1alpha1.md#2-release-manifest-flexnode-release)).

## 3. The deployment: what the operator sets

Rotation and retention are per-site choices, so they are deployment parameters, not baked into the release:

```json
"parameters": {"prefix": "line3", "sample_period_s": 1, "rotate_s": 3600, "keep_files": 48, "max_mb": 512}
```

`x-flex-runtime: systemd` makes the collector a systemd unit that survives an engine restart and a reboot;
`process` (the default runtime of `drivers.native: true`) is for development and tests. `output_dir` points the
captures elsewhere (e.g. a mounted disk); without it they stay in `FLEX_DATA_DIR`, which the engine owns: the
systemd driver deletes it when the deployment is removed, so copy what you need off the device before you remove it.

For the container profile the same payload goes into an image that sets `FLEX_DATA_DIR=/data` on a named volume
(descriptor `runOptions.volumes: ["data:/data"]`, allowed on the device in `drivers.container.allowedRunOptions`);
the volume is kept across updates and rollbacks and deleted with the deployment.

## 4. Build, publish, deploy

```sh
python tools/native_release.py py-payload docs/examples/capture_release/payload -o build/capture.tar.gz
python tools/native_release.py release --oci build/oci --key release.key --module capture_logger --version 1.0.0 \
    --descriptor docs/examples/capture_release/descriptor.json build/capture.tar.gz
```

`release` signs the release with `release.key` (the devices list its public key in `releasePublicKeys`) and prints
the manifest digest; put it into the deployment's `digest` (the example's zeros are a placeholder) and publish the
desired state from your control plane. A payload with Python dependencies adds them with `py-payload -r ...` (an
offline, hash-pinned venv per payload, EV-36).

On the device, `flexengine deployments` shows the collector `Installed` once the health gate passed and
`flexengine logs` its stdout; the captures are in `<dataDir>/native/capture/data/captures/`.

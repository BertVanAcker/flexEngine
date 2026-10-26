# Migrating from flexEngine 0.6 to 2.0

flexEngine 2.0 is not a drop-in update. It talks to a different cloud API, deploys signed releases instead of
building repositories on the device, and has its own configuration and CLI. Plan the move per device; v1 and v2
devices can run side by side while the flexCloud console still drives v1 devices.

## Stay on 0.6 for now

A device that the flexCloud console deploys to (Push, Build, Run, Stop over Redis) needs v1. Pin it so a routine
upgrade does not pull 2.0:

```bash
pip install 'flexEngine<2'
```

0.6.x fixes come from branch `v1.x` (tags `v0.6.*`). `pip install flexEngine` without a version installs 0.6.x until
2.0.0 is final; after that it installs 2.x.

## Move a device to 2.0

1. **Python and platform.** 2.0 needs Python ≥ 3.11 on Linux (x86_64 or aarch64). Windows and ARMv6 devices stay on
   0.6.
2. **Stop and remove v1.** `flexEngine stop <workspace>`, then remove the v1 venv (or `pip uninstall flexEngine`).
   2.0 is the same PyPI project, so installing it into the v1 venv replaces v1. The v1 workspace (`agent.py`,
   `configuration/`, `apps/`, `logs/`) is not read by 2.0; keep it only as a record.
3. **Install 2.0.** On a lab device: the offline bundle and `sudo packaging/install.sh --device-id <id> --extras ...`
   (README §1). For development: `pip install --pre 'flexEngine>=2.0.0a1'`. The CLI is `flexengine` (lowercase); v1's
   `flexEngine` command is gone.
4. **Write `/etc/flexengine/engine.yaml`.** v1 settings map as follows:

   | v1 `config.yaml` | 2.0 |
   |---|---|
   | `flexCloud.redis_host` / `redis_port` / `redis_db` | `cloud.apiUrl` (HTTPS) and `cloud.caFile`; there is no Redis |
   | `deviceCredentials.name` | `devices[].deviceId` |
   | `deviceCredentials.key`, `shared_secret` | none: the device creates its own Ed25519 key and onboards with a one-time token (`flexengine enroll`) |
   | `deviceCredentials.projectID`, `primaryHardwareID` | none in the engine; the cloud and the model place software on devices |
   | `heartbeat.*` | `cloud.pollInterval`; status reports replace heartbeats |
   | `logging.*` | `logFormat`, `historyMax`, `otel` |

   Add the trust anchors (`cloud.cloudPublicKey`, `cloud.releasePublicKeys`) and one driver block per runtime the
   device offers. Check it with `flexengine config check /etc/flexengine/engine.yaml --resolve`. Every key:
   [docs/config-reference.md](docs/config-reference.md).
5. **Turn applications into releases.** A v1 flexIA package (repository with `flexIA.ini`) becomes signed 2.0 releases
   with `flex import-flexia` (flexmodel's CLI): it runs the v1 build in CI, packages the result and proposes the
   desired state. What maps and what is refused (custom workflows): [docs/flexia-import.md](docs/flexia-import.md).
6. **Enroll and start.** Create an onboarding token in the cloud, `flexengine enroll --token-file ...`, then
   `systemctl enable --now flexengine` (README §3). `flexengine status` and `flexengine doctor` show the result.

## Code that imported flexEngine

The v1 Python API (`flexEngine.supervisor`, `flexEngine.command`, ...) has no 2.0 equivalent. Integrations talk to
the engine through its contracts instead: the southbound API for clouds, the local API (CP-7) and CLI on the device,
the driver SPI (CP-5) for new runtimes ([docs/spi-guide.md](docs/spi-guide.md)).

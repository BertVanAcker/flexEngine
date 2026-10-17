# flexEngineV2 packaging (alpha, EV-08)

| File | What |
|---|---|
| `install.sh` | idempotent installer: `flex` user, `/etc/flexengine`, `/var/lib/flexengine`, venv under `/opt/flexengine`, systemd unit; `--uninstall [--purge]`, `--dry-run` |
| `systemd/flexengine.service` | `Type=notify`, `WatchdogSec=30`, `Restart=on-failure`, `User=flex`, hardening; group needs in its header |
| `systemd/flexengine@.service` | second instance with `/etc/flexengine/<name>.yaml` and `/var/lib/flexengine-<name>` (dev/test) |
| `engine.yaml.template` | first `/etc/flexengine/engine.yaml` (written only if absent) |
| `requirements-lock.txt` | pinned base + `[mcu,applab,plc,otel]` closure, used as pip constraints |
| `gen-lock.py` | regenerates the lock from a working venv |
| `build-wheels.sh` | engine wheel + offline wheelhouses (x86_64, aarch64) + per-arch bundles in `flexengine/dist/` |

Layout on the target:

| Path | Owner / mode | Upgrade |
|---|---|---|
| `/opt/flexengine` (venv, `bin/flexengine`) | root 0755 | replaced/updated |
| `/etc/flexengine/engine.yaml` | root:flex 0640 | **never overwritten** |
| `/etc/flexengine/tokens/` | flex:flex 0700, token files 0600 owned by flex (the engine deletes a token after onboarding) | **never overwritten** |
| `/var/lib/flexengine` (`dataDir`) | flex 0700 (systemd `StateDirectory=`) | **never touched** |
| `/run/flexengine` (`api.sock`) | flex, group flex 0750 (`RuntimeDirectory=`) | – |
| `/etc/systemd/system/flexengine{,@}.service`, `*.service.d/10-groups.conf` | root 0644 | rewritten |

## LattePanda 3 Delta (Ubuntu 24.04, x86_64)

On a build host (any Linux with Docker, network access once):

```bash
flexengine/packaging/build-wheels.sh                # -> flexengine/dist/flexengine-v2-2.0.0a1-linux_x86_64.tar.gz
scp flexengine/dist/flexengine-v2-2.0.0a1-linux_x86_64.tar.gz lattepanda.lab:
```

On the LattePanda (offline is fine; needs `python3` 3.12 and `python3.12-venv`, which install.sh apt-installs if
missing):

```bash
tar xzf flexengine-v2-2.0.0a1-linux_x86_64.tar.gz && cd flexengine-v2-2.0.0a1
sudo packaging/install.sh --extras mcu,plc,applab,otel --dry-run     # the plan
sudo packaging/install.sh --extras mcu,plc,applab,otel --device-id panda
/opt/flexengine/bin/flexengine version                                 # 2.0.0a1
sudo install -m 0600 -o flex -g flex /path/to/token /etc/flexengine/tokens/panda
sudoedit /etc/flexengine/engine.yaml                                   # apiUrl, keys, drivers
sudo systemctl enable --now flexengine && journalctl -u flexengine -f
```

The bundle's `install.sh` finds the wheel and `wheelhouse/linux_x86_64-cp312` next to it. From a repo checkout:
`sudo flexengine/packaging/install.sh --wheel flexengine/dist/flexengine_v2-*.whl --wheelhouse flexengine/dist/wheelhouse`,
or `--from-source flexengine` (online, or with a wheelhouse that holds setuptools).

Groups: install.sh adds a drop-in `SupplementaryGroups=` with whichever of `dialout` (MCU serial) and `docker`
(container driver, `runtime: docker`) exist; Docker CE must be installed first for `docker` to be picked up
(`--groups dialout,docker` to force, `--groups none` for none). RAUC (D-Bus policy) and the native systemd driver
(polkit rule) need host policy files that are not part of alpha — see the unit header.

Upgrade: run the new bundle's `install.sh` with the same options. The venv is updated, the units are rewritten,
a running service is restarted; `engine.yaml`, `tokens/` and `/var/lib/flexengine` are kept. Re-running with the
same inputs ends with `changes: 0`.

Uninstall: `sudo packaging/install.sh --uninstall` (keeps config, data and the `flex` user);
`--uninstall --purge` removes them too.

Second instance (dev/test): `sudo cp /etc/flexengine/engine.yaml /etc/flexengine/sil.yaml`, set
`dataDir: /var/lib/flexengine-sil`, `sudo systemctl start flexengine@sil`.

`Type=notify`: the unit expects `READY=1` and a `WATCHDOG=1` ping at least every 30 s
(`flexengine2.local.sdnotify`). Until `host.run` sends them the unit times out at start (`TimeoutStartSec=120`);
for a quick try use a drop-in with `Type=simple` and `WatchdogSec=0`.

## Mac (Apple Silicon) / dev hosts — no service

```bash
python3 -m venv flexengine/.venv
flexengine/.venv/bin/pip install -e 'flexengine[dev]'            # + ,mcu,plc,applab,otel as needed
flexengine/.venv/bin/flexengine version
flexengine/.venv/bin/flexengine run --config my-engine.yaml       # dataDir: a local directory, foreground
```

No `flex` user, no systemd; `sdnotify` is a no-op without `$NOTIFY_SOCKET`. For a staged copy of the Linux layout
(e.g. to inspect what install.sh writes): `packaging/install.sh --no-service --root /tmp/fe --from-source flexengine`
(install.sh needs bash; macOS's bash 3.2 is enough).

## Arduino UNO Q (Linux side, Debian arm64)

- Check `python3 --version` on the board: build-wheels.sh makes `linux_aarch64-cp311` (Debian 12) and
  `linux_aarch64-cp313` (Debian 13) wheelhouses by default (`--targets aarch64-3.12` for others).
- The arm64 wheelhouses carry `[applab,otel]` only (the UNO Q runs the applab driver; the MCU side is Zephyr).
  `[mcu]` (crcmod) and `[plc]` (pyads) have no PyPI wheels: build-wheels.sh prebuilds them in a
  `python:X.Y` arm64 container when Docker can run `linux/arm64` (qemu-user binfmt, or an arm64 build host such as the
  Mac); otherwise their sdists land in `wheelhouse/*/sdist/` and the board needs `build-essential python3-dev`.
- Install: `sudo packaging/install.sh --extras applab --device-id unoq` from the `linux_aarch64` bundle. The
  `flex` user needs no `dialout`/`docker` here unless the container driver is used.
- `cryptography` wheels for aarch64 are manylinux_2_34 (glibc ≥ 2.34; Debian 12 has 2.36).

## Regenerating the lock

```bash
flexengine/.venv/bin/pip install -e 'flexengine[dev,mcu,applab,plc,otel]'   # the environment to pin
flexengine/.venv/bin/python flexengine/packaging/gen-lock.py                 # rewrites requirements-lock.txt
flexengine/.venv/bin/python flexengine/packaging/gen-lock.py --check         # CI: exit 1 if stale
flexengine/packaging/build-wheels.sh --clean && pytest flexengine/tests/packaging
```

The lock is the pip-freeze of the dependency closure of `flexengine-v2[mcu,applab,plc,otel]` in that venv (no
pip-tools). It is passed as `-c` (constraints): it pins what is installed but does not force dists a target does
not need; a dependency that only another Python needs is resolved there unpinned.

## Tests

`flexengine/.venv/bin/python -m pytest flexengine/tests/packaging -m 'slow or docker or not slow'` — sdnotify unit
tests, dry-run and `--root` staging on the host, and (marker `docker`) clean `ubuntu:24.04` containers with systemd:
install from the wheel, `systemd-analyze verify`, idempotent reinstall, upgrade, uninstall, purge, plus the arm64
wheelhouse checks (an arm64 install only where `docker run --platform linux/arm64` works).

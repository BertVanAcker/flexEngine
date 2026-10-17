# Importing v1 flexIA packages (`flex import-flexia`, EV-35)

v1 flexEngine receives a flexIA package (a source tree with `flexIA.ini`) and **builds and runs it on the device**.
flexEngineV2 never builds on the device: it installs signed releases (CP-3) named by digest in a signed desired state
(CP-1). `flex import-flexia` runs the v1 build in CI, packages the result as V2 releases and proposes the desired state:

```
flex import-flexia <package dir> [--device NAME ...] [--device-id NAME=ID ...] [--arch x86_64,aarch64]
                   [--out DIR] [--key release.key] [--version V]
                   [--publish URL --admin-token-file F [--plan]] [--health auto|shim|http:PORT[/PATH]]
                   [--accept-lossy] [--partial] [--find-links DIR ...] [--no-index] [--python-version X.Y]
                   [--dry-run] [--json]
```

It is a subcommand of the flexmodel CLI next to `flex release` (`flexmodel/src/flexmodel/release/flexia.py`,
`flexia_ini.py`, `flexia_shim.py`), not a separate entry point: it writes the same release format with the same code
(probe P2 `publish_release` / `sign_release` / OCI `Layout` / `upload`, via `release/_probes.py`, and
`pipeline.sublayout_tar`), the same release key and the same admin API upload, so a device cannot tell an imported
release from one made by `flex release`.

Output (`--out`, default `./flexia-release`): `oci/` (OCI layout: release manifests, descriptor config blobs,
payload layers, SPDX SBOM, Ed25519 signature referrers), `proposal.json` (per device: the `deployments` list for
`POST /api/v1/admin/plans` plus order notes and a capability check list), `report.json` (every unit, release and
note), `work/` (build scratch). Exit code: 0 imported; 1 imported with refusals (`--partial`) or a build/publish
failure; 2 refused (nothing built).

## What maps

| v1 workflow | V2 release (driver) | Notes |
|---|---|---|
| `docker` | one container release (`container`) | Dockerfile at the package root (v1 `docker_build` ignores `workflow_link`; a link to a Dockerfile or a directory is honoured). `[build] arguments`: `--build-arg`, `-f`, `--target`. `[run] arguments`: see below |
| `docker-compose` | one container release **per service** (`container`) | `build:` (context, dockerfile, dockerfile_inline, args, target) is built; `image:` is pulled at import time and released by digest. Until the compose profile (EV-27) every service is its own deployment |
| `docker-custom-compose` | as `docker-compose` | v1 never generated the file; imported when it exists (warning) |
| `application-descriptor` (`config.yml`) | per component: `type: docker` → container release; `type: python` (also `flcpy`, `native`) → native release (`native`) | v1 left this workflow TODO; the importer reads `components` / `manifests` (`path`, optional `entry`, `arguments`, `dockerfile`) / `requirements` / `dependencies` |
| `custom`, `none`, unknown | **refused** | a custom workflow runs arbitrary steps on the device; build it in CI and use `docker` or `application-descriptor` |

Every release: descriptor (`module` = `<package>-<service>`, repository `flexnodes/<module>`, `interfaceHash: null`,
`safetyClass: none` — a v1 package has no flexModel module, so a safety-relevant workload cannot be imported: it
needs a model, S4), payload layer(s), minimal SPDX SBOM (the node, its Dockerfile `FROM` images or its wheels), the
signature referrer by `--key` (created when missing; the devices must list its public key in `releasePublicKeys`).
Version: `--version` (CI: `git describe`), default `0.0.0-src.<12 hex of the package digest>`.

**Container releases.** The image is built here (`docker buildx build --platform linux/<arch> --load`, else
`docker|podman build --platform`), then an overlay image (no `RUN` steps, so it also builds for a foreign CPU without
emulation) bakes in what compose / `docker run` would have set at start: `environment`, `env_file`, `-e`,
`--env-file` → `ENV`; `entrypoint`, `command`, `working_dir`, `user`, `--entrypoint`, `-w`, `-u`. It is saved
(`docker save --platform`) as a docker-archive payload annotated `dev.flex.imageRef =
docker.io/flexnode/<module>@<digest>` (the engine runs it by digest) and `dev.flex.arch`. Compose interpolation
(`${VAR}`, `${VAR:-default}`, `.env`) happens at import time, so values are frozen into the signed release.

| compose / `docker run` | V2 |
|---|---|
| `devices`, `--device` | `runOptions.devices` (must be on the device's `drivers.container.allowedRunOptions`) |
| `gpus`, `deploy.resources.reservations.devices` (gpu), `--gpus` | `runOptions.gpus` |
| `runtime`, `group_add`, `--runtime`, `--group-add` | `runOptions.runtime`, `runOptions.groupAdd` |
| `restart`, `deploy.restart_policy`, `--restart` | `restartPolicy` (`no` → never, `always`/`unless-stopped` → always, `on-failure[:N]`); compose default `no` becomes the V2 default on-failure:5 |
| `healthcheck` with an http URL on the container (`curl http://localhost:8080/healthz`, `urllib…urlopen(...)`) | http health probe on that port/path |
| no (http) healthcheck | **liveness shim**: `python3 /flex/flex_health_shim.py --port 18080 -- <entrypoint+cmd>` serves `/healthz` while the node runs (needs python3 in the image; flcpy nodes have it). Otherwise give the node an endpoint and pass `--health http:PORT/PATH` |
| `depends_on` | order note: the proposal lists deployments in dependency order and `order[].after`; V2 starts them independently |
| `cpus`, `mem_limit`, `deploy.resources.limits`, `[REQUIREMENTS] cpu/memory` | `descriptor.resources` + a placement check; **not enforced** |

**Native releases** (`type: python` runnables; payload layout of EV-36, `bundle.v1alpha1.md` §2): the runnable's
files, a generated `app.py` (serves `/healthz` on `FLEX_HEALTH_PORT` and runs the entry script in-process with
`runpy`, cwd = the unpacked tree, `arguments` as argv; an entry called `app.py` is renamed `flex_entry_app.py`),
`flex_health_shim.py`, `release.json`, `unit.json`, and — when the device has `[DEPENDENCIES] lib_deps` or the
runnable a `requirements.txt` — `requirements.lock` (`name==version --hash=sha256:…`) + `wheels/` from
`pip download --only-binary=:all:` for CPython `--python-version` (default: the importer's) and each CPU type
(`--find-links` / `--no-index` for private wheels). Pure-python wheels → one layer for every CPU type; CPU- or
ABI-specific wheels → **one release with one native layer per `--arch`**, annotated `dev.flex.arch` and
`dev.flex.pyabi`, picked on the device by the native driver's `platform()` (EV-37). Without EV-36 in the engine,
a payload with wheels installs but its imports fail (the process driver runs the engine's interpreter).

**REQUIREMENTS** → `descriptor.requires` (`architecture` normalised: intel → x86_64, arm/arm64 → aarch64, armv7 →
armv7l; `os`; `devices`) and the proposal's `checks`: driver present, architecture, cpu cores, memory, and one manual
item per device kind (gpio/camera/serial/i2c/spi → add `runOptions.devices` and allow it on the device; wlan → nothing
to map; `can` → refused, needs host networking). With `--plan` the checks are evaluated against the onboarded
device's capabilities (CP-2). `[DEPENDENCIES] depends_on` (device level) → `rolloutAfterDevices` (apply those
plans first). `--arch` builds every listed CPU type; without it the device's architecture, else the host's.
Containers for several CPU types become one release per CPU type (version `<v>-<arch>`, the proposal picks the
device's).

## What does not map (the report lists each occurrence)

| v1 / compose | Level | Why / gap |
|---|---|---|
| `custom`, `none` workflows | error | no build on the device |
| `volumes` (bind / named), `tmpfs`, `-v`, `--mount` | error (warning with `--accept-lossy`: dropped) | the container driver mounts nothing: state is lost on update |
| `privileged`, `cap_add`, `security_opt`, `pid`/`ipc`/`userns_mode`, `sysctls`, `network_mode: host`, `--network host`, `--privileged` | error | not allowed by the container driver (S4) |
| `deploy.replicas > 1`, `scale`, `extends`, `include`, `secrets`, `configs`, `build.ssh/secrets` | error | one container per deployment; no secret/config distribution; flatten with `docker compose config` |
| component types other than docker/python (rust, cpp, arduino) | error | native binaries: EV-37; firmware: EV-38 |
| `os` other than linux/generic, `can` device | error | Linux drivers only; SocketCAN needs host networking |
| `ports`, `-p`, `--publish` | warning | no published ports: reach the container on its bridge IP (EV-27) |
| `networks`, `links`, service-name DNS | warning | every deployment runs on the default bridge; services do not resolve each other |
| `depends_on` (service and device) | warning | order note only; no ordering in V2 yet |
| `env_file` values | warning | baked into the signed image: do not put secrets there; per-device values → deployment `parameters` (`FLEX_PARAM_<NAME>`) |
| cpu / memory limits | warning/info | recorded, not enforced |
| `extra_hosts`, `dns*`, `cap_drop`, `shm_size`, `ulimits`, `read_only`, … | warning | not mapped |
| `container_name`, `labels`, `logging`, `hostname`, `stop_*`, `tty`, `stdin_open`, `-d`, `--rm`, `--name` | info | ignored (the engine names, labels and supervises containers) |
| compose `profiles` | info | a service whose profile is not activated by `[run] arguments --profile` is skipped, as `docker compose up` would |
| inline `#` comments, `key: value`, quoted values, missing sections | info/warning | tolerated (v1 keeps an inline comment in the value) |

Engine-side changes that would remove gaps (not done here): a non-HTTP liveness health type for the container driver
(no shim, images without python3); `platform()` for the container driver (one multi-arch container release instead
of one per CPU type); environment/ports/volumes/limits in `runOptions` or the compose profile (EV-27); deployment
ordering in the desired state; placement checks of `descriptor.requires` in the backend.

## CI recipe

```sh
# in the package's CI (never on the device); the release key comes from the CI secret store
pip install -e flexmodel            # or the flexmodel wheel
flex import-flexia . --version "$(git describe --tags --always)" --key "$FLEX_RELEASE_KEY_FILE" \
    --out build/flexia --publish "$FLEX_CLOUD_URL" --admin-token-file "$FLEX_ADMIN_TOKEN_FILE" --plan --json \
    > build/flexia/import.json
# review build/flexia/report.json (notes) and the plans; a second person approves and applies:
#   POST /api/v1/admin/plans/<planHash>/approve, then /apply
```

`--dry-run` maps and reports without building (a cheap PR check); `--device` limits the import to some devices.
Cross-CPU images: `docker buildx` with QEMU binfmt for Dockerfiles with `RUN` steps; without emulation only
Dockerfiles without `RUN` build for a foreign CPU (and the liveness shim's python3 is not verified).

## Tests

- `flexmodel/tests/test_flexia.py`: the v1 example `flexIA.ini` (RoboSAPIENS, verbatim in
  `flexmodel/tests/fixtures/flexia/`) and the v1 template; mapping functions; refusals (custom/none workflows,
  unsupported compose options, `--accept-lossy`, `--partial`); RoboSAPIENS dry run (order notes, device suffixes,
  checks); native payloads with wheels (lock, SBOM, signature), multi-arch native layers, the generated wrapper and
  the shim; a container release (Docker).
- `flexengine/tests/integration/flexia/test_flexia_e2e.py` (Docker): flcpy publisher (docker workflow,
  flexCommunicator from PyPI wheels, liveness shim), a two-service compose package, an application descriptor with two
  native python runnables and a docker runnable, the RoboSAPIENS example (`local`), and a crashing node: import →
  publish to the reference fake cloud → `--plan` → plan/apply → a real engine (`python -m flexengine2.testing.single`,
  container driver on real Docker + process driver) installs and runs them, health OK (the crash fails the gate).

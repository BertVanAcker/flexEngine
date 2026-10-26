# flexEngine: working notes

flexEngine 2 (import `flexengine2`, CLI `flexengine`, PyPI project `flexEngine`): the on-device deployment engine of
the flex* stack. Operator guide: `README.md`; contracts: `api/`; what changed from 0.6: `CHANGES.md`.

## Branches
- `master` is the default branch and holds 2.x. Work on `fe-<n>/<topic>` branches (e.g. `fe-4/run-state`), merged
  by PR; the required check is `ci-ok`.
- `v1.x` is the 0.6.x maintenance branch (flexEngine v1, still driven by the flexCloud console). Do not merge it into
  `master` or the other way round.
- Releases: tag `v2.X.Y[aN|rcN]` on `master` after bumping `src/flexengine2/__init__.py`; `release.yml` checks the
  tag against the version and publishes to PyPI (trusted publisher: workflow `release.yml`, environment `pypi` —
  keep both names). v1 releases are tagged `v0.6.*` on `v1.x`.

## Testing
- Before every push: `./run_tests.sh` (push tier: unit contract api security property integration-fast) in a venv
  with `pip install -e '.[dev,mcu,applab,plc,otel,relay]' openapi-spec-validator`. For changes to coverage-relevant
  code also `./run_tests.sh coverage`; for packaging, executors or long-running behaviour `./run_tests.sh nightly`
  (docker/podman cases skip without a usable runtime).
- CI (`.github/workflows/ci.yml`) runs the same layers on every PR and on `master`, plus the coverage gate and a wheel
  build + install smoke test; `ci-ok` aggregates them. `nightly.yml` runs the nightly tier, `release.yml` everything.
- No framework checkout is needed. Framework files the tests read are vendored in `tests/vectors/framework/`, probe
  P2's stand-in apps in `src/flexengine2/testing/apps/` (`tests/vectors/framework/SOURCES.json` names origin and
  commit); refresh them with `tools/vendor_framework.py --framework <checkout> --update`, never by hand. Cases that need the framework's code are marked `framework` / `flexmodel` (or skip at
  module level) and run with `FLEX_REPO=<checkout> FLEXMODEL_SRC=<checkout>/flexmodel/src ./run_tests.sh ...`.
- Hardware: desk runbooks in `hwtest/` (EV-HW-01…11); dry-run new or changed runbooks against the simulated desk
  (`hwtest/dryrun.py`) before handing them to the lab owner.

## Contracts
- Connection points stay `v1alpha1` (CP-5 driver SPI, CP-7 local API and CP-8 telemetry: `v1`). Within a version,
  changes are additive only (new optional keys, new enum values, new reason codes); never rename, remove or change
  the meaning of a field, route, reason code or CLI option. A breaking change is a new version.
- Generated files are regenerated, not edited: `api/cp-reference.md` (`api/tools/gen_cp_reference.py`),
  `docs/config-reference.md` (`python -m flexengine2.config.refdoc`), the reason-code registry
  (`api/tools/gen_reason_codes.py`), test vectors (`api/tools/gen_vectors.py`); staleness tests catch drift.
- Board and runtime differences live in drivers, behind the SPI; the reconciler and the wire format do not change
  per driver.

## Conventions
- Commit messages explain the why, not only the what. No model names in commits or code.
- Never force-push; no secrets in commits (keys in tests are generated or obvious test values).
- Keep CHANGES.md current: every user-visible change gets a line under the version in progress.

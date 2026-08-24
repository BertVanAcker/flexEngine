# Releasing flexEngine

This describes how to cut a release. It's for maintainers — end-user install
instructions live in [README.md](README.md#installation).

## One-time setup (already needed before the first release)

1. **PyPI trusted publisher.** On the `flexEngine` project's PyPI page,
   under *Publishing*, add a trusted publisher:
   - Owner: `BertVanAcker`
   - Repository: `flexEngine`
   - Workflow: `release.yml`
   - Environment: `pypi`

   This lets `.github/workflows/release.yml` publish via OIDC — no PyPI API
   token/secret needed. (If the project doesn't exist on PyPI yet, use
   PyPI's "pending publisher" flow instead, which lets you register the
   trusted publisher before the first release creates the project.)

2. **GitHub environment.** In the repo's *Settings → Environments*, make
   sure a `pypi` environment exists (GitHub creates it automatically the
   first time the workflow runs, but you may want to add protection rules,
   e.g. required reviewers, ahead of time).

## Cutting a release

1. **Bump the version** in [`flexEngine/__init__.py`](flexEngine/__init__.py):

   ```python
   VERSION = (0, 1, 0)
   ```

   `__version__` is derived from this tuple. This is the single source of
   truth for the package version — `setup.py` reads it from here, and the
   release workflow checks the git tag against it.

2. **Commit and push** the version bump to `master` as normal.

3. **Tag the release** and push the tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

   The tag must be `v` + the exact value of `__version__` (e.g. `__version__
   == "0.1.0"` → tag `v0.1.0`). A mismatch fails the release build with an
   explicit error rather than publishing the wrong version.

4. **Let the workflow run.** Pushing a `v*` tag triggers
   [`.github/workflows/release.yml`](.github/workflows/release.yml), which:
   - runs the full [`ci.yml`](.github/workflows/ci.yml) suite (Linux
     Redis-backed tests, macOS/Windows install smoke tests, emulated ARMv6
     smoke test),
   - verifies the tag matches `flexEngine.__version__`,
   - builds the sdist + wheel,
   - publishes them to PyPI,
   - creates a GitHub Release for the tag with the sdist, wheel,
     `scripts/install.sh`, and `scripts/install.ps1` attached.

   Watch it under the repo's *Actions* tab. If any step fails, nothing gets
   published — fix the issue and push a new tag (do not reuse a failed tag
   after force-pushing over it).

## Dry-running a build locally

To sanity-check packaging before tagging, without touching PyPI:

```bash
python -m pip install build
python -m build
unzip -l dist/*.whl        # confirm flexEngine/templates/*.tmpl is included

python -m venv /tmp/flexengine-check
/tmp/flexengine-check/bin/pip install dist/*.whl
/tmp/flexengine-check/bin/flexEngine init /tmp/flexengine-check-ws
```

## Troubleshooting

- **"Tag 'vX.Y.Z' does not match flexEngine.__version__"** — you tagged
  before bumping (or forgot to bump) `flexEngine/__init__.py`. Delete the
  tag locally and on the remote, bump the version, and re-tag.
- **PyPI publish step fails with a permissions/OIDC error** — the trusted
  publisher isn't configured correctly on PyPI (owner/repo/workflow/
  environment name must match exactly), or the `pypi` GitHub environment is
  missing/misconfigured.

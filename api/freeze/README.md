# Freeze gate (FE-3)

flexEngine 2.0 freezes its public surface before the release: the connection-point specs, the reason codes, the CLI,
the engine configuration and the test vectors. After the freeze a 2.x change is either an **addition** with a row in
`CHANGES.md` or a **new version** of the contract (`v1alpha2`, SPI 2.0, `local-api.v2`; api/README.md). The model is
flexCommunicator's v2 freeze (V2-F1…F6). The JSON files here are the recorded baselines; `tools/freeze.py` collects
the same surface from the tree and compares.

| Gate | What is recorded | Golden file |
|---|---|---|
| FE-F1 | every machine-readable spec in `src/flexengine2/api/` (CP-1…CP-8 schemas, `openapi-southbound.yaml`, `local-api.v1.openapi.yaml`, the reason-code registry) and its sha256 | `files.json` |
| FE-F2 | the structure of those specs, leaf by leaf: every JSON Schema node (types, enums, patterns, `required`, `additionalProperties`, `$ref`s), every OpenAPI path, method, parameter (by `name@in`), request body and response with its media types and schema. Documentation keys (`description`, `title`, `summary`, `examples`, `$comment`) are not recorded. Covers the local API (CP-7), the driver SPI (CP-5, `driver-spi.v1.schema.json`, whose member table equals `drivers/base.py`) and telemetry (CP-8, `telemetry.v1.schema.json`) | `specs.json` |
| FE-F3 | the reason-code registry: per code its `field`, `stage`, `profile`, `meaning` and `retired` flag; the field map. Report templates may change (a text keeps its code), meanings may not | `codes.json` |
| FE-F4 | the `flexengine` CLI, from its argparse parser: every command and sub-command, every option (flags, required, action, nargs, default, type, choices, dest) and positional. Help texts are not recorded | `cli.json` |
| FE-F5 | the engine configuration schema (`src/flexengine2/config/schema.json`, `engine.yaml`), as FE-F2 | `config.json` |
| FE-F6 | the CP-3/CP-4 test vectors in `api/vectors/` and their sha256 (vectors never change; new ones are added) | `vectors.json` |

    python tools/freeze.py check            # all gates (CI job `freeze`, also tests/api/test_freeze.py)
    python tools/freeze.py check FE-F4      # one gate
    python tools/freeze.py list specs       # the keys a gate collects now, to write a CHANGES row

## How `check` decides

- An entry that is **gone** fails: a removed spec file, schema node, route, response, reason code, CLI option,
  config key or vector.
- An entry whose value **changed** fails: a retyped field, a tightened pattern, a field added to `required`, a new
  keyword on an existing node (a new `maxLength` is a tightening), a changed meaning, a renamed flag, a changed
  default, a changed vector. A reordered or shortened `enum` fails. FE-F1 only notes a changed file hash; FE-F2
  decides whether that change is additive.
- An entry that is **new** passes only when a row of a contract-change table in `CHANGES.md` names its gate and
  matches it. New means a new child of a container (a property, `$defs` entry, path, method, response, media type,
  parameter, `oneOf` / `anyOf` alternative, reason code, command, option, SPI member), a new `enum` value
  (`<key>=<value>`) or a code marked `retired`. A new parameter or option that is required is a change, not an
  addition: it breaks existing callers.
- Every golden file names the date it was recorded on; `CHANGES.md` must have a row of that date naming the gate.

A contract-change table has the header `| Date | Gate | Entries | Why |`. The Entries column holds back-quoted keys
or globs (`fnmatch`, `*` crosses `/`); `all` marks a re-recorded baseline and admits nothing by itself. Keys are what
a failing check prints, for example:

| Gate | Key |
|---|---|
| FE-F1 | `telemetry.v1.schema.json` |
| FE-F2 | `desired-state.v1alpha1.schema.json:/$defs/ApplicationDeployment/properties/x-flex-runState`, `desired-state.v1alpha1.schema.json:/$defs/DriverProfile/enum=os-rauc`, `openapi-southbound.yaml:/paths/~1api~1v1~1devices~1{deviceId}~1events` |
| FE-F3 | `codes/FE-BLD-005` |
| FE-F4 | `flexengine/commands/deployments/options/--wide` |
| FE-F5 | `schema.json:/properties/devices/items/properties/drivers/properties/container/properties/composeCommand` |
| FE-F6 | `release/cases.json` |

So a 2.1 addition lands with one row such as

    | 2026-11-20 | FE-F1, FE-F2, FE-F3 | `status.v1alpha1.schema.json:/$defs/*/properties/queuedAt`, `codes/FE-REC-013` | 2.1: queued-at timestamp and its reason code (additions only) |

and the golden files stay as they are until the next release re-records them; then the additions are part of the
baseline.

## Re-recording

Re-recording is explicit, never automatic: `check` never writes. At a release (or for a change agreed before the
`v2.0.0` tag):

    python tools/freeze.py record all --why "baseline of 2.1.0"
    python tools/freeze.py record FE-F4 --why "..."          # one gate

`record` prints the `CHANGES.md` row to add (date, gates, `all`, why); without that row `check` fails. A removal or
a change recorded this way after `v2.0.0` is a break of the 2.0 contract: it belongs to a new contract version, and
the row says which.

## What the gate does not hold

Behaviour behind an unchanged spec (the contract tests in `tests/api/`, the driver conformance suite in
`tests/contract/` and the vectors hold that); the prose specs in `api/*.md` (FE-F2 holds their machine-readable
parts); CP-6 child-device protocols (prose only, `cp6-children.md`; their wire is held by the driver suites); the
output format of `--json` CLI commands other than `status` (`cli-status.v1.schema.json`); log lines in `logFormat:
text`.

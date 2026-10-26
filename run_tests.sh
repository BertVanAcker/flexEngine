#!/usr/bin/env bash
# flexEngine 2 test suite, one command (engine plan §7.1). Used by CI and on the lab LattePanda.
#
#   ./run_tests.sh [layer ...]        default: push
#
# Layers (plan §7.2):
#   unit              tests/unit                                   layer 1
#   contract          tests/contract, push tier (fake executors)   layer 2
#   contract-nightly  tests/contract on real executors (docker, podman, qemu; missing ones skip)
#   api               tests/api                                    layer 3
#   security          tests/security                               layer 4
#   property          tests/property, push budget                  layer 5
#   property-nightly  tests/property, nightly budget (>= 200 schedules)
#   integration-fast  tests/integration -m "not slow"              layer 6, push subset
#   integration       tests/integration, everything incl. the docker-backed cases   layer 6
#   packaging         tests/packaging (docker cases skip without docker)  layer 8
#   coverage          layers 1-6 under coverage.py + gate (§7.3) -> <results>/coverage-summary.json
#                     (FLEX_COVERAGE_LAYERS overrides the layers, e.g. the push subset in CI)
# Groups:
#   push     = unit contract api security property integration-fast
#   nightly  = contract-nightly property-nightly integration packaging
#   all      = push + nightly
#
# Environment:
#   PYTHON            interpreter with flexengine2's test deps (default: .venv/bin/python, python3)
#   FLEX_RESULTS_DIR  where JUnit XML, run-summary.json and generated reports go (default: results/latest, gitignored)
#   FLEXMODEL_SRC     optional: flexmodel's src/ (not on PyPI); put on PYTHONPATH for the relay cases, which skip without it
#   FLEX_BASETEMP     parent dir for pytest --basetemp (one subdir per layer; default: pytest's own)
#   PYTEST_ARGS       extra pytest arguments (e.g. "-x -k scenario")
#
# The checkout's sources always win over an installed copy: PYTHONPATH starts with src/.
# Exit status: 0 when every selected layer passed, else 1.
set -o pipefail   # no -u: macOS bash 3.2 treats empty arrays as unset

FE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${PYTHON:-}" ]]; then
  for p in "$FE/.venv/bin/python"; do
    if [[ -x "$p" ]]; then PYTHON="$p"; break; fi
  done
  PYTHON="${PYTHON:-python3}"
fi
export PYTHONPATH="$FE/src${FLEXMODEL_SRC:+:$FLEXMODEL_SRC}${PYTHONPATH:+:$PYTHONPATH}"
export FLEX_ALLOW_PLAIN_HTTP=1          # the reference fake cloud speaks plain HTTP (lab flag, decision DA-4)
RES="${FLEX_RESULTS_DIR:-$FE/results/latest}"
mkdir -p "$RES"
export FLEX_RESULTS_DIR="$RES"     # tests that write reports or metrics put them here
read -r -a EXTRA <<< "${PYTEST_ARGS:-}" || true

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }

expand() {
  case "$1" in
    push) echo unit contract api security property integration-fast ;;
    nightly) echo contract-nightly property-nightly integration packaging ;;
    all) echo unit contract api security property integration-fast contract-nightly property-nightly integration packaging ;;
    *) echo "$1" ;;
  esac
}

# test path and marker expression of one layer (paths relative to the repository root)
layer_path() {
  case "$1" in
    unit|contract|api|security|property|packaging) echo "tests/$1" ;;
    contract-nightly) echo "tests/contract" ;;
    property-nightly) echo "tests/property" ;;
    integration|integration-fast) echo "tests/integration" ;;
    *) return 1 ;;
  esac
}
layer_mark() {
  case "$1" in
    contract-nightly) echo "docker or podman or qemu" ;;
    property-nightly) echo "slow" ;;
    integration-fast) echo "not slow" ;;
    integration) echo "slow or not slow" ;;   # any -m opts into the docker-backed nightly cases (rauc, arduino-ota)
  esac
}

COMMIT="$(git -C "$FE" rev-parse --short HEAD 2>/dev/null || echo unknown)"

run_layer() {
  local layer="$1" path mark t0 code
  path="$(layer_path "$layer")" || { echo "unknown layer: $layer" >&2; usage >&2; return 2; }
  mark="$(layer_mark "$layer")"
  local args=("$path")
  if [[ -n "$mark" ]]; then args+=(-m "$mark"); fi
  if [[ -n "${FLEX_BASETEMP:-}" ]]; then mkdir -p "$FLEX_BASETEMP"; args+=(--basetemp "$FLEX_BASETEMP/$layer"); fi
  local profile=push
  if [[ "$layer" == property-nightly ]]; then profile=nightly; fi
  echo "=== $layer: pytest ${args[*]}"
  t0=$(date +%s)
  (cd "$FE" && FLEX_PROPERTY_PROFILE=$profile "$PYTHON" -m pytest -q -p no:cacheprovider "${args[@]}" \
     --junitxml="$RES/junit-$layer.xml" -o junit_suite_name="flexengine-$layer" ${EXTRA[@]+"${EXTRA[@]}"})
  code=$?
  "$PYTHON" "$FE/tools/run_summary.py" "$RES" "$layer" "$code" "$(( $(date +%s) - t0 ))" "$COMMIT" "$PYTHON" \
    | tee -a "$RES/summary.txt"
  return $code
}

run_coverage() {
  # Layers 1-6 together (plan §7.3), engine subprocesses included (coverage's subprocess patch, .coveragerc).
  local cov="$FE/tests/work/coverage" code=0 l
  rm -rf "${cov:?}" && mkdir -p "$cov"
  export COVERAGE_RCFILE="$FE/.coveragerc" COVERAGE_FILE="$cov/.coverage"
  for l in ${FLEX_COVERAGE_LAYERS:-unit contract contract-nightly api security property integration}; do
    local args=("$(layer_path "$l")") mark
    mark="$(layer_mark "$l")"
    if [[ -n "$mark" ]]; then args+=(-m "$mark"); fi
    if [[ -n "${FLEX_BASETEMP:-}" ]]; then mkdir -p "$FLEX_BASETEMP"; args+=(--basetemp "$FLEX_BASETEMP/cov-$l"); fi
    echo "=== coverage: $l"
    (cd "$FE" && FLEX_PROPERTY_PROFILE=push "$PYTHON" -m coverage run -m pytest -q -p no:cacheprovider "${args[@]}" \
       --junitxml="$RES/junit-cov-$l.xml" ${EXTRA[@]+"${EXTRA[@]}"}) || code=1
  done
  (cd "$FE" && "$PYTHON" -m coverage combine -q && "$PYTHON" -m coverage json -q -o "$cov/coverage.json") || return 1
  "$PYTHON" "$FE/tools/coverage_report.py" "$cov/coverage.json" --out "$RES/coverage-summary.json" || code=1
  return $code
}

LAYERS=()
if [[ $# -eq 0 ]]; then set -- push; fi
for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
    *) for x in $(expand "$a"); do LAYERS+=("$x"); done ;;
  esac
done

echo "flexengine run_tests: python=$PYTHON commit=$COMMIT results=$RES layers=${LAYERS[*]}"
"$PYTHON" -c "import flexengine2, sys; p = flexengine2.__file__; print('flexengine2 from', p); sys.exit(0 if p.startswith('$FE/src/') else 1)" \
  || { echo "flexengine2 is not imported from this checkout ($FE/src)" >&2; exit 1; }

rc=0
for l in "${LAYERS[@]}"; do
  if [[ "$l" == coverage ]]; then run_coverage || rc=1; else run_layer "$l" || rc=1; fi
done
exit $rc

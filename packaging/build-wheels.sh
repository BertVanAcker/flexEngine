#!/usr/bin/env bash
# flexEngine wheels + offline wheelhouses per target (EV-08).
#
#   packaging/build-wheels.sh [--out dist] [--python PY] [--targets LIST] [--extras LIST]
#                                        [--arm64-extras LIST] [--no-docker] [--no-bundle] [--clean]
#                                        [--wheel FILE] [--bundle-only]
#
#   --wheel         use this engine wheel instead of building one (release.yml: the wheel that goes to PyPI, so the
#                   bundle installs exactly the published build); copied into --out
#   --bundle-only   no wheelhouse downloads: bundle the engine wheel and the wheelhouses already in --out
#
#   --targets       space/comma list of <arch>-<pyver> (default "x86_64-3.12 x86_64-3.13 aarch64-3.11 aarch64-3.13")
#                   x86_64-3.12  = LattePanda 3 Delta (Ubuntu 24.04);  aarch64-3.11/3.13 = UNO Q Linux side
#                   (Debian 12 / 13 -- check `python3 --version` on the board); x86_64-3.13 = this dev venv / CI.
#   --extras        extras for the x86_64 wheelhouses (default mcu,applab,plc,otel)
#   --arm64-extras  extras for the aarch64 wheelhouses (default applab,otel: the UNO Q runs the applab driver; mcu/plc
#                   need crcmod/pyads, which PyPI ships as sdists only -- see below)
#
# Output (dist/ is gitignored):
#   dist/flexengine-<ver>-py3-none-any.whl             the engine itself (pure Python)
#   dist/wheelhouse/linux_<arch>-cp<XY>/*.whl           every dependency, pinned by packaging/requirements-lock.txt
#                                                       (used as constraints), + setuptools/wheel for --from-source
#   dist/wheelhouse/linux_<arch>-cp<XY>/sdist/          sdist-only deps that could not be prebuilt for that target
#   dist/flexengine-<ver>-linux_<arch>.tar.gz           offline bundle: packaging/ + wheel + that arch's wheelhouses
#   dist/SHA256SUMS
#
# Binary wheels come from `pip download --only-binary=:all: --platform manylinux_*_<arch> --python-version X.Y`.
# crcmod (via smp, [mcu]) and pyads ([plc]) have no wheels on PyPI: they are built here with `pip wheel` inside a
# python:X.Y container of the target arch (offline, --network none) when Docker can run that platform (x86_64 here;
# aarch64 needs qemu-user binfmt). Otherwise their sdists go to sdist/ and the target needs gcc + make + python3-dev
# (pyads compiles adslib.so). The flexengine wheel build needs network access once (setuptools build isolation).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$(cd "$HERE/.." && pwd)"                  # repository root
OUT="$PKG/dist"
PY="${PYTHON:-}"
[[ -n "$PY" ]] || { [[ -x "$PKG/.venv/bin/python" ]] && PY="$PKG/.venv/bin/python" || PY=python3; }
TARGETS="x86_64-3.12 x86_64-3.13 aarch64-3.11 aarch64-3.13"
EXTRAS="mcu,applab,plc,otel"
ARM_EXTRAS="applab,otel"
DOCKER=1; BUNDLE=1; CLEAN=0; GIVEN_WHEEL=""; BUNDLE_ONLY=0
SDIST_ONLY=(crcmod pyads)
LOCK="$HERE/requirements-lock.txt"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --targets) TARGETS="${2//,/ }"; shift 2 ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    --arm64-extras) ARM_EXTRAS="$2"; shift 2 ;;
    --no-docker) DOCKER=0; shift ;;
    --no-bundle) BUNDLE=0; shift ;;
    --clean) CLEAN=1; shift ;;
    --wheel) GIVEN_WHEEL="$2"; shift 2 ;;
    --bundle-only) BUNDLE_ONLY=1; shift ;;
    -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
say() { printf '== %s\n' "$*"; }
PIP=("$PY" -m pip --disable-pip-version-check)
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
[[ $CLEAN == 1 ]] && rm -rf "$OUT/wheelhouse" "$OUT"/flexengine-*.whl "$OUT"/*.tar.gz "$OUT/SHA256SUMS"
VER="$("$PY" -c 'import re,sys; print(re.search(r"__version__ = \"([^\"]+)\"", open(sys.argv[1]).read()).group(1))' \
  "$PKG/src/flexengine2/__init__.py")"

# ---- 1. the engine wheel ----------------------------------------------------------------------------------------------
if [[ -n "$GIVEN_WHEEL" ]]; then
  [[ "$(basename "$GIVEN_WHEEL")" == "flexengine-$VER-py3-none-any.whl" ]] \
    || { echo "--wheel $GIVEN_WHEEL is not flexengine-$VER-py3-none-any.whl (the checkout's version)" >&2; exit 2; }
  say "flexEngine $VER wheel (given)"
  if [[ "$(cd "$(dirname "$GIVEN_WHEEL")" && pwd)/$(basename "$GIVEN_WHEEL")" != "$OUT/$(basename "$GIVEN_WHEEL")" ]]; then
    rm -f "$OUT"/flexengine-*.whl; cp "$GIVEN_WHEEL" "$OUT/"
  fi
elif [[ $BUNDLE_ONLY == 1 ]]; then
  say "flexEngine $VER wheel (already in $OUT)"
else
  say "flexEngine $VER wheel"
  rm -f "$OUT"/flexengine-*.whl
  tmpb="$(mktemp -d)"; trap 'rm -rf "$tmpb"' EXIT
  cp -r "$PKG/pyproject.toml" "$PKG/README.md" "$PKG/src" "$tmpb/"   # clean tree: no build/ or egg-info in the checkout
  find "$tmpb/src" -name '__pycache__' -prune -exec rm -rf {} + ; find "$tmpb/src" -name '*.egg-info' -prune -exec rm -rf {} +
  "${PIP[@]}" wheel -q --no-deps -w "$OUT" "$tmpb"
fi
WHEEL="$OUT/flexengine-$VER-py3-none-any.whl"
[[ -f "$WHEEL" ]] || { echo "no engine wheel $WHEEL" >&2; exit 1; }
echo "   $WHEEL"

# ---- 2. wheelhouses ---------------------------------------------------------------------------------------------------
in_lock() { grep -iE "^$1==" "$LOCK" | head -1; }
plat_args() {  # manylinux tags a glibc >= 2.35 target accepts, newest first, + plain linux_<arch> (our own builds)
  local a="$1" t
  for t in manylinux_2_35 manylinux_2_34 manylinux_2_31 manylinux_2_28 manylinux_2_27 manylinux_2_24 manylinux_2_17 \
           manylinux2014 linux; do printf -- '--platform\n%s_%s\n' "$t" "$a"; done
}
docker_can() {  # docker_can <arch>: can Docker run a linux/<arch> container here?
  [[ $DOCKER == 1 ]] && command -v docker >/dev/null || return 1
  local p; [[ $1 == x86_64 ]] && p=linux/amd64 || p=linux/arm64
  docker run --rm --network none --platform "$p" "python:$2" true >/dev/null 2>&1
}
for tgt in $TARGETS; do
  [[ $BUNDLE_ONLY == 1 ]] && break
  arch="${tgt%-*}"; pyv="${tgt#*-}"; cp="cp${pyv/./}"
  ex="$EXTRAS"; [[ $arch == aarch64 ]] && ex="$ARM_EXTRAS"
  WH="$OUT/wheelhouse/linux_${arch}-${cp}"
  say "wheelhouse $tgt [$ex] -> $WH"
  rm -rf "$WH"; mkdir -p "$WH"
  mapfile -t PLAT < <(plat_args "$arch")
  XDL=(download -q --only-binary=:all: --python-version "$pyv" --implementation cp --abi "$cp" --abi abi3 --abi none
       "${PLAT[@]}" -c "$LOCK" -d "$WH")
  # setuptools + wheel: build backends for --from-source and for the sdist-only deps
  "${PIP[@]}" "${XDL[@]}" --no-deps setuptools wheel
  # which sdist-only dists does this extras set pull in? (crcmod <- smp <- smpclient [mcu]; pyads [plc])
  need=()
  [[ ",$ex," == *",mcu,"* ]] && need+=(crcmod)
  [[ ",$ex," == *",plc,"* ]] && need+=(pyads)
  if [[ ${#need[@]} -gt 0 ]]; then
    mkdir -p "$WH/sdist"
    for n in "${need[@]}"; do
      pin="$(in_lock "$n")"; [[ -n "$pin" ]] || pin="$n"
      "${PIP[@]}" download -q --no-deps --no-binary=:all: -d "$WH/sdist" "$pin"
    done
    if docker_can "$arch" "$pyv"; then
      plat=linux/amd64; [[ $arch == aarch64 ]] && plat=linux/arm64
      echo "   building ${need[*]} in python:$pyv ($plat, offline)"
      docker run --rm --network none --platform "$plat" -v "$WH:/wh" "python:$pyv" sh -ec \
        'pip -q --disable-pip-version-check wheel --no-deps --no-index --find-links /wh -w /wh /wh/sdist/*.tar.gz
         chown -R '"$(id -u):$(id -g)"' /wh'
      rm -rf "$WH/sdist"
    else
      echo "   NOTE: cannot run linux/$arch containers here: ${need[*]} stay as sdists (target needs gcc, make, python3-dev)"
    fi
  fi
  if [[ ! -d "$WH/sdist" ]]; then
    # full resolve: the engine wheel + extras, every transitive dep as a binary wheel for that target
    "${PIP[@]}" "${XDL[@]}" --find-links "$WH" "${WHEEL}[${ex}]"
  else
    # sdists present (pip refuses them with --platform): pinned closure from the lock, no further resolution.
    # Deps that only exist for other Pythons than the lock's are not covered -- check the install on the target.
    mapfile -t pins < <(grep -vE '^(#|$)' "$LOCK" | grep -viE "^($(IFS='|'; echo "${SDIST_ONLY[*]}"))==")
    "${PIP[@]}" "${XDL[@]}" --no-deps "${pins[@]}"
  fi
  rm -f "$WH"/flexengine-*.whl
  n=$(find "$WH" -maxdepth 1 -name '*.whl' | wc -l); s=$(find "$WH" -name '*.tar.gz' | wc -l)
  echo "   $n wheels, $s sdists, $(du -sh "$WH" | cut -f1)"
done

# ---- 3. offline bundles + checksums ------------------------------------------------------------------------------------
if [[ $BUNDLE == 1 ]]; then
  for arch in $(for t in $TARGETS; do echo "${t%-*}"; done | sort -u); do
    b="$OUT/flexengine-$VER-linux_$arch.tar.gz"
    if [[ $BUNDLE_ONLY == 1 ]] && ! compgen -G "$OUT/wheelhouse/linux_${arch}-*" >/dev/null; then
      echo "   no wheelhouse for $arch in $OUT/wheelhouse: no $arch bundle"; continue
    fi
    say "bundle $b"
    stage="$(mktemp -d)"; top="flexengine-$VER-linux_$arch"; d="$stage/$top"
    mkdir -p "$d/wheelhouse"
    cp -r "$HERE" "$d/packaging"; rm -rf "$d/packaging/__pycache__"
    cp "$WHEEL" "$d/"
    for w in "$OUT/wheelhouse/linux_${arch}-"*; do [[ -d "$w" ]] && cp -r "$w" "$d/wheelhouse/"; done
    tar -C "$stage" -czf "$b" "$top"
    rm -rf "$stage"
  done
fi
sums="$(cd "$OUT" && find . -type f \( -name '*.whl' -o -name '*.tar.gz' \) -print0 | sort -z | xargs -0 sha256sum)"
printf '%s\n' "$sums" > "$OUT/SHA256SUMS"
say "done: $OUT ($(wc -l < "$OUT/SHA256SUMS") files in SHA256SUMS)"
echo "   install (LattePanda): sudo packaging/install.sh --wheel $WHEEL --wheelhouse $OUT/wheelhouse --extras $EXTRAS"

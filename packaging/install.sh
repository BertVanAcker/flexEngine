#!/usr/bin/env bash
# flexEngineV2 installer (EV-08): venv under PREFIX, `flex` service user, /etc/flexengine + /var/lib/flexengine,
# systemd unit flexengine.service (Type=notify + watchdog). Idempotent: every step checks first and only changes what
# differs; re-running with the same inputs reports "changes: 0". Config and data are never overwritten.
#
#   sudo packaging/install.sh [--wheel FILE | --from-source DIR] [--wheelhouse DIR] [--extras mcu,plc,applab,otel]
#                             [--prefix /opt/flexengine] [--python python3] [--device-id ID] [--groups LIST]
#                             [--enable] [--start] [--no-service] [--root DIR] [--dry-run]
#   sudo packaging/install.sh --uninstall [--purge] [--prefix ...] [--root DIR] [--dry-run]
#
#   --wheel FILE       install this flexengine-v2 wheel (dist/flexengine_v2-<ver>-py3-none-any.whl)
#   --from-source DIR  build + install from a checkout's flexengine/ directory (non-editable)
#                      default: the wheel next to this script (offline bundle), else the checkout this script is in
#   --wheelhouse DIR   offline install: pip --no-index --find-links DIR (DIR or DIR/linux_<arch>-cp<XY>, as written by
#                      build-wheels.sh); default: ../wheelhouse of an offline bundle. Without it pip uses the index.
#   --extras LIST      optional dependency sets (mcu, plc, applab, otel); default none. relay needs flexmodel (no PyPI)
#   --groups LIST      SupplementaryGroups for the unit (drop-in 10-groups.conf); default: those of dialout,docker
#                      that exist on this host. "none" = no drop-in.
#   --enable / --start enable the unit (boot) / enable + start (or restart after an upgrade). Default: installed, not
#                      enabled -- put the onboarding token into /etc/flexengine/tokens/ and edit engine.yaml first.
#   --no-service       no service user, no systemd unit (Mac / dev hosts); venv + dirs only
#   --root DIR         stage everything under DIR (tests, image builds): no useradd/chown/systemctl/apt
#   --uninstall        stop + disable the unit, remove it and the venv; keeps /etc/flexengine and /var/lib/flexengine
#   --purge            with --uninstall: also remove config, data and the flex user/group
#   --dry-run          print the plan, change nothing
#
# Lock: packaging/requirements-lock.txt is passed to pip as constraints (-c); regenerate with packaging/gen-lock.py.
set -euo pipefail
INSTALLER_VERSION=1
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/flexengine; ETC=/etc/flexengine; STATE=/var/lib/flexengine; UNITDIR=/etc/systemd/system
SUSER=flex; SGROUP=flex; UNIT=flexengine.service; TUNIT=flexengine@.service
WHEEL=""; SRC=""; WHOUSE=""; EXTRAS=""; PY=python3; DEVICE_ID=""; GROUPS_OPT="auto"
DRY=0; NOSVC=0; ROOT=""; MODE=install; PURGE=0; ENABLE=0; START=0
LOCK="$HERE/requirements-lock.txt"; TEMPLATE="$HERE/engine.yaml.template"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel) WHEEL="$2"; shift 2 ;;
    --from-source) SRC="$2"; shift 2 ;;
    --wheelhouse) WHOUSE="$2"; shift 2 ;;
    --extras) EXTRAS="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --groups) GROUPS_OPT="$2"; shift 2 ;;
    --enable) ENABLE=1; shift ;;
    --start) ENABLE=1; START=1; shift ;;
    --no-service) NOSVC=1; shift ;;
    --root) ROOT="$2"; shift 2 ;;
    --uninstall) MODE=uninstall; shift ;;
    --purge) PURGE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ $PURGE == 1 && $MODE != uninstall ]] && { echo "install.sh: --purge needs --uninstall" >&2; exit 2; }
[[ -n "$WHEEL" && -n "$SRC" ]] && { echo "install.sh: --wheel and --from-source are exclusive" >&2; exit 2; }
[[ "$PREFIX" == /* ]] || { echo "install.sh: --prefix must be absolute" >&2; exit 2; }
if [[ -n "$ROOT" ]]; then
  [[ "$ROOT" == /* ]] || ROOT="$PWD/$ROOT"
  [[ -d "$ROOT" ]] && ROOT="$(cd "$ROOT" && pwd)"; [[ "$ROOT" == / ]] && ROOT=""
fi
LIVE=1; [[ -n "$ROOT" ]] && LIVE=0           # LIVE=1: touch users, ownership, systemd, apt of this host
if [[ $LIVE == 1 && $NOSVC == 0 && $(uname -s) != Linux ]]; then
  echo "install.sh: no systemd on $(uname -s): use --no-service (or pip install -e 'flexengine[dev]', see README)" >&2
  exit 2
fi
OWN=0; [[ $LIVE == 1 && $EUID -eq 0 ]] && OWN=1   # set owner/group (root only)
if [[ $LIVE == 1 && $NOSVC == 0 && $EUID -ne 0 && $DRY == 0 ]]; then
  echo "install.sh: run as root (sudo), or use --no-service / --root DIR / --dry-run" >&2; exit 1
fi
R_PREFIX="$ROOT$PREFIX"; R_ETC="$ROOT$ETC"; R_STATE="$ROOT$STATE"; R_UNITDIR="$ROOT$UNITDIR"

# ---- helpers -----------------------------------------------------------------------------------------------------------
c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
[[ -t 1 ]] || { c_ok=""; c_warn=""; c_err=""; c_off=""; }
CHANGES=0
step() { printf '%s== %s%s\n' "$c_ok" "$*" "$c_off"; }
ok()   { printf '   ok    %s\n' "$*"; }
warn() { printf '   %sWARN%s  %s\n' "$c_warn" "$c_off" "$*"; }
die()  { printf '   %sFAIL%s  %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
run()  {  # a change: printed, counted, executed unless --dry-run
  CHANGES=$((CHANGES + 1))
  if [[ $DRY == 1 ]]; then printf '   [dry-run] %s\n' "$*"; else printf '   + %s\n' "$*"; "$@"; fi
}
sha() { if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi; }
fmode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
fowner() { stat -c '%U:%G' "$1" 2>/dev/null || stat -f '%Su:%Sg' "$1"; }
ensure_dir() {  # ensure_dir <path> <mode> <owner> <group>
  local d="$1" m="$2" o="$3" g="$4"
  if [[ ! -d "$d" ]]; then
    if [[ $OWN == 1 ]]; then run install -d -m "$m" -o "$o" -g "$g" "$d"; else run install -d -m "$m" "$d"; fi
    return 0
  fi
  local fixed=0
  [[ "$(fmode "$d")" == "${m#0}" ]] || { run chmod "$m" "$d"; fixed=1; }
  if [[ $OWN == 1 && "$(fowner "$d")" != "$o:$g" ]]; then run chown "$o:$g" "$d"; fixed=1; fi
  if [[ $fixed == 0 ]]; then if [[ $OWN == 1 ]]; then ok "$d ($m $o:$g)"; else ok "$d ($m)"; fi; fi
}
write_file() {  # write_file <path> <mode> <owner> <group> [keep]   content on stdin; keep = never replace an existing file
  local f="$1" m="$2" o="$3" g="$4" keep="${5:-}" tmp
  if [[ -n "$keep" && -e "$f" ]]; then ok "$f kept (exists, never overwritten)"; cat >/dev/null; return 0; fi
  tmp="$(mktemp)"; cat > "$tmp"
  if [[ -f "$f" ]] && cmp -s "$tmp" "$f" && [[ "$(fmode "$f")" == "${m#0}" ]] && \
     { [[ $OWN == 0 ]] || [[ "$(fowner "$f")" == "$o:$g" ]]; }; then
    ok "$f unchanged"; rm -f "$tmp"; return 0
  fi
  CHANGES=$((CHANGES + 1))
  if [[ $DRY == 1 ]]; then printf '   [dry-run] write %s (%s bytes, %s)\n' "$f" "$(wc -c < "$tmp" | tr -d ' ')" "$m"
  else
    printf '   + write %s (%s)\n' "$f" "$m"
    mkdir -p "$(dirname "$f")"
    if [[ $OWN == 1 ]]; then install -m "$m" -o "$o" -g "$g" "$tmp" "$f"; else install -m "$m" "$tmp" "$f"; fi
  fi
  rm -f "$tmp"
}
remove() {  # remove <path>...: rm -rf what exists
  local p; for p in "$@"; do if [[ -e "$p" || -L "$p" ]]; then run rm -rf "$p"; else ok "$p absent"; fi; done
}
have_systemd() { [[ $LIVE == 1 && $NOSVC == 0 ]] && command -v systemctl >/dev/null && [[ -d /run/systemd/system ]]; }
sctl() { if have_systemd; then run systemctl "$@"; else ok "systemctl $* skipped (no running systemd${ROOT:+, --root})"; fi; }
venv_version() { [[ -x "$R_PREFIX/bin/flexengine" ]] && "$R_PREFIX/bin/flexengine" version 2>/dev/null || true; }
summary() {
  if [[ $DRY == 1 ]]; then step "summary ($MODE, dry-run)"; else step "summary ($MODE)"; fi
  printf '   %-14s %s\n' "$@"
  if [[ $DRY == 1 ]]; then printf '   changes: %d planned, nothing changed (dry-run)\n' "$CHANGES"
  else printf '   changes: %d\n' "$CHANGES"; fi
}

# ======================================================================================================================
if [[ $MODE == uninstall ]]; then
  step "uninstall flexengine (prefix $PREFIX${ROOT:+, root $ROOT})"
  if have_systemd && [[ -f "$R_UNITDIR/$UNIT" ]]; then
    if systemctl is-active -q flexengine 2>/dev/null; then sctl stop flexengine; else ok "flexengine not running"; fi
    if systemctl is-enabled -q flexengine 2>/dev/null; then sctl disable flexengine; else ok "flexengine not enabled"; fi
    for i in $(systemctl list-units --plain --no-legend 'flexengine@*' 2>/dev/null | awk '{print $1}'); do sctl stop "$i"; done
  fi
  remove "$R_UNITDIR/$UNIT" "$R_UNITDIR/$TUNIT" "$R_UNITDIR/$UNIT.d/10-groups.conf" "$R_UNITDIR/$TUNIT.d/10-groups.conf"
  for d in "$R_UNITDIR/$UNIT.d" "$R_UNITDIR/$TUNIT.d"; do
    [[ -d "$d" ]] && [[ -z "$(ls -A "$d")" ]] && run rmdir "$d"
  done
  have_systemd && sctl daemon-reload
  if [[ -e "$R_PREFIX" ]]; then
    [[ -f "$R_PREFIX/.flexengine-install" || -f "$R_PREFIX/pyvenv.cfg" ]] || \
      die "$R_PREFIX is not a flexengine venv (no .flexengine-install / pyvenv.cfg): not removing it"
    remove "$R_PREFIX"
  else ok "$R_PREFIX absent"; fi
  if [[ $PURGE == 1 ]]; then
    step "purge config, data, user"
    remove "$R_ETC" "$R_STATE"
    if [[ $LIVE == 1 && $NOSVC == 0 ]]; then
      if getent passwd "$SUSER" >/dev/null; then run userdel "$SUSER"; else ok "user $SUSER absent"; fi
      if getent group "$SGROUP" >/dev/null; then run groupdel "$SGROUP"; else ok "group $SGROUP absent"; fi
    fi
    summary "venv" "removed" "unit" "removed" "config" "removed ($ETC)" "data" "removed ($STATE)"
  else
    summary "venv" "removed" "unit" "removed" "config" "kept ($ETC)" "data" "kept ($STATE)" \
            "user" "kept ($SUSER; --purge removes it)"
  fi
  exit 0
fi

# ======================================================================================================================
# ---- inputs ------------------------------------------------------------------------------------------------------------
step "inputs"
if [[ -z "$WHEEL" && -z "$SRC" ]]; then
  for w in "$HERE"/../flexengine_v2-*-py3-none-any.whl; do [[ -f "$w" ]] && WHEEL="$w"; done   # offline bundle
  [[ -n "$WHEEL" ]] || { [[ -f "$HERE/../pyproject.toml" && -d "$HERE/../src/flexengine2" ]] && SRC="$HERE/.."; }
  [[ -n "$WHEEL$SRC" ]] || die "nothing to install: pass --wheel FILE or --from-source DIR"
fi
if [[ -z "$WHOUSE" ]]; then [[ -d "$HERE/../wheelhouse" ]] && WHOUSE="$HERE/../wheelhouse"; fi
if [[ -n "$WHEEL" ]]; then
  [[ -f "$WHEEL" ]] || die "wheel not found: $WHEEL"
  WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"
  SPEC="$WHEEL"; SRC_ID="wheel $(basename "$WHEEL") $(sha < "$WHEEL")"
  NEWVER="$(basename "$WHEEL" | cut -d- -f2)"
else
  [[ -f "$SRC/pyproject.toml" ]] || die "--from-source $SRC: no pyproject.toml"
  SRC="$(cd "$SRC" && pwd)"; SPEC="$SRC"
  SRC_ID="source $SRC $( (cd "$SRC" && find pyproject.toml src -type f ! -path '*/__pycache__/*' ! -name '*.pyc' \
            ! -path '*.egg-info*' | LC_ALL=C sort | xargs cat) | sha)"
  NEWVER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/src/flexengine2/__init__.py")"
fi
command -v "$PY" >/dev/null || die "python not found: $PY (Ubuntu: apt install python3 python3-venv)"
PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python >= 3.11 needed, $PY is $PYV"
ARCH="$(uname -m)"; [[ "$ARCH" == arm64 ]] && ARCH=aarch64
PIP_SRC=()
if [[ -n "$WHOUSE" ]]; then
  [[ -d "$WHOUSE" ]] || die "wheelhouse not found: $WHOUSE"
  WHOUSE="$(cd "$WHOUSE" && pwd)"
  sub="$WHOUSE/linux_${ARCH}-cp${PYV/./}"
  [[ -d "$sub" ]] && WHOUSE="$sub"
  ls "$WHOUSE"/*.whl >/dev/null 2>&1 || die "no wheels in $WHOUSE (build-wheels.sh --targets ${ARCH}-${PYV})"
  PIP_SRC=(--no-index --find-links "$WHOUSE")
  [[ -d "$WHOUSE/sdist" ]] && PIP_SRC+=(--find-links "$WHOUSE/sdist")
fi
[[ -f "$LOCK" ]] || die "lock file missing: $LOCK"
EXTRAS="${EXTRAS// /}"; REQ="$SPEC"; [[ -n "$EXTRAS" ]] && REQ="${SPEC}[${EXTRAS}]"
[[ -n "$DEVICE_ID" ]] || DEVICE_ID="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo flexengine)"
STAMP="installer=$INSTALLER_VERSION
python=$PYV
src=$SRC_ID
extras=$EXTRAS
lock=$(sha < "$LOCK")"
ok "flexengine-v2 $NEWVER from ${WHEEL:-$SRC}"
if [[ -n "$WHOUSE" ]]; then FROM="offline wheelhouse $WHOUSE"; else FROM="package index (online)"; fi
ok "python $PY ($PYV, $ARCH), extras [${EXTRAS:-none}], $FROM"
ok "prefix $PREFIX, config $ETC, data $STATE${ROOT:+, staged under $ROOT}"

# ---- python3-venv (Ubuntu splits it out) -------------------------------------------------------------------------------
if ! "$PY" -c 'import ensurepip, venv' 2>/dev/null; then
  if [[ $LIVE == 1 && $EUID -eq 0 ]] && command -v apt-get >/dev/null; then
    step "python3-venv"; run apt-get install -y "python${PYV}-venv"
  else die "$PY has no venv/ensurepip module: apt install python${PYV}-venv"; fi
fi

# ---- service user ------------------------------------------------------------------------------------------------------
if [[ $LIVE == 1 && $NOSVC == 0 ]]; then
  step "service user $SUSER"
  if getent group "$SGROUP" >/dev/null; then ok "group $SGROUP"; else run groupadd --system "$SGROUP"; fi
  if getent passwd "$SUSER" >/dev/null; then ok "user $SUSER ($(id -nG "$SUSER" 2>/dev/null | tr ' ' ,))"
  else run useradd --system --gid "$SGROUP" --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin \
         --comment "flexEngineV2" "$SUSER"; fi
fi

# ---- directories -------------------------------------------------------------------------------------------------------
step "directories"
ensure_dir "$R_ETC" 0750 root "$SGROUP"
ensure_dir "$R_ETC/tokens" 0750 root "$SGROUP"
ensure_dir "$R_STATE" 0700 "$SUSER" "$SGROUP"

# ---- venv --------------------------------------------------------------------------------------------------------------
step "venv $PREFIX"
OLDVER="$(venv_version)"
VENV_CHANGED=0
if [[ -f "$R_PREFIX/.flexengine-install" ]] && [[ "$(cat "$R_PREFIX/.flexengine-install")" == "$STAMP" ]] && [[ -n "$OLDVER" ]]; then
  ok "up to date (flexengine $OLDVER, same wheel/source, extras, lock, python)"
else
  VENV_CHANGED=1
  if [[ -e "$R_PREFIX" && ! -f "$R_PREFIX/pyvenv.cfg" ]]; then
    [[ -z "$(ls -A "$R_PREFIX" 2>/dev/null)" ]] || die "$R_PREFIX exists and is not a venv: refusing to touch it"
  fi
  if [[ -f "$R_PREFIX/pyvenv.cfg" ]] && ! grep -qE "^version(_info)? *= *$PYV\." "$R_PREFIX/pyvenv.cfg"; then
    warn "venv was built for another Python: recreating"; run rm -rf "$R_PREFIX"
  fi
  [[ -f "$R_PREFIX/pyvenv.cfg" ]] || run "$PY" -m venv "$R_PREFIX"
  PIPI=("$R_PREFIX/bin/python" -m pip --disable-pip-version-check -q install ${PIP_SRC[@]+"${PIP_SRC[@]}"} -c "$LOCK")
  run "${PIPI[@]}" "$REQ"
  # same version, different build (dev rebuilds): force the engine itself, never its dependencies
  run "${PIPI[@]}" --no-deps --force-reinstall "$SPEC"
  if [[ $DRY == 0 ]]; then
    NEWV="$(venv_version)"; [[ -n "$NEWV" ]] || die "$R_PREFIX/bin/flexengine does not run"
    printf '%s' "$STAMP" > "$R_PREFIX/.flexengine-install"
    ok "flexengine ${OLDVER:+$OLDVER -> }$NEWV"
  fi
fi
write_file "$R_PREFIX/share/doc/flexengine/README.md" 0644 root root < "$HERE/README.md"
write_file "$R_PREFIX/share/flexengine/engine.yaml.template" 0644 root root < "$TEMPLATE"

# ---- config (only if absent) -------------------------------------------------------------------------------------------
step "config"
write_file "$R_ETC/engine.yaml" 0640 root "$SGROUP" keep < <(sed "s/@DEVICE_ID@/$DEVICE_ID/g" "$TEMPLATE")

# ---- systemd -----------------------------------------------------------------------------------------------------------
UNIT_CHANGED=0
if [[ $NOSVC == 0 ]]; then
  step "systemd units"
  before=$CHANGES
  # (no pipes into write_file: it must run in this shell to count changes)
  write_file "$R_UNITDIR/$UNIT" 0644 root root < <(sed "s#@PREFIX@#$PREFIX#g" "$HERE/systemd/$UNIT")
  write_file "$R_UNITDIR/$TUNIT" 0644 root root < <(sed "s#@PREFIX@#$PREFIX#g" "$HERE/systemd/$TUNIT")
  GRP=""
  if [[ "$GROUPS_OPT" == auto ]]; then
    if [[ $LIVE == 1 ]]; then for g in dialout docker; do getent group "$g" >/dev/null && GRP="$GRP $g"; done; fi
  elif [[ "$GROUPS_OPT" != none ]]; then GRP="${GROUPS_OPT//,/ }"; fi
  GRP="${GRP# }"
  if [[ -n "$GRP" ]]; then
    for u in "$UNIT" "$TUNIT"; do
      write_file "$R_UNITDIR/$u.d/10-groups.conf" 0644 root root < <(printf '%s\n' \
        "# written by flexengine install.sh (--groups); see the unit header for what needs which group" \
        "[Service]" "SupplementaryGroups=$GRP")
    done
  else ok "no supplementary groups (dialout/docker absent or --groups none)"; fi
  [[ $CHANGES -gt $before ]] && UNIT_CHANGED=1
  [[ $UNIT_CHANGED == 1 ]] && sctl daemon-reload
  if have_systemd; then
    if [[ $ENABLE == 1 ]]; then
      if systemctl is-enabled -q flexengine 2>/dev/null; then ok "flexengine enabled"; else sctl enable flexengine; fi
    fi
    if systemctl is-active -q flexengine 2>/dev/null; then
      if [[ $VENV_CHANGED == 1 || $UNIT_CHANGED == 1 ]]; then sctl restart flexengine; else ok "flexengine running"; fi
    elif [[ $START == 1 ]]; then sctl start flexengine; fi
  fi
fi

# ---- summary -----------------------------------------------------------------------------------------------------------
state="not installed"; [[ $NOSVC == 1 ]] && state="skipped (--no-service)"
[[ -f "$R_UNITDIR/$UNIT" || ( $DRY == 1 && $NOSVC == 0 ) ]] && state="$UNITDIR/$UNIT"
if have_systemd && [[ -f "$R_UNITDIR/$UNIT" ]]; then
  state="$state ($(systemctl is-enabled flexengine 2>/dev/null || true), $(systemctl is-active flexengine 2>/dev/null || true))"
fi
if [[ $DRY == 1 ]]; then SHOWVER="$NEWVER (planned)"; else SHOWVER="$(venv_version)"; fi
summary "version" "$SHOWVER" \
        "venv" "$PREFIX ($( [[ $VENV_CHANGED == 1 ]] && echo "${OLDVER:+upgraded from $OLDVER}${OLDVER:-installed}" || echo unchanged))" \
        "config" "$ETC/engine.yaml" "data" "$STATE" "unit" "$state" "groups" "${GRP:-none}"
if [[ $DRY == 0 && $NOSVC == 0 && $START == 0 ]]; then
  echo "   next: put the onboarding token into $ETC/tokens/$DEVICE_ID, edit $ETC/engine.yaml, then"
  echo "         sudo systemctl enable --now flexengine && journalctl -u flexengine -f"
fi

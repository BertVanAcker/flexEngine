#!/usr/bin/env bash
# flexEngine installer for macOS / Linux / Linux ARMv6+
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- 0.2.0 --workspace ~/my-agent --config ~/my-config.yaml
#
# Docker is optional and only needed if you plan to deploy applications as
# containers. When run interactively you'll be asked whether to install it;
# for scripted/non-interactive installs pass --install-docker or --no-docker
# explicitly (the default is to skip it).
#
# Installs flexEngine into an isolated venv (no sudo, no system-wide
# changes) and scaffolds an agent workspace via `flexEngine init`.
set -euo pipefail

VERSION=""
WORKSPACE="./flexEngine-workspace"
CONFIG=""
INSTALL_DOCKER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --config)
      CONFIG="$2"
      shift 2
      ;;
    --install-docker)
      INSTALL_DOCKER="yes"
      shift
      ;;
    --no-docker)
      INSTALL_DOCKER="no"
      shift
      ;;
    -*)
      echo "error: unknown option '$1'" >&2
      exit 1
      ;;
    *)
      VERSION="$1"
      shift
      ;;
  esac
done

INSTALL_DIR="${FLEXENGINE_INSTALL_DIR:-$HOME/.local/share/flexEngine}"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found. Install Python 3.10+ and re-run this script." >&2
  exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "error: flexEngine requires Python 3.10+, found $PY_VERSION." >&2
  exit 1
fi

ARCH="$(uname -m)"
if [ "$ARCH" = "armv6l" ] || [ "$ARCH" = "armv7l" ]; then
  echo "note: detected $ARCH. One of flexEngine's dependencies (psutil) may not"
  echo "      have a prebuilt wheel for this architecture and could need to be"
  echo "      compiled from source. If installation fails below, install build"
  echo "      tools first, e.g.: sudo apt install -y python3-dev build-essential"
  echo
fi

echo "Installing flexEngine into an isolated environment at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"

PIP_SPEC="flexEngine"
if [ -n "$VERSION" ]; then
  PIP_SPEC="flexEngine==$VERSION"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$PIP_SPEC"

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/flexEngine" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/flexEngine" "\$@"
EOF
chmod +x "$BIN_DIR/flexEngine"

echo "flexEngine installed."

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo
    echo "note: $BIN_DIR is not on your PATH. Add this to your shell profile:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
if command -v docker >/dev/null 2>&1; then
  echo "Docker is already installed ($(docker --version)), skipping."
elif [ "$INSTALL_DOCKER" = "no" ]; then
  echo "Skipping Docker install (--no-docker). Install it later: https://docs.docker.com/get-docker/"
else
  if [ -z "$INSTALL_DOCKER" ]; then
    if [ -t 0 ]; then
      printf 'Docker is optional (only needed to deploy applications as containers). Install it now? [y/N] '
      read -r REPLY
      case "$REPLY" in
        [yY]*) INSTALL_DOCKER="yes" ;;
        *) INSTALL_DOCKER="no" ;;
      esac
    else
      # No terminal attached to ask (e.g. piped from curl) - default to skip.
      INSTALL_DOCKER="no"
    fi
  fi

  if [ "$INSTALL_DOCKER" = "yes" ]; then
    echo "Installing Docker ..."
    case "$(uname -s)" in
      Linux)
        curl -fsSL https://get.docker.com | sh
        echo "note: to run docker without sudo, add your user to the docker group:"
        echo "  sudo usermod -aG docker \$USER && newgrp docker"
        ;;
      Darwin)
        if command -v brew >/dev/null 2>&1; then
          brew install --cask docker
          echo "note: open the Docker app once to finish setup and start the daemon."
        else
          echo "note: Homebrew not found, can't install Docker Desktop automatically."
          echo "      Download it from: https://www.docker.com/products/docker-desktop/"
        fi
        ;;
      *)
        echo "note: automatic Docker install isn't supported on this OS. See:"
        echo "      https://docs.docker.com/get-docker/"
        ;;
    esac
  else
    echo "Skipping Docker install. You can install it later: https://docs.docker.com/get-docker/"
  fi
fi

INIT_ARGS=("$WORKSPACE")
if [ -n "$CONFIG" ]; then
  INIT_ARGS+=(--config "$CONFIG")
fi
if [ ! -t 0 ]; then
  # stdin isn't a terminal (e.g. this script was piped from curl) - the
  # interactive prompts below can't be answered, so fall back to defaults.
  INIT_ARGS+=(--non-interactive)
fi

echo
echo "Setting up workspace at $WORKSPACE ..."
"$VENV_DIR/bin/flexEngine" init "${INIT_ARGS[@]}"

if [ ! -t 0 ] && [ -z "$CONFIG" ]; then
  echo
  echo "note: ran non-interactively (no terminal attached to answer prompts)."
  echo "      edit $WORKSPACE/configuration/config.yaml, or re-run:"
  echo "      flexEngine init $WORKSPACE --force"
fi

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL_DIR="${HERMES_INSTALL_DIR:-$HERMES_HOME/hermes-core}"
VENV_DIR="$INSTALL_DIR/.venv"

mkdir -p "$INSTALL_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to install hermes-core" >&2
  exit 1
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install "$REPO_ROOT"

"$VENV_DIR/bin/hermes-core" install "$@"

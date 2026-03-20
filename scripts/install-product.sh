#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
INSTALL_DIR="${HERMES_INSTALL_DIR:-$HERMES_HOME/hermes-agent}"
export HERMES_REPO_URL_SSH="${HERMES_REPO_URL_SSH:-git@github.com:ErniConcepts/hermes-agent-core.git}"
export HERMES_REPO_URL_HTTPS="${HERMES_REPO_URL_HTTPS:-https://github.com/ErniConcepts/hermes-agent-core.git}"

"$SCRIPT_DIR/install.sh" --skip-setup "$@"

PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Could not find installed Hermes Core Python at $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" -m hermes_cli.main product install

#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${HERMES_HOME}" /workspace

case "${1:-smoke}" in
  smoke)
    shift || true
    python experiments/gvisor/smoke_test.py "$@"
    ;;
  shell)
    shift || true
    exec /bin/bash "$@"
    ;;
  hermes)
    shift || true
    exec hermes "$@"
    ;;
  *)
    exec "$@"
    ;;
esac

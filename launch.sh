#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"
exec .venv/bin/python app.py "$@"

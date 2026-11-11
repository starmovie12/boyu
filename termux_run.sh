#!/usr/bin/env bash
# Termux non-interactive wrapper for updl (1-Month Rolling Backwards Continue Mode)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "[Termux Setup] Python virtual environment (.venv) ban raha hai..."
    python3 -m venv --system-site-packages .venv || python3 -m venv --without-pip --system-site-packages .venv
fi

PYTHON_BIN=".venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

ACTION="${1:-continue}"
shift || true

case "$ACTION" in
    setup)
        echo "[Termux Setup] Environment ready: $($PYTHON_BIN --version)"
        if [ -x ".venv/bin/pip" ]; then
            .venv/bin/pip install -r requirements.txt
        fi
        ;;
    probe)
        "$PYTHON_BIN" -m updl probe "$@"
        ;;
    continue|month|next)
        "$PYTHON_BIN" -m updl continue "$@"
        ;;
    status|report)
        "$PYTHON_BIN" -m updl report "$@"
        ;;
    *)
        "$PYTHON_BIN" -m updl "$ACTION" "$@"
        ;;
esac

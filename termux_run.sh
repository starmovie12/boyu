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
        if command -v pkg >/dev/null 2>&1 && [ -n "${PREFIX:-}" ]; then
            # Termux: numpy/pandas/scipy/pyarrow ko pip se compile mat karo (numpy build fail hota hai).
            # Termux ke ready-made packages lo, baaki pure-Python cheezein pip se.
            echo "[Termux Setup] pkg se numpy, pandas, scipy, pyarrow install ho rahe hain..."
            pkg install -y python-numpy python-scipy python-pandas python-pyarrow
            "$PYTHON_BIN" -m pip install -r requirements-termux.txt
        else
            "$PYTHON_BIN" -m pip install -r requirements.txt
        fi
        "$PYTHON_BIN" -c "import numpy, pandas, scipy, pyarrow, yaml, requests; print('[Termux Setup] OK -> pandas', pandas.__version__, '| numpy', numpy.__version__, '| scipy', scipy.__version__, '| pyarrow', pyarrow.__version__)"
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

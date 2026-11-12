#!/usr/bin/env bash
# Termux non-interactive wrapper for updl (1-Month Rolling Backwards Continue Mode)
#   bash termux_run.sh setup      packages + .venv (run once, and again after `pkg upgrade`)
#   bash termux_run.sh probe      Stage-0 probe (max 40 API calls)
#   bash termux_run.sh continue   next 1-month batch (resumes an interrupted batch first)
#   bash termux_run.sh record     live option-chain Recorder (market time only)
#   bash termux_run.sh export     phone CSV zip of the downloaded window
#   bash termux_run.sh <command>  any other `python -m updl` command
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IS_TERMUX=0
if command -v pkg >/dev/null 2>&1 && [ -n "${PREFIX:-}${TERMUX__PREFIX:-}" ]; then
    IS_TERMUX=1
fi

say() { printf '%s\n' "$*"; }

need_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        say "[Setup] python3 nahi mila. Pehle chalao: pkg update -y && pkg install -y python git"
        exit 1
    fi
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        say "[Setup] Python 3.11 ya naya chahiye. Abhi: $(python3 --version 2>&1). Chalao: pkg upgrade -y"
        exit 1
    fi
}

# A Termux `pkg upgrade` can move Python to a new minor version (3.13 -> 3.14). .venv/bin/python3 is a
# symlink to the unversioned python3, so asking it for its version always shows the NEW version; the venv's
# own record (pyvenv.cfg "version =") and its lib/pythonX.Y folder still belong to the OLD one. Compare those.
venv_ok() {
    [ -x ".venv/bin/python3" ] && [ -f ".venv/pyvenv.cfg" ] || return 1
    local sys_v cfg_v
    sys_v="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    cfg_v="$(sed -n 's/^version[_info]* *= *\([0-9]*\.[0-9]*\).*/\1/p' .venv/pyvenv.cfg | head -n 1)"
    [ "$sys_v" = "$cfg_v" ] && [ -d ".venv/lib/python$sys_v" ]
}

ensure_venv() {
    need_python
    if [ -d ".venv" ] && ! venv_ok; then
        say "[Setup] .venv purane Python ka hai (pkg upgrade ke baad). Naya .venv bana raha hoon..."
        rm -rf .venv
    fi
    if [ ! -d ".venv" ]; then
        say "[Setup] Python virtual environment (.venv) ban raha hai..."
        python3 -m venv --system-site-packages .venv || python3 -m venv --without-pip --system-site-packages .venv
    fi
}

wake_lock() {
    if [ "$IS_TERMUX" -eq 1 ] && command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock || true
        say "[Termux] Wake-lock ON (phone so jaye tab bhi kaam chale). Kaam ke baad: termux-wake-unlock"
    fi
    say "[Termux] Agar Android process band kar de, wahi command dobara chalao. Kaam wahin se aage badhega."
}

ACTION="${1:-continue}"
shift || true

case "$ACTION" in
    setup)
        if [ "$IS_TERMUX" -eq 1 ]; then
            # Termux: numpy/pandas/scipy/pyarrow ko pip se compile mat karo (numpy build fail hota hai).
            # Termux ke ready-made packages lo, baaki pure-Python cheezein pip se.
            # pkg pehle: pkg install khud Python ko naye version par le ja sakta hai, .venv uske BAAD banta hai.
            say "[Termux Setup] pkg update + numpy, pandas, scipy, pyarrow install ho rahe hain..."
            pkg update -y || say "[Termux Setup] pkg update fail hua; phir bhi aage try kar raha hoon."
            if ! pkg install -y python python-numpy python-scipy python-pandas python-pyarrow; then
                say "[Termux Setup] pkg install fail hua."
                say "  Google Play wale Termux ke repo mein python-pandas nahi hai."
                say "  F-Droid ya GitHub wala Termux use karo (Play wala Termux uninstall karke)."
                say "  Ya: pkg update -y && pkg upgrade -y, phir dobara: bash termux_run.sh setup"
                exit 1
            fi
        fi
        ensure_venv
        PYTHON_BIN=".venv/bin/python3"
        say "[Termux Setup] Environment ready: $($PYTHON_BIN --version)"
        if [ "$IS_TERMUX" -eq 1 ]; then
            "$PYTHON_BIN" -m pip install -r requirements-termux.txt
            if [ ! -d "$HOME/storage" ]; then
                say "[Termux Setup] Phone Download folder ke liye ek baar chalao: termux-setup-storage (Allow dabana)"
            fi
        else
            "$PYTHON_BIN" -m pip install -r requirements.txt
        fi
        "$PYTHON_BIN" -c "import numpy, pandas, scipy, pyarrow, yaml, requests; print('[Termux Setup] OK -> pandas', pandas.__version__, '| numpy', numpy.__version__, '| scipy', scipy.__version__, '| pyarrow', pyarrow.__version__)"
        ;;
    *)
        if [ -d ".venv" ] && ! venv_ok; then
            say "[Termux] .venv purana ya kharab hai. Pehle chalao: bash termux_run.sh setup"
            exit 1
        fi
        PYTHON_BIN=".venv/bin/python3"
        if [ ! -x "$PYTHON_BIN" ]; then
            say "[Termux] .venv nahi mila. Pehle chalao: bash termux_run.sh setup"
            exit 1
        fi
        case "$ACTION" in
            probe|continue|month|next|download|record) wake_lock ;;
        esac
        case "$ACTION" in
            continue|month|next) "$PYTHON_BIN" -m updl continue "$@" ;;
            status|report)       "$PYTHON_BIN" -m updl report "$@" ;;
            *)                   "$PYTHON_BIN" -m updl "$ACTION" "$@" ;;
        esac
        ;;
esac

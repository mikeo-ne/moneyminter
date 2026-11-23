#!/usr/bin/env bash
# Create (or repair) the Money Minter virtualenv, then verify the driver stack.
#
#   bash scripts/bootstrap-venv.sh
#
# Safe to re-run. It picks a Python >= 3.10 that actually exists, rebuilds the
# venv if the current one is broken, and tells you the exact command to use
# afterwards — because a venv without a working `python` shim is a common
# macOS annoyance and every doc in this repo says `python -m moneyminter`.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- pick python
say "==> Looking for Python 3.10+"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$cand"
        break
    fi
done
[ -n "$PY" ] || die "No Python 3.10+ found. Install one:  brew install python@3.12"
PY_PATH="$(command -v "$PY")"
say "    using $PY_PATH ($("$PY" -c 'import platform; print(platform.python_version())'))"

# ------------------------------------------------------------- validate venv
broken=0
if [ -d .venv ]; then
    if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
        say "==> Existing .venv is usable"
    else
        warn "==> .venv exists but its interpreter does not run — rebuilding"
        broken=1
    fi
fi

if [ ! -d .venv ] || [ "$broken" = 1 ]; then
    rm -rf .venv
    say "==> Creating .venv"
    "$PY_PATH" -m venv .venv
fi

VPY=".venv/bin/python"
[ -x "$VPY" ] || VPY=".venv/bin/python3"
[ -x "$VPY" ] || die ".venv has no interpreter; remove .venv and re-run"

# Some venvs ship python3 but no python shim; every doc here uses `python`.
if [ ! -e .venv/bin/python ] && [ -e "$VPY" ]; then
    ln -sf "$(basename "$VPY")" .venv/bin/python
    say "==> Added the missing .venv/bin/python shim"
fi

say "==> Installing dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt

say "==> Checking the MT5 driver for this platform"
case "$(uname -s)" in
    Darwin)
        "$VPY" -m pip install --quiet mt5_mac && say "    mt5_mac installed"
        ;;
    Linux)
        say "    Linux: MT5 needs a remote bridge (or Wine). See LIVE_TRADING.md §0"
        ;;
esac

say "==> Running the test suite"
"$VPY" -m pytest tests -q 2>&1 | tail -3

cat <<EOF

$(say "Done.")
Activate with:            source .venv/bin/activate
Then always use:          python -m moneyminter doctor

If your shell cannot find 'python' after activating, either run
                          .venv/bin/python -m moneyminter doctor
or add the shim:          ln -sf python3 .venv/bin/python

EOF

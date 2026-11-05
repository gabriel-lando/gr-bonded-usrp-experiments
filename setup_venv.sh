#!/usr/bin/env bash
#
# Create the Python environment for the experiment scripts: a .venv with
# --system-site-packages, so it sees the system GNU Radio, UHD, gr-ieee802-11,
# gr-foo and gr-bonded-usrp Python modules, plus requirements.txt.
#
# Usage:
#   ./setup_venv.sh
#   source .venv/bin/activate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv --system-site-packages "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
"$VENV_DIR/bin/python3" -c "from gnuradio import bonded_usrp; import ieee802_11, foo" \
    || echo "warning: gr-bonded-usrp, gr-ieee802-11 or gr-foo is not importable" >&2
echo "Ready: source $VENV_DIR/bin/activate"

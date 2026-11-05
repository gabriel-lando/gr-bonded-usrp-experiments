# Source (do not execute) to make Python load a gr-ieee802-11 build with
# gr-ieee802-11-perf.patch applied, installed to $IEEE_PERF_PREFIX
# (default ~/deps/ieee-perf-install), instead of the system one. Affects only
# this shell.
IEEE_PERF_PREFIX=${IEEE_PERF_PREFIX:-$HOME/deps/ieee-perf-install}
_py=$(python3 -c 'import sys; print(f"python{sys.version_info[0]}.{sys.version_info[1]}")')
export PYTHONPATH="$IEEE_PERF_PREFIX/lib/$_py/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export LD_LIBRARY_PATH="$IEEE_PERF_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
unset _py

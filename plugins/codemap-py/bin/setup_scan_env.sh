#!/usr/bin/env bash
# DEPRECATED — the implementation now lives in setup_scan_env.py.
#
# This file survives only as a delegating shim so pre-existing bash call sites keep
# working unmodified. `.sh` does not execute on Windows, which is why the logic moved;
# new call sites must invoke the .py directly:
#
#   python "${CLAUDE_PLUGIN_ROOT}/bin/setup_scan_env.py" --arguments "$ARGUMENTS"
#
# `exec` replaces this shell, so the .py keeps this process's PID (the tmpfile names
# embed it), its exit code, and both output streams — the shim is invisible to callers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/python" "${SCRIPT_DIR}/setup_scan_env.py" "$@"

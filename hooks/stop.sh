#!/usr/bin/env bash
# Foreman Stop hook (collector C3).
#
# The Stop and SessionEnd events can each be the last thing a run emits, and for a headless
# `claude -p` cadence either may fire first. Both must converge on exactly one receipt, so
# this hook delegates to the same emitter; build_receipt.py is idempotent per run_id, so
# whichever event fires first writes the receipt and the other is a no-op.
#
# Killing a run mid-flight still reaches one of these events with no partial on the spool,
# which is what turns a silent death into a status=failed receipt.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${script_dir}/session_end.sh"

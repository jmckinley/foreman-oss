#!/usr/bin/env bash
# Foreman SessionStart hook -- app-side drain of the operator decision queue.
#
# When a Claude Code session starts in a project's worktree, this delivers any operator
# decisions that were queued while no session was running (SPEC.md section 18). The engine
# resolves the project from the working directory, applies pending app-side decisions in
# order, and emits the SessionStart `additionalContext` JSON so the delivered decisions
# enter the session's context. Applying is idempotent: a decision already applied is
# skipped, so re-running the hook is safe.
#
# This hook must never break session startup. If Foreman is not configured (no state
# checkout) or the cwd is not a known project, it exits 0 silently.
set -euo pipefail

if [[ -z "${FOREMAN_DIR:-}" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  FOREMAN_DIR="$(cd "${script_dir}/.." && pwd)"
  export FOREMAN_DIR
fi

# The state branch checkout holding the queue. Without it there is nothing to drain.
if [[ -z "${FOREMAN_STATE_DIR:-}" || ! -d "${FOREMAN_STATE_DIR}" ]]; then
  exit 0
fi

# The project is resolved from the directory the session started in.
export FOREMAN_CWD="${FOREMAN_CWD:-$PWD}"

# Best-effort session identity for the applied-by record.
export FOREMAN_SESSION_UUID="${FOREMAN_SESSION_UUID:-${CLAUDE_SESSION_ID:-}}"

# Never let a drain failure abort the session; log to stderr and continue.
PYTHONPATH="${FOREMAN_DIR}" python3 -m collectors.decisions drain-session || {
  echo "foreman: decision drain failed (non-fatal)" >&2
  exit 0
}

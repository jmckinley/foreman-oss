#!/usr/bin/env bash
# Foreman SessionEnd hook (collector C3).
#
# Writes the receipt envelope for a cadence run and merges the prompt's partial. The
# heavy lifting -- verdict derivation, metric-key checking, schema validation -- lives in
# collectors/build_receipt.py so it can be tested without a live session. This hook only
# locates the repo, stamps the end time, and shells out.
#
# Identity comes from the FOREMAN_* environment set by the dispatcher, never from stdin,
# so the same script works on cloud, local and session tiers. A missing partial is not an
# error here: build_receipt records status=failed, which is the correct verdict for a run
# that ended without reporting.
set -euo pipefail

# Resolve the foreman repo root from this script's location unless already provided.
if [[ -z "${FOREMAN_DIR:-}" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  FOREMAN_DIR="$(cd "${script_dir}/.." && pwd)"
  export FOREMAN_DIR
fi

# Only act on Foreman-dispatched runs. An interactive session with no FOREMAN_RUN_ID set
# is not a cadence and must not produce a receipt.
if [[ -z "${FOREMAN_RUN_ID:-}" ]]; then
  exit 0
fi

# Stamp the end time once so both hooks agree on the receipt filename.
if [[ -z "${FOREMAN_ENDED:-}" ]]; then
  FOREMAN_ENDED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export FOREMAN_ENDED
fi

# Capture the Claude Code version for the parser pin, best-effort.
if [[ -z "${FOREMAN_CC_VERSION:-}" ]] && command -v claude >/dev/null 2>&1; then
  FOREMAN_CC_VERSION="$(claude --version 2>/dev/null | head -n1 || true)"
  export FOREMAN_CC_VERSION
fi

python3 -m collectors.build_receipt

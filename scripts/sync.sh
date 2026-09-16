#!/usr/bin/env bash
# Update BOTH Foreman surfaces to the current committed state, in one step:
#   • cloud (Vercel)  — git push; the git integration auto-deploys the read-only board
#   • local dashboard — restart (http.server has no hot-reload; data/registry render live either way)
#
# Run it directly after a change, or let the post-commit hook (scripts/install-hooks.sh) run it
# automatically after every commit. Gated on validate so an invalid registry/cadence set is
# never published to either surface.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! python3 -m collectors.validate >/dev/null 2>&1; then
  echo "sync: registry/cadence validation FAILED — refusing to publish"; exit 1
fi

echo "cloud: pushing (Vercel git-integration auto-deploys)…"
git push origin main 2>&1 | tail -1

if bash scripts/restart-dashboard.sh >/dev/null 2>&1; then
  echo "local: dashboard restarted — http://localhost:8787"
fi
echo "cloud: https://foreman-six-gilt.vercel.app  (deploy in progress)"

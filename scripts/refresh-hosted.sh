#!/usr/bin/env bash
# Refresh the hosted (Vercel) read-only board with current data.
#
# The board renders from committed index.db + registry.yaml + data/keyshare.json, and Vercel
# git-integration auto-deploys `main`. So this recollects, regenerates the shared-key artifact,
# and commits + pushes -- the push triggers the deploy. index.db is a rebuildable cache but is
# committed to seed the hosted board, so refreshing it is expected churn. Cron for a fresh board.
#
# (Requires the repo git author to be an email verified on the connected GitHub account, else
# Vercel BLOCKs the git deploy -- see docs/HANDOVER.md.)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FM="$(pwd)"
HOST="${FOREMAN_HOST:-mbp}"

# Operator-wired runtime env (FOREMAN_QUOTA_CMD etc.) so collect's quota fetch fires. No secrets.
set -a; [ -f config/foreman.env ] && . config/foreman.env; set +a

echo "collecting (git + github) ..."
python3 -m collectors.collect --state-dir ./state --index ./index.db --host "$HOST" --foreman-dir .

echo "regenerating shared-key artifact ..."
FOREMAN_DIR="$FM" FOREMAN_HOST="$HOST" python3 -m collectors.keyscan

if git diff --quiet index.db data/keyshare.json; then
  echo "no data changes; hosted board already current"
  exit 0
fi
git add index.db data/keyshare.json
git commit -q -m "Refresh hosted board data (collect + keyscan)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
git push origin main
echo "pushed; Vercel auto-deploys: https://foreman-six-gilt.vercel.app"

#!/usr/bin/env bash
set -euo pipefail
# Restart the Foreman dashboard on :8787, serving the repo as the live instance.
# The stdlib http.server does NOT hot-reload, so run this after every app commit.
# Usage: scripts/restart-dashboard.sh [port]

REPO="/srv/acme-demo/foreman"
PORT="${1:-8787}"

# Stop whatever is already bound to the port.
if pid="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)"; then
  kill $pid 2>/dev/null || true
  for _ in 1 2 3 4 5 6; do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || break
    sleep 0.5
  done
fi

cd "$REPO"
FOREMAN_DIR="$REPO" \
FOREMAN_STATE_DIR="$REPO/state" \
FOREMAN_INDEX="$REPO/index.db" \
PYTHONPATH="$REPO" \
  nohup python3 -m collectors.web --port "$PORT" --refresh 30 > /tmp/foreman-web.log 2>&1 &
disown
sleep 2

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "dashboard restarted on http://localhost:$PORT (FOREMAN_DIR=$REPO)"
else
  echo "FAILED to start; see /tmp/foreman-web.log" >&2
  tail -5 /tmp/foreman-web.log >&2 || true
  exit 1
fi

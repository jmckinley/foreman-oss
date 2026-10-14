#!/usr/bin/env bash
# Foreman one-command install: put the `foreman` CLI on PATH, scaffold an instance, create the
# state-branch worktree, and run a preflight. Idempotent — safe to re-run. POSIX-portable bash
# across macOS and Linux.
#
#   curl -fsSL .../install.sh | bash                 # from a clone, just: scripts/install.sh
#   FOREMAN_DIR=~/work/foreman scripts/install.sh    # choose the instance dir
#   scripts/install.sh --with-services               # also install the launchd/systemd scheduler
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FOREMAN_DIR="${FOREMAN_DIR:-$HOME/foreman}"
HOST="${FOREMAN_HOST:-$(hostname -s 2>/dev/null || hostname)}"
WITH_SERVICES=""
for arg in "$@"; do
  case "$arg" in
    --with-services) WITH_SERVICES="--with-services" ;;
    --dir=*) FOREMAN_DIR="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }

# 1. Install the CLI. Prefer pipx (isolated); fall back to pip --user. Editable so the shipped
#    schema/cadences/prompts dirs resolve at their repo paths (see pyproject note).
say "installing the foreman CLI from $REPO_ROOT"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force -e "$REPO_ROOT"
elif command -v pip3 >/dev/null 2>&1; then
  pip3 install --user -e "$REPO_ROOT"
else
  echo "need pipx or pip3 on PATH" >&2; exit 1
fi

FOREMAN="$(command -v foreman || echo "python3 -m collectors.cli")"

# 2. Scaffold the instance (registry + state/ + spool/ + index cache). Idempotent.
say "scaffolding an instance at $FOREMAN_DIR (host: $HOST)"
$FOREMAN init "$FOREMAN_DIR" --host "$HOST" ${WITH_SERVICES:+$WITH_SERVICES}

# 3. Create the state-branch worktree if the instance doesn't have receipts yet. The state
#    branch holds receipts/ and queue/ only; it is where every verdict is committed.
STATE_DIR="$FOREMAN_DIR/state"
if [ ! -d "$STATE_DIR/receipts" ]; then
  say "creating the state worktree at $STATE_DIR"
  if git -C "$REPO_ROOT" show-ref --verify --quiet refs/heads/state; then
    git -C "$REPO_ROOT" worktree add "$STATE_DIR" state 2>/dev/null || mkdir -p "$STATE_DIR/receipts" "$STATE_DIR/queue"
  else
    mkdir -p "$STATE_DIR/receipts" "$STATE_DIR/queue"
  fi
fi

# 4. Preflight. Never fails the install — it reports what still needs wiring.
say "running preflight (foreman doctor)"
FOREMAN_DIR="$FOREMAN_DIR" FOREMAN_STATE_DIR="$STATE_DIR" $FOREMAN doctor "$FOREMAN_DIR" || true

cat <<EOF

Foreman is installed. Next:

  export FOREMAN_DIR="$FOREMAN_DIR"
  export FOREMAN_STATE_DIR="$STATE_DIR"

  $FOREMAN discover register <path-to-a-repo>   # add a project (or scan: discover scan ~)
  $FOREMAN loops enable <project> security-review
  $FOREMAN collect --host "$HOST"               # snapshot git + GitHub into the index
  $FOREMAN web                                   # open http://127.0.0.1:8787

  # optional: get pinged on reds
  export FOREMAN_NOTIFY_WEBHOOK=https://hooks.slack.com/services/...
  $FOREMAN notify test
EOF

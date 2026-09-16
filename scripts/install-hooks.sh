#!/usr/bin/env bash
# Install Foreman's git hooks (not tracked by git, so run this once per clone).
# post-commit: after every commit, update local + cloud via scripts/sync.sh.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$(git rev-parse --git-dir)/hooks/post-commit"
cp scripts/hooks/post-commit "$dest"
chmod +x "$dest"
echo "installed post-commit hook -> $dest"
echo "every commit now: git push (cloud auto-deploys) + restart the local dashboard"

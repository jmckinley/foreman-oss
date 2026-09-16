"""C4 -- git state per host per worktree.

Runs read-only ``git`` in a worktree and produces one ``git_state`` row: branch,
ahead/behind, dirty files, worktrees and orphans, stale branches, unmerged agent branches,
largest blob, and force pushes in the last 7 days. The row is a point-in-time snapshot,
upserted by (project, host, taken); the current state is always re-derivable from git, so
dropping the index costs only a re-run.

Failure modes (SPEC.md section 9): host asleep or worktree moved. Every field degrades to a
safe default rather than aborting the collector, because a partial snapshot beats none.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

AGENT_BRANCH_PREFIXES = ("foreman/", "agent/", "claude/")
STALE_BRANCH_DAYS = 30


def _git(worktree: Path, *args: str, timeout: float = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(["git", "-C", str(worktree), *args],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return p.returncode == 0, p.stdout.strip()


def _ref_exists(wt: Path, ref: str) -> bool:
    ok, _ = _git(wt, "rev-parse", "--verify", "--quiet", ref)
    return ok


def collect(worktree: str | Path, project: str, host: str, *, default_branch: str = "main",
            taken: str, now: dt.datetime | None = None) -> dict:
    wt = Path(worktree)
    now = now or dt.datetime.now(dt.timezone.utc)

    ok_branch, branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")

    ahead = behind = 0
    base = f"origin/{default_branch}" if _ref_exists(wt, f"origin/{default_branch}") else None
    if base:
        ok, out = _git(wt, "rev-list", "--left-right", "--count", f"{base}...HEAD")
        if ok and out:
            left, right = out.split()
            behind, ahead = int(left), int(right)

    _, dirty = _git(wt, "status", "--porcelain")
    dirty_files = len([ln for ln in dirty.splitlines() if ln.strip()])

    worktrees, orphans = _worktrees(wt)
    stale = _stale_branches(wt, now)
    unmerged = _unmerged_agent_branches(wt, default_branch)
    largest = _largest_blob_mb(wt)
    forced = _force_pushes_7d(wt, branch if ok_branch else default_branch, now)

    return {
        "project": project,
        "host": host,
        "taken": taken,
        "branch": branch if ok_branch else None,
        "ahead": ahead,
        "behind": behind,
        "dirty_files": dirty_files,
        "worktrees": worktrees,
        "orphan_worktrees": orphans,
        "stale_branches": stale,
        "unmerged_agent_branches": unmerged,
        "largest_blob_mb": largest,
        "force_pushes_7d": forced,
    }


def _worktrees(wt: Path) -> tuple[int, int]:
    ok, out = _git(wt, "worktree", "list", "--porcelain")
    if not ok:
        return 0, 0
    paths = [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]
    orphans = sum(1 for p in paths if not Path(p).exists())
    return len(paths), orphans


def _stale_branches(wt: Path, now: dt.datetime) -> int:
    ok, out = _git(wt, "for-each-ref", "--format=%(committerdate:unix) %(refname:short)",
                   "refs/heads")
    if not ok or not out:
        return 0
    cutoff = now.timestamp() - STALE_BRANCH_DAYS * 86400
    count = 0
    for line in out.splitlines():
        try:
            ts = int(line.split(" ", 1)[0])
        except (ValueError, IndexError):
            continue
        if ts < cutoff:
            count += 1
    return count


def _unmerged_agent_branches(wt: Path, default_branch: str) -> int:
    base = default_branch if _ref_exists(wt, default_branch) else f"origin/{default_branch}"
    if not _ref_exists(wt, base):
        return 0
    ok, out = _git(wt, "branch", "--no-merged", base, "--format=%(refname:short)")
    if not ok:
        return 0
    return sum(1 for b in out.splitlines()
               if b.strip().startswith(AGENT_BRANCH_PREFIXES))


def _largest_blob_mb(wt: Path) -> float | None:
    # --unordered is faster (no sorting), and a short timeout keeps a huge-history repo from
    # stalling the whole collect for a full minute. On timeout we report None (unknown), which
    # repo_health treats as 0 -- better than blocking every other project's snapshot.
    ok, out = _git(wt, "cat-file", "--batch-all-objects", "--unordered",
                   "--batch-check=%(objecttype) %(objectsize)", timeout=12)
    if not ok or not out:
        return None
    largest = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "blob":
            try:
                largest = max(largest, int(parts[1]))
            except ValueError:
                pass
    return round(largest / (1024 * 1024), 3)


def _force_pushes_7d(wt: Path, branch: str, now: dt.datetime) -> int:
    """Best-effort: count non-fast-forward rewinds of ``branch`` in the local reflog.

    The reflog is local, so this catches rewinds this checkout observed; a force push seen
    only on the remote is a C5/GitHub concern. Any error yields 0 rather than a false alarm.
    """
    ok, out = _git(wt, "reflog", "show", "--format=%H %gt", branch)
    if not ok or not out:
        return 0
    entries = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                entries.append((parts[0], int(parts[1])))
            except ValueError:
                continue
    cutoff = now.timestamp() - 7 * 86400
    forced = 0
    for (new_sha, ts), (old_sha, _) in zip(entries, entries[1:]):
        if ts < cutoff:
            break
        is_anc, _ = _git(wt, "merge-base", "--is-ancestor", old_sha, new_sha)
        if not is_anc:  # old is not an ancestor of new -> the branch was rewound
            forced += 1
    return forced

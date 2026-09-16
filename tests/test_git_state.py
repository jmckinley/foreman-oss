"""git_state.collect against real git worktrees (via the git_repo fixture)."""
import datetime as dt

from collectors import git_state

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)


def test_collect_clean_repo(git_repo):
    row = git_state.collect(git_repo(), "p", "mbp", taken="2026-09-10T12:00:00Z", now=NOW)
    assert row["branch"] == "main"
    assert row["dirty_files"] == 0
    assert row["unmerged_agent_branches"] == 0 and row["stale_branches"] == 0
    assert row["ahead"] == 0 and row["behind"] == 0


def test_collect_flags_dirty_agent_and_stale(git_repo):
    repo = git_repo(agent_branch=True, stale_branch=True, dirty=True)
    row = git_state.collect(repo, "p", "mbp", taken="2026-09-10T12:00:00Z", now=NOW)
    assert row["dirty_files"] >= 1                       # uncommitted file
    assert row["unmerged_agent_branches"] >= 1           # foreman/work branch not merged
    assert row["stale_branches"] >= 1                    # the 60-day-old branch
    assert row["largest_blob_mb"] is not None            # blob scan ran


def test_collect_missing_worktree(tmp_path):
    # a path that isn't a git repo degrades gracefully rather than raising
    row = git_state.collect(tmp_path / "nope", "p", "mbp", taken="t", now=NOW)
    assert row["project"] == "p" and row["branch"] is None
    assert row["dirty_files"] == 0

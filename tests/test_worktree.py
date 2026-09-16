"""Worktree-isolated launch (M8.4 half): a writes cadence runs in a throwaway git worktree,
reaped when the run ends, so an unattended run never dirties the project's main tree."""

import datetime as dt
import subprocess
from pathlib import Path

import os

from collectors import scheduler, budget, db, decisions as D

_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _commit_branch(repo, branch):
    """Create `branch` with one commit ahead of main, then return to main."""
    for args in (["checkout", "-b", branch],):
        subprocess.run(["git", "-C", str(repo), *args], env=_GIT_ENV, check=True, capture_output=True)
    (Path(repo) / "work.txt").write_text("change")
    subprocess.run(["git", "-C", str(repo), "add", "work.txt"], env=_GIT_ENV, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "work"], env=_GIT_ENV, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "main"], env=_GIT_ENV, check=True, capture_output=True)


def _wt_list(repo) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                         capture_output=True, text=True)
    return [l for l in out.stdout.splitlines() if l.strip()]


def test_add_and_remove_worktree(git_repo, tmp_path):
    repo = git_repo()
    dest = tmp_path / "wt" / "throwaway"
    assert scheduler._add_worktree(str(repo), dest) is True
    assert dest.is_dir() and (dest / "a.txt").is_file()      # checked out HEAD
    assert len(_wt_list(repo)) == 2                           # main + throwaway

    scheduler.remove_worktree(str(repo), dest)
    assert not dest.exists() and len(_wt_list(repo)) == 1     # cleaned up


def test_reap_cleans_up_finished_worktree(git_repo, tmp_path, spool_dir, state_dir):
    repo = git_repo()
    dest = tmp_path / "wt" / "run1"
    assert scheduler._add_worktree(str(repo), dest)
    conn = db.open_index(":memory:")
    # a finished (dead pid), non-budgeted, isolated launch
    budget.record_launch(spool_dir, {
        "run_id": "01J00000000000000000000WT1", "pid": 2_000_000_000, "project": "p",
        "cadence": "c", "tier": "local", "host": "mbp", "started": "2026-09-06T11:00:00Z",
        "lock_key": "c:p", "budget": {}, "worktree": str(dest), "repo": str(repo)})
    stopped = budget.reap(spool_dir, state_dir, conn,
                          now=dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc))
    assert stopped == []                                      # not over budget, just finished
    assert not dest.exists() and len(_wt_list(repo)) == 1     # worktree reaped
    assert budget.load_launches(spool_dir) == []             # dropped from the registry


class _FakeProc:
    pid = 4242


def _fake_spawn(argv, *, cwd, env):
    _fake_spawn.last_cwd = cwd
    _fake_spawn.last_argv = argv
    return _FakeProc()


def _writes_foreman(tmp_path, repo, *, writes: bool):
    fm = tmp_path / "fm"
    (fm / "cadences").mkdir(parents=True, exist_ok=True)
    (fm / "prompts").mkdir(exist_ok=True)
    (fm / "prompts" / "w.md").write_text("do work\n")
    body = ("slug: wcad\ntier: local\nschedule: \"7 3 * * 1\"\napplies_to: [p]\n"
            "prompt_ref: prompts/w.md\nmetrics: [m]\n"
            "verdict: {green: 'm == 0', amber: 'm < 5', red: otherwise}\n")
    if writes:
        body += "writes:\n  branch: foreman/wcad\n  open_pr: true\n"
    (fm / "cadences" / "wcad.yaml").write_text(body)
    (fm / "registry.yaml").write_text(
        "version: 1\nhosts:\n  mbp:\n    os: darwin\n    role: interactive\n"
        "defaults:\n  marketplace: x\n  marketplace_pin: \"0\"\n  context_budget_tokens: 100\n"
        "  receipt_branch: state\n  spool_dir: /tmp\n"
        "projects:\n  - slug: p\n    repo: o/p\n    default_branch: main\n"
        f"    worktree:\n      mbp: {repo}\n    tier_default: local\n    cadences: [wcad]\n")
    return fm


def test_launch_isolates_writes_cadence(git_repo, tmp_path, spool_dir, state_dir, monkeypatch):
    repo = git_repo()
    fm = _writes_foreman(tmp_path, repo, writes=True)
    conn = db.open_index(":memory:")
    monkeypatch.setattr(scheduler, "_spawn", _fake_spawn)
    now = dt.datetime(2026, 9, 7, 3, 17)
    res = scheduler._launch_run(fm, state_dir, spool_dir, conn, project="p", cadence="wcad",
                                tier="local", host="mbp", now=now)
    assert res["isolated"] is True
    assert Path(_fake_spawn.last_cwd).parent == spool_dir / "worktrees"   # ran in the throwaway
    # the scheduler owns receipt emission for launched runs: build_receipt runs after claude
    assert "collectors.build_receipt" in _fake_spawn.last_argv[-1]
    lc = budget.load_launches(spool_dir)[0]
    assert lc["worktree"] and lc["repo"] == str(repo)        # recorded for cleanup
    assert Path(lc["worktree"]).is_dir() and len(_wt_list(repo)) == 2


def test_reap_requests_approval_for_open_pr_run(git_repo, spool_dir, state_dir):
    repo = git_repo()
    _commit_branch(repo, "foreman/wcad")                     # the run left a branch with a commit
    conn = db.open_index(":memory:")
    budget.record_launch(spool_dir, {
        "run_id": "01J00000000000000000000WT2", "pid": 2_000_000_000, "project": "p",
        "cadence": "wcad", "tier": "local", "host": "mbp", "started": "2026-09-06T11:00:00Z",
        "lock_key": "wcad:p", "budget": {}, "worktree": None, "repo": str(repo),
        "open_pr": True, "branch": "foreman/wcad", "base": "main"})
    budget.reap(spool_dir, state_dir, conn, now=dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc))

    pending = D.load_pending(state_dir, "p")
    approvals = [d for _, d in pending if d["kind"] == "approve_push"]
    assert len(approvals) == 1
    assert approvals[0]["payload"]["branch"] == "foreman/wcad"
    assert approvals[0]["payload"]["ahead"] == 1


def test_reap_no_approval_when_branch_unchanged(git_repo, spool_dir, state_dir):
    repo = git_repo()                                        # no branch created -> nothing to push
    conn = db.open_index(":memory:")
    budget.record_launch(spool_dir, {
        "run_id": "01J00000000000000000000WT3", "pid": 2_000_000_000, "project": "p",
        "cadence": "wcad", "tier": "local", "host": "mbp", "started": "2026-09-06T11:00:00Z",
        "lock_key": "wcad:p", "budget": {}, "worktree": None, "repo": str(repo),
        "open_pr": True, "branch": "foreman/wcad", "base": "main"})
    budget.reap(spool_dir, state_dir, conn, now=dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc))
    assert D.load_pending(state_dir, "p") == []             # no commits -> no approval requested


def test_launch_in_place_for_readonly_cadence(git_repo, tmp_path, spool_dir, state_dir, monkeypatch):
    repo = git_repo()
    fm = _writes_foreman(tmp_path, repo, writes=False)
    conn = db.open_index(":memory:")
    monkeypatch.setattr(scheduler, "_spawn", _fake_spawn)
    now = dt.datetime(2026, 9, 7, 3, 17)
    res = scheduler._launch_run(fm, state_dir, spool_dir, conn, project="p", cadence="wcad",
                                tier="local", host="mbp", now=now)
    assert res["isolated"] is False
    assert _fake_spawn.last_cwd == str(repo)                   # ran in the project's own tree
    assert budget.load_launches(spool_dir) == []             # nothing to supervise
    assert len(_wt_list(repo)) == 1                           # no throwaway made

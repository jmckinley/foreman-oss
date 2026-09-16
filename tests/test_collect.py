"""The collect orchestrator: seed_dimensions + run_all wiring every collector into the index,
and the CLI guardrail."""
from collectors import collect, db


def _foreman(tmp_path, worktree):
    fd = tmp_path / "fm"
    (fd / "cadences").mkdir(parents=True)
    (fd / "cadences" / "docs-sync.yaml").write_text(
        'slug: docs-sync\ntier: cloud\nschedule: "19 9 * * *"\n'
        'metrics: [x]\nverdict: {green: "x == 0", amber: "x < 5", red: otherwise}\n')
    (fd / "registry.yaml").write_text(
        "version: 1\n"
        "hosts:\n  mbp: {os: darwin, role: interactive}\n"
        "projects:\n"
        "  - slug: alpha\n    repo: o/alpha\n    default_branch: main\n"
        f"    worktree: {{mbp: {worktree}}}\n    tier_default: cloud\n"
        "    cadences: [docs-sync]\n    escalation: {github_issues: false}\n")
    return fd


def test_seed_dimensions():
    conn = db.open_index(":memory:")
    reg = {"hosts": {"mbp": {"os": "darwin", "role": "interactive"}},
           "projects": [{"slug": "alpha", "repo": "o/alpha", "default_branch": "main",
                         "tier_default": "cloud"}]}
    collect.seed_dimensions(conn, reg, "mbp", "2026-09-06T12:00:00Z")
    assert conn.execute("SELECT last_seen FROM host WHERE name='mbp'").fetchone()["last_seen"]
    assert conn.execute("SELECT repo FROM project WHERE slug='alpha'").fetchone()["repo"] == "o/alpha"


def test_run_all_wires_collectors(tmp_path, state_dir, make_receipt, git_repo, now):
    repo = git_repo()                                   # a real git worktree on 'mbp'
    fd = _foreman(tmp_path, repo)
    make_receipt("alpha", "docs-sync", "2026-09-06T08:00:00Z", "green")
    idx = str(tmp_path / "index.db")
    out = collect.run_all(index_path=idx, state_dir=state_dir, foreman_dir=fd, host="mbp",
                          now=now, gh=lambda *a, **k: None)   # gh stub -> zeroed github_state
    assert out["runs"] >= 1 and out["git_state"] == 1 and out["github_state"] == 1
    assert out["taken"] == "2026-09-06T12:00:00Z"
    conn = db.open_index(idx)
    assert conn.execute("SELECT COUNT(*) c FROM git_state WHERE project='alpha'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM run WHERE project='alpha'").fetchone()["c"] >= 1


def test_run_all_skips_absent_worktree(tmp_path, state_dir, now):
    fd = _foreman(tmp_path, tmp_path / "does-not-exist")
    out = collect.run_all(index_path=str(tmp_path / "i.db"), state_dir=state_dir, foreman_dir=fd,
                          host="mbp", now=now, gh=lambda *a, **k: None)
    assert out["git_state"] == 0 and out["github_state"] == 1   # no worktree, but repo snapshot runs


def test_cli_requires_state_dir(monkeypatch, capsys):
    monkeypatch.delenv("FOREMAN_STATE_DIR", raising=False)
    assert collect.main(["--index", ":memory:"]) == 2
    assert "state-dir" in capsys.readouterr().err


def test_cli_happy(tmp_path, state_dir, make_receipt, git_repo, monkeypatch, capsys):
    repo = git_repo()
    fd = _foreman(tmp_path, repo)
    make_receipt("alpha", "docs-sync", "2026-09-06T08:00:00Z", "green")
    monkeypatch.setattr("collectors.github_state._gh_json", lambda *a, **k: None)
    monkeypatch.setenv("FOREMAN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("FOREMAN_DIR", str(fd))
    assert collect.main(["--index", str(tmp_path / "index.db"), "--host", "mbp"]) == 0
    assert "collected:" in capsys.readouterr().out

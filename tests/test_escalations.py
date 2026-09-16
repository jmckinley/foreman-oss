"""Escalation lifecycle: reconcile opens/comments/closes GitHub issues as a (project, cadence)
goes red -> stays red -> clears green, plus /close and the gh wrapper's parsing."""
import datetime as dt

from collectors import escalations as E, db

_CADENCE = ("slug: docs-sync\ntier: cloud\nschedule: \"19 9 * * *\"\n"
            "metrics: [x]\nverdict: {green: \"x == 0\", amber: \"x < 5\", red: otherwise}\n")


def _foreman(tmp_path, *, github_issues=True):
    fd = tmp_path / "fm"
    (fd / "cadences").mkdir(parents=True)
    (fd / "cadences" / "docs-sync.yaml").write_text(_CADENCE)
    (fd / "registry.yaml").write_text(
        "version: 1\nprojects:\n"
        "  - slug: alpha\n    repo: o/alpha\n    default_branch: main\n"
        "    worktree: {mbp: /tmp/alpha}\n    tier_default: cloud\n    cadences: [docs-sync]\n"
        f"    escalation: {{github_issues: {str(github_issues).lower()}, labels: [bug]}}\n")
    return fd


def test_unit_helpers():
    assert E._esc_id("a", "c", "T1") == E._esc_id("a", "c", "T1")
    assert E._esc_id("a", "c", "T1") != E._esc_id("a", "c", "T2")
    assert E.issue_title("alpha", "docs-sync") == "[foreman] docs-sync red on alpha"
    r = {"project": "alpha", "cadence": "docs-sync", "ended": "2026-09-06T08:00:00Z", "run_id": "R1"}
    assert E.receipt_relpath(r) == "receipts/alpha/docs-sync/20260906T080000Z-R1.json"
    hist = [{"verdict": "green"}, {"verdict": "red"}, {"verdict": "red"}]
    assert E.streak_start(hist) is hist[1]                      # first of the red streak
    assert E._latest_green([{"verdict": "red"}, {"verdict": "green"}])["verdict"] == "green"
    assert E._latest_green([{"verdict": "red"}]) is None


def test_reconcile_opens_comments_closes(tmp_path, state_dir, make_receipt, fake_github, now):
    fd = _foreman(tmp_path)
    conn = db.open_index(":memory:")
    make_receipt("alpha", "docs-sync", "2026-09-06T08:00:00Z", "red", next_action="fix it")
    r1 = E.reconcile(conn, state_dir, fd, github=fake_github, now=now)
    assert r1["opened_issues"] == 1 and r1["open"] == 1
    row = conn.execute("SELECT * FROM escalation WHERE resolved IS NULL").fetchone()
    assert int(row["github_issue"]) in fake_github.issues          # stored as TEXT
    assert fake_github.issues[int(row["github_issue"])]["state"] == "open"

    # a fresh red run (new run_id) comments rather than opening a second issue
    make_receipt("alpha", "docs-sync", "2026-09-06T09:00:00Z", "red", next_action="still broken")
    r2 = E.reconcile(conn, state_dir, fd, github=fake_github, now=now)
    assert r2["opened_issues"] == 0 and r2["commented"] == 1

    # a green run clears it: escalation resolved + issue closed
    make_receipt("alpha", "docs-sync", "2026-09-06T10:00:00Z", "green")
    r3 = E.reconcile(conn, state_dir, fd, github=fake_github, now=now)
    assert r3["closed"] == 1 and r3["open"] == 0
    assert conn.execute("SELECT resolved FROM escalation").fetchone()["resolved"] is not None
    assert all(i["state"] == "closed" for i in fake_github.issues.values())


def test_reconcile_no_issue_when_disabled(tmp_path, state_dir, make_receipt, fake_github, now):
    fd = _foreman(tmp_path, github_issues=False)
    conn = db.open_index(":memory:")
    make_receipt("alpha", "docs-sync", "2026-09-06T08:00:00Z", "red")
    r = E.reconcile(conn, state_dir, fd, github=fake_github, now=now)
    assert r["opened_issues"] == 0                       # records the escalation, no gh issue
    assert conn.execute("SELECT github_issue FROM escalation").fetchone()["github_issue"] is None


def test_close(tmp_path, state_dir, make_receipt, fake_github, now):
    fd = _foreman(tmp_path)
    conn = db.open_index(":memory:")
    make_receipt("alpha", "docs-sync", "2026-09-06T08:00:00Z", "red")
    E.reconcile(conn, state_dir, fd, github=fake_github, now=now)
    eid = conn.execute("SELECT id FROM escalation").fetchone()["id"]
    assert E.close(conn, eid, "handled manually", foreman_dir=fd, github=fake_github) is True
    assert conn.execute("SELECT resolved FROM escalation WHERE id=?", (eid,)).fetchone()["resolved"]
    assert all(i["state"] == "closed" for i in fake_github.issues.values())
    # second close is a no-op; unknown id too
    assert E.close(conn, eid, "again", foreman_dir=fd, github=fake_github) is False
    assert E.close(conn, 999, "x", foreman_dir=fd, github=fake_github) is False


def test_github_wrapper_parsing():
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "issue" and args[1] == "list":
            return '[{"number": 7, "title": "[foreman] docs-sync red on alpha"}]'
        if args[0] == "issue" and args[1] == "create":
            return "https://github.com/o/alpha/issues/42\n"
        return ""

    gh = E.GitHub(run=run)
    assert gh.find_issue("o/alpha", "[foreman] docs-sync red on alpha") == 7
    assert gh.find_issue("o/alpha", "missing title") is None
    assert gh.open_issue("o/alpha", "t", "b", ["bug"]) == 42
    gh.comment("o/alpha", 42, "still red")
    gh.close_issue("o/alpha", 42, "done")
    assert ["issue", "comment", "42", "--repo", "o/alpha", "--body", "still red"] in calls
    assert any(a[:2] == ["issue", "close"] for a in calls)


def test_cli_reconcile_and_close(tmp_path, state_dir, make_receipt, monkeypatch):
    fd = _foreman(tmp_path, github_issues=False)      # no gh subprocess in the CLI path
    idx = str(tmp_path / "index.db")
    db.open_index(idx).close()
    # the CLI's reconcile uses real now(), so keep the receipt inside the staleness window
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    make_receipt("alpha", "docs-sync", recent, "red")
    monkeypatch.setenv("FOREMAN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("FOREMAN_DIR", str(fd))
    monkeypatch.setenv("FOREMAN_INDEX", idx)
    assert E.main(["reconcile"]) == 0
    conn = db.open_index(idx)
    eid = conn.execute("SELECT id FROM escalation").fetchone()["id"]
    conn.close()
    assert E.main(["close", str(eid), "done"]) == 0
    assert E.main(["close", "123456", "nope"]) == 1   # unknown -> non-zero

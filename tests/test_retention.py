"""retention — the index/spool pruning + aggregation rollups (SPEC §15)."""
import datetime as dt
import os

from collectors import retention as R, db

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)


def test_prune_config_snapshots_keeps_one_per_week_per_hash():
    conn = db.open_index(":memory:")
    # three old snapshots, same project/host, same ISO week, same hash -> keep one
    for i, day in enumerate(("2026-01-05", "2026-01-06", "2026-01-07")):
        conn.execute("INSERT INTO config_snapshot(id,project,host,taken,hash) VALUES(?,?,?,?,?)",
                     (i + 1, "alpha", "mbp", f"{day}T08:00:00Z", "h1"))
    conn.commit()
    assert R.prune_config_snapshots(conn, NOW) == 2
    assert conn.execute("SELECT COUNT(*) c FROM config_snapshot").fetchone()["c"] == 1


def test_prune_sessions_over_180d():
    conn = db.open_index(":memory:")
    conn.execute("INSERT INTO session(session_uuid,project,started) VALUES('old','a','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO session(session_uuid,project,started) VALUES('new','a','2026-09-01T00:00:00Z')")
    conn.commit()
    assert R.prune_sessions(conn, NOW) == 1
    assert conn.execute("SELECT session_uuid FROM session").fetchone()["session_uuid"] == "new"


def test_aggregate_telemetry_collapses_to_monthly():
    conn = db.open_index(":memory:")
    for day, tok in (("2024-01-01", 100), ("2024-01-20", 50)):     # >2y old, same month
        db.upsert(conn, "telemetry_day", {
            "day": day, "project": "a", "host": "mbp", "model": "opus", "sessions": 1,
            "cost_usd": 1.0, "tokens": tok, "lines_added": 0, "lines_removed": 0, "commits": 0,
            "prs": 0, "edit_accept": 0, "edit_reject": 0, "active_seconds": 0},
            keys=["day", "project", "host", "model"])
    assert R.aggregate_telemetry(conn, NOW) == 2
    rows = conn.execute("SELECT day, tokens FROM telemetry_day").fetchall()
    assert len(rows) == 1 and rows[0]["day"] == "2024-01" and rows[0]["tokens"] == 150


def test_prune_repo_state_last_of_day():
    conn = db.open_index(":memory:")
    for hh in ("08", "09", "10"):
        conn.execute("INSERT INTO git_state(project,host,taken,branch) VALUES('a','mbp',?, 'main')",
                     (f"2026-06-01T{hh}:00:00Z",))
    conn.execute("INSERT INTO github_state(project,taken,open_prs) VALUES('a','2026-06-01T08:00:00Z',1)")
    conn.execute("INSERT INTO github_state(project,taken,open_prs) VALUES('a','2026-06-01T09:00:00Z',2)")
    conn.commit()
    out = R.prune_repo_state(conn, NOW)
    assert out["git_state"] == 2 and out["github_state"] == 1
    assert conn.execute("SELECT taken FROM git_state").fetchone()["taken"] == "2026-06-01T10:00:00Z"


def test_prune_spool(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    old = spool / "old.jsonl"; old.write_text("x")
    recent = spool / "recent.partial.json"; recent.write_text("y")
    old_ts = (NOW - dt.timedelta(days=30)).timestamp()
    os.utime(old, (old_ts, old_ts))
    assert R.prune_spool(spool, NOW, days=7) == 1
    assert not old.exists() and recent.exists()
    assert R.prune_spool(tmp_path / "nope", NOW) == 0        # absent dir -> 0


def test_run_all():
    conn = db.open_index(":memory:")
    conn.execute("INSERT INTO session(session_uuid,project,started) VALUES('old','a','2026-01-01T00:00:00Z')")
    conn.commit()
    out = R.run_all(conn, now=NOW)
    assert out["sessions_pruned"] == 1 and "repo_state_pruned" in out

"""Retention (SPEC.md section 15), run as a weekly local maintenance job.

Everything here prunes the index cache or the spool; receipts are never touched (they live
in git forever). Each function takes an explicit ``now`` so the job is deterministic and
testable. Run all of it with ``python -m collectors.retention --index <db> --spool <dir>``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from collectors import db

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _cut(now: dt.datetime, days: int) -> str:
    return (now - dt.timedelta(days=days)).strftime(_ISO)


def prune_config_snapshots(conn, now: dt.datetime) -> int:
    """Keep every snapshot for 90 days; older than that keep one per ISO week per
    (project, host) plus any whose hash differs from its predecessor."""
    cutoff = _cut(now, 90)
    rows = conn.execute(
        "SELECT id, project, host, taken, hash FROM config_snapshot WHERE taken < ? "
        "ORDER BY project, host, taken", (cutoff,)).fetchall()
    keep: set[int] = set()
    prev_key = None
    prev_hash = None
    weeks: set = set()
    for r in rows:
        gkey = (r["project"], r["host"])
        if gkey != prev_key:
            prev_hash, weeks = None, set()
            prev_key = gkey
        iso_week = r["taken"][:4] + "-W" + str(dt.date.fromisoformat(r["taken"][:10]).isocalendar()[1])
        if r["hash"] != prev_hash or iso_week not in weeks:
            keep.add(r["id"])
            weeks.add(iso_week)
        prev_hash = r["hash"]
    doomed = [r["id"] for r in rows if r["id"] not in keep]
    for sid in doomed:
        conn.execute("DELETE FROM config_key WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM component WHERE snapshot_id = ?", (sid,))
        conn.execute("DELETE FROM config_snapshot WHERE id = ?", (sid,))
    conn.commit()
    return len(doomed)


def prune_sessions(conn, now: dt.datetime) -> int:
    """Sessions and their decisions/skill fires older than 180 days are deleted."""
    cutoff = _cut(now, 180)
    old = [r["session_uuid"] for r in conn.execute(
        "SELECT session_uuid FROM session WHERE started IS NOT NULL AND started < ?", (cutoff,))]
    for uuid in old:
        conn.execute("DELETE FROM session_decision WHERE session_uuid = ?", (uuid,))
        conn.execute("DELETE FROM skill_fire WHERE session_uuid = ?", (uuid,))
        conn.execute("DELETE FROM session WHERE session_uuid = ?", (uuid,))
    conn.commit()
    return len(old)


def aggregate_telemetry(conn, now: dt.datetime) -> int:
    """Telemetry older than 2 years collapses from daily to one row per month."""
    cutoff = _cut(now, 730)
    rows = conn.execute("SELECT * FROM telemetry_day WHERE day < ? AND length(day) = 10",
                        (cutoff,)).fetchall()
    monthly: dict[tuple, dict] = {}
    for r in rows:
        # Monthly rows use a 7-char day (YYYY-MM) so the daily-only DELETE below (length 10)
        # never touches them.
        key = (r["day"][:7], r["project"], r["host"], r["model"])
        agg = monthly.setdefault(key, {c: 0 for c in
              ("sessions", "tokens", "lines_added", "lines_removed", "commits", "prs",
               "edit_accept", "edit_reject", "active_seconds")})
        agg["_cost"] = agg.get("_cost", 0.0) + (r["cost_usd"] or 0)
        for c in agg:
            if c != "_cost":
                agg[c] += r[c] or 0
    for (day, project, host, model), agg in monthly.items():
        db.upsert(conn, "telemetry_day", {
            "day": day, "project": project, "host": host, "model": model,
            "sessions": agg["sessions"], "cost_usd": agg["_cost"], "tokens": agg["tokens"],
            "lines_added": agg["lines_added"], "lines_removed": agg["lines_removed"],
            "commits": agg["commits"], "prs": agg["prs"], "edit_accept": agg["edit_accept"],
            "edit_reject": agg["edit_reject"], "active_seconds": agg["active_seconds"],
        }, keys=["day", "project", "host", "model"])
    conn.execute("DELETE FROM telemetry_day WHERE day < ? AND length(day) = 10", (cutoff,))
    conn.commit()
    return len(rows)


def _prune_last_of_day(conn, table: str, group_cols: list[str], now: dt.datetime) -> int:
    """Beyond 30 days keep only the last snapshot per group per day."""
    cutoff = _cut(now, 30)
    rows = conn.execute(f"SELECT rowid, taken, {', '.join(group_cols)} FROM {table} "
                        f"WHERE taken < ? ORDER BY taken", (cutoff,)).fetchall()
    last_by: dict[tuple, int] = {}
    for r in rows:
        key = tuple(r[c] for c in group_cols) + (r["taken"][:10],)
        last_by[key] = r["rowid"]  # later taken overwrites -> last of day wins
    keep = set(last_by.values())
    doomed = [r["rowid"] for r in rows if r["rowid"] not in keep]
    for rid in doomed:
        conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rid,))
    conn.commit()
    return len(doomed)


def prune_repo_state(conn, now: dt.datetime) -> dict:
    return {"git_state": _prune_last_of_day(conn, "git_state", ["project", "host"], now),
            "github_state": _prune_last_of_day(conn, "github_state", ["project"], now)}


def prune_spool(spool_dir: Path, now: dt.datetime, *, days: int = 7) -> int:
    """Spool files older than 7 days (drained plus grace) are deleted."""
    spool_dir = Path(os.path.expanduser(spool_dir))
    if not spool_dir.is_dir():
        return 0
    cutoff = (now - dt.timedelta(days=days)).timestamp()
    removed = 0
    for f in list(spool_dir.glob("*.jsonl")) + list(spool_dir.glob("*.partial.json")):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def run_all(conn, *, now: dt.datetime | None = None, state_dir=None, spool_dir=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {
        "config_snapshots_pruned": prune_config_snapshots(conn, now),
        "sessions_pruned": prune_sessions(conn, now),
        "telemetry_aggregated": aggregate_telemetry(conn, now),
        "repo_state_pruned": prune_repo_state(conn, now),
    }
    if spool_dir:
        out["spool_pruned"] = prune_spool(Path(spool_dir), now)
    if state_dir:
        from collectors import decisions
        out["decisions"] = decisions.gc(Path(state_dir), now=now)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="retention")
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    ap.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL"))
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    args = ap.parse_args(argv)
    conn = db.open_index(args.index or str(db.default_path()))
    import json
    print(json.dumps(run_all(conn, spool_dir=args.spool, state_dir=args.state_dir), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

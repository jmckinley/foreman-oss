"""C7 -- run state and quota from the scheduler surfaces.

Reads cloud-routine and desktop-task history and the plan quota, writing ``run`` status and
the ``quota`` row (SPEC.md section 9). The underlying surfaces are undocumented and change
between versions, so both inputs are injected: a ``quota_fetch`` callable and a
``routine_history`` list. The defaults shell out best-effort and degrade to "unavailable"
rather than guessing, because a wrong quota number would defer real work (the quota guard,
§16.3) or a wrong run row would mask a red.

What C7 adds beyond receipts: a scheduler run that started but died before writing a receipt
leaves no receipt-derived ``run`` row. C7 records it (status from history) so the crash is
visible in the index instead of looking like a run that never fired.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path

from collectors import db, quota

_ISO = "%Y-%m-%dT%H:%M:%SZ"


# ------------------------------------------------------------------------- quota

def default_quota_fetch() -> dict | None:
    """Best-effort quota read. FOREMAN_QUOTA_CMD should print JSON {pct_used, plan, window_resets}.

    There is no documented quota CLI, so the operator wires the command (e.g. a usage-API
    call or a ccusage-style tool). Absent or unparseable output yields None -- the guard then
    simply does not fire, rather than acting on a bad number.
    """
    cmd = os.environ.get("FOREMAN_QUOTA_CMD")
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and data.get("pct_used") is not None else None


def collect_quota(conn, *, fetch=default_quota_fetch, now: dt.datetime | None = None) -> dict | None:
    data = fetch()
    if not data or data.get("pct_used") is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    quota.record(conn, pct_used=float(data["pct_used"]), plan=data.get("plan"),
                 window_resets=data.get("window_resets"), taken=now.strftime(_ISO))
    return data


# -------------------------------------------------------------- run reconciliation

def _history_run_id(rec: dict) -> str:
    """Deterministic id for a receipt-less scheduler run (not a ULID; receipts own those)."""
    h = hashlib.sha1(f"{rec['project']}|{rec['cadence']}|{rec['started']}".encode()).hexdigest()
    return "rt-" + h[:22]


def reconcile_runs(conn, history: list[dict]) -> int:
    """Record scheduler runs that produced no receipt. Idempotent; skips a (project, cadence)
    day that already has a run (receipt-derived or previously recorded here)."""
    added = 0
    for rec in history:
        day = rec["started"][:10]
        exists = conn.execute(
            "SELECT 1 FROM run WHERE project = ? AND cadence = ? AND substr(started,1,10) = ? "
            "LIMIT 1", (rec["project"], rec["cadence"], day)).fetchone()
        if exists:
            continue
        db.upsert(conn, "run", {
            "run_id": _history_run_id(rec),
            "cadence": rec["cadence"], "project": rec["project"],
            "host": rec.get("host", "cloud"), "tier": rec.get("tier", "cloud"),
            "started": rec["started"], "ended": rec.get("ended"),
            "status": rec.get("status", "failed"), "verdict": rec.get("verdict"),
            "cost_usd": None, "tokens_in": None, "tokens_out": None,
            "cc_version": None, "receipt_path": None,
        }, keys=["run_id"])
        added += 1
    conn.commit()
    return added


def collect(conn, *, quota_fetch=default_quota_fetch, routine_history=None,
            now: dt.datetime | None = None) -> dict:
    q = collect_quota(conn, fetch=quota_fetch, now=now)
    runs = reconcile_runs(conn, routine_history or [])
    return {"quota": q, "runs_recorded": runs}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="runstate")
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    ap.add_argument("--history", help="JSON file of routine/task run records")
    args = ap.parse_args(argv)
    conn = db.open_index(args.index or str(db.default_path()))
    history = []
    if args.history and Path(args.history).is_file():
        history = json.loads(Path(args.history).read_text())
    print(json.dumps(collect(conn, routine_history=history), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

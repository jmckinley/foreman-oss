"""Locking and identity (SPEC.md section 14).

Three hosts and a cloud tier can all be told to run the same cadence on the same project.
The lock keeps two agents from pushing the same branch. A lock is stealable only once it
has expired, so a crashed run never blocks forever.

The loser of a race does not fail silently: it emits a ``status: locked`` receipt, which
is how a suppressed duplicate stays visible in the brief.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from collectors.build_receipt import write_receipt

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _iso(t: dt.datetime) -> str:
    return t.strftime(_ISO)


def lock_key(template: str, project: str) -> str:
    """Render a cadence's lock_key template, e.g. 'docs-sync:{project}'."""
    return template.replace("{project}", project)


def acquire(conn, key: str, host: str, run_id: str, *, timeout_minutes: int,
            now: dt.datetime | None = None) -> bool:
    """Try to take the lock. Returns True iff this run now holds it.

    Expiry is ``timeout_minutes + 5`` (SPEC.md section 14): a crashed run's lock becomes
    stealable five minutes after its own timeout. The ON CONFLICT clause only steals an
    already-expired lock; a live lock is left untouched and this returns False.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(minutes=timeout_minutes + 5)
    conn.execute(
        """
        INSERT INTO lock(key, holder_host, run_id, acquired, expires)
        VALUES (:key, :host, :run, :now, :expires)
        ON CONFLICT(key) DO UPDATE SET
          holder_host = excluded.holder_host,
          run_id      = excluded.run_id,
          acquired    = excluded.acquired,
          expires     = excluded.expires
        WHERE lock.expires < :now
        """,
        {"key": key, "host": host, "run": run_id, "now": _iso(now), "expires": _iso(expires)},
    )
    conn.commit()
    # We hold it iff the row now names our run_id (covers both fresh insert and steal).
    row = conn.execute("SELECT run_id FROM lock WHERE key = :key", {"key": key}).fetchone()
    return bool(row) and row["run_id"] == run_id


def release(conn, key: str, run_id: str) -> None:
    """Release only if we still hold it (an expired-and-stolen lock is not ours to clear)."""
    conn.execute("DELETE FROM lock WHERE key = :key AND run_id = :run",
                 {"key": key, "run": run_id})
    conn.commit()


def holder(conn, key: str):
    return conn.execute("SELECT * FROM lock WHERE key = :key", {"key": key}).fetchone()


def emit_locked_receipt(state_dir: Path, *, run_id: str, cadence: str, project: str,
                        host: str, tier: str, started: str, ended: str, cc_version: str,
                        holder_host: str, lock_state: str = "held") -> Path:
    """Write the receipt for a run that could not acquire the lock.

    verdict is green (nothing is wrong; another holder is doing the work) and status is
    locked, so the brief filters it out of the verdict history while still recording that a
    duplicate dispatch happened. ``lock_state`` is 'unverified' when a cloud run could not
    reach the index and proceeded anyway.
    """
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "cadence": cadence,
        "project": project,
        "host": host,
        "tier": tier,
        "started": started,
        "ended": ended,
        "status": "locked",
        "verdict": "green",
        "lock": lock_state,
        "cc_version": cc_version,
        "metrics": {},
        "next_action": f"Wait for {holder_host} to finish {cadence} on {project}.",
        "notes": f"lock held by {holder_host}; duplicate dispatch suppressed",
    }
    return write_receipt(Path(state_dir), receipt)

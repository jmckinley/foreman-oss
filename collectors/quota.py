"""Quota guard (SPEC.md sections 16.3, 12).

Cadences consume the same usage limits as interactive work. Below 15% headroom Foreman
defers every non-red cadence to the next window and says so in the brief. The ``quota`` row
is populated by C7 (routine/desktop history); this module reads it and decides.
"""

from __future__ import annotations

import datetime as dt

from collectors import db

DEFER_BELOW_HEADROOM = 15.0


def record(conn, *, pct_used: float, plan: str | None = None,
           window_resets: str | None = None, taken: str | None = None) -> None:
    taken = taken or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.upsert(conn, "quota", {"taken": taken, "plan": plan,
                              "window_resets": window_resets, "pct_used": pct_used},
              keys=["taken"])
    conn.commit()


def latest(conn):
    return conn.execute("SELECT * FROM quota ORDER BY taken DESC LIMIT 1").fetchone()


def headroom(conn) -> float | None:
    row = latest(conn)
    return None if row is None or row["pct_used"] is None else 100.0 - row["pct_used"]


def low(conn, threshold: float = DEFER_BELOW_HEADROOM) -> bool:
    h = headroom(conn)
    return h is not None and h < threshold

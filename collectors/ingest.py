"""Ingest receipts into the index: ``run`` and ``metric``.

Receipts on the ``state`` branch are the source of truth; these rows are a query cache
(SPEC.md section 8). Ingestion is a full idempotent scan, upserting by natural keys, so
dropping ``index.db`` and re-ingesting reproduces identical rows.

Escalations are owned by ``collectors/escalations.py`` (they carry GitHub-issue linkage that
must survive a re-ingest), not written here. Locked/skipped receipts are recorded as runs
(status distinguishes them) but are coordination artifacts, not verdicts.
"""

from __future__ import annotations

from collectors import db
from collectors.brief import load_receipts
from pathlib import Path


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ingest_run(conn, receipt: dict, receipt_path: str | None = None) -> None:
    tokens = receipt.get("tokens") or {}
    db.upsert(conn, "run", {
        "run_id": receipt["run_id"],
        "cadence": receipt["cadence"],
        "project": receipt["project"],
        "host": receipt["host"],
        "tier": receipt["tier"],
        "started": receipt["started"],
        "ended": receipt.get("ended"),
        "status": receipt["status"],
        "verdict": receipt.get("verdict"),
        "cost_usd": receipt.get("cost_usd"),
        "tokens_in": tokens.get("in"),
        "tokens_out": tokens.get("out"),
        "cc_version": receipt.get("cc_version"),
        "receipt_path": receipt_path,
    }, keys=["run_id"])

    for name, value in (receipt.get("metrics") or {}).items():
        db.upsert(conn, "metric", {
            "run_id": receipt["run_id"],
            "name": name,
            "value": float(value) if _num(value) or isinstance(value, bool) else None,
            "text_value": None if _num(value) or isinstance(value, bool) else str(value),
        }, keys=["run_id", "name"])


def ingest_receipts(conn, state_dir: str | Path) -> dict:
    """Full idempotent ingest of every receipt under ``state_dir/receipts`` (run + metric)."""
    groups = load_receipts(Path(state_dir), include_coordination=True)
    runs = 0
    for history in groups.values():
        for receipt in history:
            ingest_run(conn, receipt)
            runs += 1
    conn.commit()
    return {"runs": runs, "pairs": len(groups)}

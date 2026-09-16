"""/dispatch -- fire a cadence off-cycle on the correct tier (SPEC.md sections 11, 16.3).

Dispatch is a decision producer (§18): it enqueues a ``dispatch_cadence`` decision that the
app-side drain or the cloud routine picks up, so it works even when the target project has no
live session. Before enqueuing it honours the quota guard: under 15% headroom every non-red
cadence is deferred to the next window and the caller is told so.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from collectors import decisions, quota


def _is_red(conn, project: str, cadence: str) -> bool:
    """Red now = an open escalation for the pair, or the latest run verdict is red."""
    esc = conn.execute(
        "SELECT 1 FROM escalation WHERE project = ? AND cadence = ? AND resolved IS NULL",
        (project, cadence)).fetchone()
    if esc:
        return True
    run = conn.execute(
        "SELECT verdict, status FROM run WHERE project = ? AND cadence = ? "
        "AND status NOT IN ('locked','skipped') ORDER BY ended DESC LIMIT 1",
        (project, cadence)).fetchone()
    return bool(run) and (run["verdict"] == "red" or run["status"] in ("failed", "timeout"))


def dispatch(conn, registry: dict, state_dir, project: str, cadence: str, *,
             actor: str = "operator", now: dt.datetime | None = None) -> dict:
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    if proj is None:
        raise ValueError(f"unknown project {project!r}")
    if cadence not in (proj.get("cadences") or []):
        raise ValueError(f"cadence {cadence!r} does not apply to {project}")

    # Quota guard: defer non-red cadences below 15% headroom.
    if quota.low(conn) and not _is_red(conn, project, cadence):
        h = quota.headroom(conn)
        return {"deferred": True,
                "reason": f"quota headroom {h:.0f}% < 15%; deferring non-red {cadence} on {project}"}

    path, dec = decisions.enqueue(Path(state_dir), project, "dispatch_cadence",
                                  {"cadence": cadence}, actor)
    return {"deferred": False, "dispatched": dec["decision_id"], "project": project,
            "cadence": cadence}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os
    from collectors import db

    ap = argparse.ArgumentParser(prog="dispatch")
    ap.add_argument("project")
    ap.add_argument("cadence")
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    args = ap.parse_args(argv)
    registry = yaml.safe_load((Path(args.foreman_dir) / "registry.yaml").read_text())
    conn = db.open_index(args.index or str(db.default_path()))
    print(json.dumps(dispatch(conn, registry, args.state_dir, args.project, args.cadence)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

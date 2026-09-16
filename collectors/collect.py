"""Run all index collectors: seed dimensions, ingest receipts, snapshot git and GitHub.

One command so "re-running all collectors" (BUILD.md M3) is a single operation. The index
is a cache: dropping it and running this again reproduces the receipt-derived rows exactly,
and refreshes the git/GitHub snapshots for the current host.

    python -m collectors.collect --state-dir <state> --index <path> [--host mbp]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import yaml

from collectors import (config_resolve, db, escalations, git_state, github_state, ingest,
                        runstate, telemetry, transcripts)

ROOT = Path(__file__).resolve().parent.parent
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def seed_dimensions(conn, registry: dict, host: str, taken: str) -> None:
    for name, h in (registry.get("hosts") or {}).items():
        db.upsert(conn, "host", {
            "name": name, "os": h.get("os"), "role": h.get("role"),
            "last_seen": taken if name == host else None,
        }, keys=["name"])
    for p in registry.get("projects") or []:
        db.upsert(conn, "project", {
            "slug": p["slug"], "repo": p.get("repo"),
            "default_branch": p.get("default_branch"), "tier_default": p.get("tier_default"),
            "marketplace_pin": p.get("marketplace_pin"), "active": 1,
        }, keys=["slug"])


def run_all(*, index_path, state_dir, foreman_dir, host, now=None, gh=github_state._gh_json,
            transcript_roots=None, spool_dir=None, quota_fetch=runstate.default_quota_fetch,
            routine_history=None, github=None, probe_fn=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    taken = now.strftime(_ISO)
    registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())

    conn = db.open_index(index_path)
    seed_dimensions(conn, registry, host, taken)
    ing = ingest.ingest_receipts(conn, state_dir)

    claude_home = Path(os.path.expanduser("~/.claude"))
    # The claude -p reconciliation probe spawns a headless Claude session PER PROJECT -- minutes
    # of wall time and real token spend on a bulk refresh. Default to a no-op probe (config is
    # still resolved from the settings layers; probe_ok just records the probe wasn't run).
    # Callers that want the effective-vs-declared reconciliation pass a real probe_fn.
    eff_probe = probe_fn if probe_fn is not None else (lambda _dir: None)
    git_rows = gh_rows = cfg_rows = 0
    for p in registry.get("projects") or []:
        wt = (p.get("worktree") or {}).get(host)
        wt_path = Path(os.path.expanduser(wt)) if wt else None
        if wt_path is not None and wt_path.exists():
            row = git_state.collect(wt_path, p["slug"], host,
                                    default_branch=p.get("default_branch", "main"),
                                    taken=taken, now=now)
            db.upsert(conn, "git_state", row, keys=["project", "host", "taken"])
            git_rows += 1
            # C6 config resolution needs the worktree on this host. Probe failures are
            # non-fatal (probe_ok records that the measurement was unavailable).
            try:
                config_resolve.resolve_project(
                    conn, project=p["slug"], host=host, taken=taken, cc_version="unknown",
                    claude_home=claude_home, project_dir=wt_path, env=dict(os.environ),
                    probe_fn=eff_probe)
                cfg_rows += 1
            except Exception:
                pass
        if p.get("repo"):
            row = github_state.collect(p["repo"], project=p["slug"], taken=taken, now=now, gh=gh)
            db.upsert(conn, "github_state", row, keys=["project", "taken"])
            gh_rows += 1

    # C1 transcripts. An envelope shape change is red but must not abort the other
    # collectors, so it is caught and surfaced rather than raised out of run_all.
    c1 = {"scanned": 0, "oversized": 0}
    c1_red = None
    if transcript_roots:
        try:
            c1 = transcripts.scan(conn, roots=transcript_roots, host=host, registry=registry)
        except transcripts.EnvelopeShapeError as exc:
            c1_red = str(exc)

    # C2 telemetry drain (idempotent recompute from the spool).
    c2 = {"records": 0, "days": 0}
    if spool_dir:
        c2 = telemetry.drain(conn, Path(spool_dir))

    # Escalation lifecycle: open/close GitHub issues from the current receipt verdicts.
    esc = escalations.reconcile(conn, state_dir, foreman_dir, now=now, github=github)

    # C7 run state + quota (scheduler surfaces). Both inputs degrade to no-op if absent.
    rs = runstate.collect(conn, quota_fetch=quota_fetch, routine_history=routine_history, now=now)

    conn.commit()
    result = {"runs": ing["runs"], "git_state": git_rows, "github_state": gh_rows,
              "config_snapshots": cfg_rows, "sessions": c1["scanned"],
              "oversized": c1["oversized"], "telemetry_days": c2["days"],
              "escalations_open": esc["open"], "quota": rs["quota"] is not None,
              "runs_recorded": rs["runs_recorded"], "taken": taken}
    if c1_red:
        result["c1_red"] = c1_red
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collect")
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mini"))
    ap.add_argument("--transcripts", action="store_true",
                    help="scan ~/.claude/projects for CLI session transcripts (C1)")
    ap.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL"),
                    help="telemetry spool to drain (C2)")
    ap.add_argument("--probe", action="store_true",
                    help="run the claude -p effective-config probe per project "
                         "(slow, real token spend; off by default)")
    args = ap.parse_args(argv)
    if not args.state_dir:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        return 2
    index_path = args.index or str(db.default_path())
    roots = [("~/.claude/projects", "cli")] if args.transcripts else None
    out = run_all(index_path=index_path, state_dir=args.state_dir,
                  foreman_dir=args.foreman_dir, host=args.host,
                  transcript_roots=roots, spool_dir=args.spool,
                  probe_fn=config_resolve._claude_probe if args.probe else None)
    print(f"collected: {out['runs']} runs, {out['git_state']} git_state, "
          f"{out['github_state']} github_state, {out['sessions']} sessions, "
          f"{out['telemetry_days']} telemetry_days @ {out['taken']}")
    if out.get("c1_red"):
        print(f"RED: C1 halted on envelope shape change: {out['c1_red']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

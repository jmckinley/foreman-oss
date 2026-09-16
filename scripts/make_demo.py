#!/usr/bin/env python3
"""Generate a synthetic demo instance (registry + index) for the PUBLIC repo, so the board renders
a believable fleet with zero real data — no real project names, paths, verdicts, or spend.

    python3 scripts/make_demo.py [outdir]      # default: examples/demo/{registry.yaml,index.db}

Point the board at it with:  FOREMAN_DIR=examples/demo FOREMAN_INDEX=examples/demo/index.db \\
                             python3 -m collectors.web
The output is deterministic (no randomness) so re-runs and screenshots are stable.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors import db  # noqa: E402

NOW = dt.datetime(2026, 3, 2, 15, 0, tzinfo=dt.timezone.utc)     # fixed "now" for stable output
LOOPS = ["docs-sync", "quality-review", "security-review", "perf-review"]

# (slug, git_state, github_state) — a spread of green/amber/red so every feature shows.
PROJECTS = [
    ("acme-api", dict(branch="main", ahead=0, behind=0, dirty_files=2, stale_branches=0,
                      largest_blob_mb=3, force_pushes_7d=0),
     dict(open_prs=1, failing_checks=0, security_alerts=0, dependabot_open=0, oldest_pr_days=2,
          issues_open=4)),
    ("widgetco-web", dict(branch="main", ahead=1, behind=0, dirty_files=12, stale_branches=4,
                          largest_blob_mb=6, force_pushes_7d=0),
     dict(open_prs=5, agent_prs=2, failing_checks=0, security_alerts=0, dependabot_open=3,
          oldest_pr_days=19, issues_open=11)),
    ("payments-svc", dict(branch="main", ahead=0, behind=3, dirty_files=1, stale_branches=1,
                          largest_blob_mb=4, force_pushes_7d=0),
     dict(open_prs=8, failing_checks=3, security_alerts=2, dependabot_open=6, oldest_pr_days=9,
          issues_open=22)),
    ("mobile-app", dict(branch="develop", ahead=16, behind=0, dirty_files=54, stale_branches=2,
                        largest_blob_mb=48, force_pushes_7d=0),
     dict(open_prs=3, failing_checks=1, security_alerts=0, dependabot_open=0, oldest_pr_days=6,
          issues_open=7)),
    ("data-pipeline", dict(branch="main", ahead=0, behind=0, dirty_files=3, stale_branches=12,
                           orphan_worktrees=1, largest_blob_mb=180, force_pushes_7d=2),
     dict(open_prs=10, failing_checks=0, security_alerts=0, dependabot_open=25, oldest_pr_days=140,
          issues_open=63)),
    ("docs-site", dict(branch="main", ahead=0, behind=0, dirty_files=0, stale_branches=0,
                       largest_blob_mb=1, force_pushes_7d=0),
     dict(open_prs=0, failing_checks=0, security_alerts=0, dependabot_open=0, oldest_pr_days=0,
          issues_open=1)),
]

# per (project, loop) verdict trend, oldest→newest — drives the sparklines + latest verdict.
TRENDS = {
    ("acme-api", "docs-sync"): "gggggg", ("acme-api", "quality-review"): "ggggag",
    ("acme-api", "security-review"): "gggggg", ("acme-api", "perf-review"): "gggggg",
    ("widgetco-web", "docs-sync"): "ggagag", ("widgetco-web", "quality-review"): "gagaaa",
    ("widgetco-web", "security-review"): "gggggg", ("widgetco-web", "perf-review"): "ggggag",
    ("payments-svc", "docs-sync"): "gggagg", ("payments-svc", "quality-review"): "gaarrr",
    ("payments-svc", "security-review"): "gggarr", ("payments-svc", "perf-review"): "ggaarr",
    ("mobile-app", "docs-sync"): "gggggg", ("mobile-app", "quality-review"): "ggggaa",
    ("mobile-app", "perf-review"): "gggarr",
    ("data-pipeline", "docs-sync"): "ggaggg", ("data-pipeline", "quality-review"): "gaaarr",
    ("data-pipeline", "security-review"): "gggggg",
    ("docs-site", "docs-sync"): "gggggg",
}
_V = {"g": "green", "a": "amber", "r": "red"}
_COST = {"green": 0.9, "amber": 1.4, "red": 2.1}


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(outdir):
    os.makedirs(outdir, exist_ok=True)
    conn = db.open_index(os.path.join(outdir, "index.db"))
    conn.execute("DELETE FROM run"); conn.execute("DELETE FROM git_state")
    conn.execute("DELETE FROM github_state"); conn.execute("DELETE FROM telemetry_day")
    taken = _iso(NOW - dt.timedelta(minutes=20))
    for slug, g, h in PROJECTS:
        conn.execute(
            "INSERT INTO git_state(project,host,taken,branch,ahead,behind,dirty_files,worktrees,"
            "orphan_worktrees,stale_branches,unmerged_agent_branches,largest_blob_mb,force_pushes_7d)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, "demo", taken, g.get("branch", "main"), g.get("ahead", 0), g.get("behind", 0),
             g.get("dirty_files", 0), 1, g.get("orphan_worktrees", 0), g.get("stale_branches", 0),
             g.get("unmerged_agent_branches", 0), g.get("largest_blob_mb", 0),
             g.get("force_pushes_7d", 0)))
        conn.execute(
            "INSERT INTO github_state(project,taken,open_prs,agent_prs,oldest_pr_days,failing_checks,"
            "security_alerts,dependabot_open,actions_minutes_month,issues_open) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (slug, taken, h.get("open_prs", 0), h.get("agent_prs", 0), h.get("oldest_pr_days", 0),
             h.get("failing_checks", 0), h.get("security_alerts", 0), h.get("dependabot_open", 0),
             h.get("actions_minutes_month", 120), h.get("issues_open", 0)))
    # run history for verdict trends + spend
    n = 0
    for (slug, loop), trend in TRENDS.items():
        for i, ch in enumerate(trend):
            verdict = _V[ch]
            ended = NOW - dt.timedelta(days=(len(trend) - i) * 5)
            n += 1
            conn.execute(
                "INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict,"
                "cost_usd,tokens_in,tokens_out) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"DEMO{n:04d}{'0'*22}"[:26], loop, slug, "demo", "cloud",
                 _iso(ended - dt.timedelta(minutes=8)), _iso(ended), "ok", verdict,
                 _COST[verdict], 40000, 6000))
    # telemetry spend (last 14 days, per project)
    for d in range(14):
        day = (NOW - dt.timedelta(days=d)).strftime("%Y-%m-%d")
        for slug, _g, _h in PROJECTS:
            conn.execute(
                "INSERT INTO telemetry_day(day,project,host,model,sessions,cost_usd,tokens,"
                "lines_added,lines_removed,commits,prs,active_seconds) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, slug, "demo", "claude-opus", 1 + (d + len(slug)) % 3,
                 round(1.2 + ((d * 7 + len(slug)) % 9) * 0.4, 2), 90000 + d * 1000,
                 120, 40, 2, 1, 3600))
    conn.commit()
    conn.close()
    _write_registry(os.path.join(outdir, "registry.yaml"))
    # copy the code the board needs to render the Loops panel, so the demo dir is self-contained.
    import shutil
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("cadences", "prompts", "schema"):
        dst = os.path.join(outdir, sub)
        if os.path.isdir(os.path.join(repo, sub)):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(os.path.join(repo, sub), dst)
    print(f"demo instance -> {outdir}  ({len(PROJECTS)} projects, {n} runs)")


def _write_registry(path):
    lines = ["# Demo registry — synthetic projects for the public board. No real data.\n",
             "host_default: demo\n", "projects:\n"]
    for slug, _g, _h in PROJECTS:
        lines += [f"  - slug: {slug}\n",
                  f"    repo: acme-demo/{slug}\n",
                  "    default_branch: main\n",
                  "    worktree:\n",
                  f"      demo: /demo/{slug}\n",
                  "    tier_default: cloud\n",
                  f"    cadences: [{', '.join(LOOPS)}]\n",
                  "    escalation:\n      github_issues: false\n"]
    with open(path, "w") as fh:
        fh.writelines(lines)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "demo")
    build(out)

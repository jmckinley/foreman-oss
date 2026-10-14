#!/usr/bin/env python3
"""Generate a self-contained synthetic demo instance for the PUBLIC repo.

The board renders a believable fleet with zero real data — no real project names, paths,
verdicts, or spend — and exercises *every* surface, including the differentiators:

  • receipts (state/receipts/) drive verdicts, Needs-you, and the Trends & insights panel;
  • config-drift rows (config_snapshot/component/config_key) make the DRIFT banner + section fire;
  • a queued red→fix→approve flow (state/queue/) shows auto-remediation with its approval gate.

    python3 scripts/make_demo.py [outdir]      # default: examples/demo/

Point the board at it (state + index now default to the instance dir, so this is enough):

    FOREMAN_DIR=examples/demo python3 -m collectors.web

NOW is the generation time so a freshly built demo is current (insights need runs within the
30-day window). Re-generate before screenshots; the committed snapshot ages like any fixture.
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors import db, decisions as D, ingest  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
LOOPS = ["docs-sync", "quality-review", "security-review", "perf-review"]
STEP_DAYS = 4                                     # gap between runs; 6 runs span 24d (< 30d window)

# (slug, git_state, github_state) — a spread so every repo-status state shows.
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

# per (project, loop) verdict trend, oldest→newest. Crafted so the insights taxonomy all shows:
# regression, chronic, degrading→projected, flapping — plus plenty of steady green.
TRENDS = {
    ("acme-api", "docs-sync"): "gggggg", ("acme-api", "quality-review"): "ggggag",
    ("acme-api", "security-review"): "gggggg", ("acme-api", "perf-review"): "gggggg",
    ("widgetco-web", "docs-sync"): "ggaggg", ("widgetco-web", "quality-review"): "gagaaa",
    ("widgetco-web", "security-review"): "gggggg", ("widgetco-web", "perf-review"): "gggaaa",
    ("payments-svc", "docs-sync"): "gggagg", ("payments-svc", "quality-review"): "garrrr",
    ("payments-svc", "security-review"): "gggggr", ("payments-svc", "perf-review"): "ggaarr",
    ("mobile-app", "docs-sync"): "gggggg", ("mobile-app", "quality-review"): "ggggaa",
    ("mobile-app", "perf-review"): "gggarr",
    ("data-pipeline", "docs-sync"): "ggaggg", ("data-pipeline", "quality-review"): "grgrgr",
    ("data-pipeline", "security-review"): "gggggg",
    ("docs-site", "docs-sync"): "gggggg",
}
_V = {"g": "green", "a": "amber", "r": "red"}
_COST = {"green": 0.9, "amber": 1.4, "red": 2.1}
_FINDINGS = {"green": 0, "amber": 3, "red": 7}
_NEXT = {
    "docs-sync": "Regenerate the drifted sections of README + docs/ and re-run.",
    "quality-review": "Address the top lint/complexity hotspots flagged in the last run.",
    "security-review": "Triage the 2 high-severity findings; patch or justify each.",
    "perf-review": "Profile the hot path the run flagged and cut the p95 regression.",
}


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_receipts(state_dir):
    """Write a receipt per run from TRENDS, so verdicts + insights are receipt-driven."""
    n = 0
    for (slug, loop), trend in sorted(TRENDS.items()):
        for i, ch in enumerate(trend):
            verdict = _V[ch]
            ended = NOW - dt.timedelta(days=(len(trend) - 1 - i) * STEP_DAYS)
            n += 1
            rid = f"DEMO{n:04d}{'0'*26}"[:26]
            r = {"schema_version": 1, "run_id": rid, "cadence": loop, "project": slug,
                 "host": "demo", "tier": "cloud",
                 "started": _iso(ended - dt.timedelta(minutes=8)), "ended": _iso(ended),
                 "status": "ok", "verdict": verdict, "cc_version": "2.0", "model": "claude-opus",
                 "cost_usd": _COST[verdict], "tokens": {"in": 40000, "out": 6000},
                 "metrics": {"findings": _FINDINGS[verdict]},
                 "next_action": _NEXT[loop] if verdict != "green" else "none"}
            if verdict == "red":
                r["escalations"] = [{"id": f"{slug}-{loop}", "severity": "high",
                                     "summary": f"{loop} failing — {_FINDINGS[verdict]} findings unresolved."}]
            d = os.path.join(state_dir, "receipts", slug, loop)
            os.makedirs(d, exist_ok=True)
            fname = f"{_iso(ended).replace(':', '').replace('-', '')}-{rid}.json"
            with open(os.path.join(d, fname), "w") as fh:
                json.dump(r, fh)
    return n


def _seed_drift(conn):
    """Config-drift rows so the DRIFT banner + section fire — the differentiator, on show.

    payments-svc: a pin mismatch, a skill the probe says silently no-ops (the moat line), and a
    permissions.deny shadowing an allow. widgetco-web: a key the live probe overrode. Plus a
    fleet-wide always-on budget breach.
    """
    taken = _iso(NOW - dt.timedelta(minutes=20))

    def snapshot(slug):
        cur = conn.execute(
            "INSERT INTO config_snapshot(project,host,taken,cc_version,hash,probe_ok,effective_json)"
            " VALUES(?,?,?,?,?,?,?)", (slug, "demo", taken, "2.0", f"hash-{slug}", 1, "{}"))
        return cur.lastrowid

    def comp(sid, kind, name, **kw):
        conn.execute(
            "INSERT INTO component(snapshot_id,kind,name,version,scope,source,invocable,"
            "always_on_tokens,disagrees_with_probe) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, kind, name, kw.get("version"), kw.get("scope", "project"),
             kw.get("source", "plugin"), kw.get("invocable", 1),
             kw.get("always_on_tokens"), kw.get("disagrees_with_probe", 0)))

    def key(sid, k, effective, winning="project", disagree=0):
        conn.execute(
            "INSERT INTO config_key(snapshot_id,key,effective,winning_layer,shadowed,"
            "disagrees_with_probe) VALUES(?,?,?,?,?,?)",
            (sid, k, json.dumps(effective), winning, "[]", disagree))

    pay = snapshot("payments-svc")
    comp(pay, "plugin", "foreman-ops", version="1.2.0", always_on_tokens=3000)   # pin drift + budget
    comp(pay, "skill", "security-review", invocable=1, disagrees_with_probe=1)   # silent no-op
    key(pay, "permissions.allow", ["Bash(git*)", "Read"])
    key(pay, "permissions.deny", ["Bash(git*)"])                                 # shadows the allow

    wid = snapshot("widgetco-web")
    comp(wid, "plugin", "foreman-ops", version="1.4.0")
    key(wid, "model", "claude-opus", winning="probe", disagree=1)                # probe overrode


def _seed_queue(state_dir):
    """A red → auto-fix → approval flow, pre-staged in the decision queue for payments-svc:
    the dispatched fix loop, and its PR waiting on the approve_push human gate."""
    from pathlib import Path
    state_dir = Path(state_dir)
    now_ms = int(NOW.timestamp() * 1000)
    D.enqueue(state_dir, "payments-svc", "dispatch_cadence",
              {"cadence": "security-review-fix", "remediation": True,
               "parent": "security-review", "attempt": 1,
               "source_run": "DEMO-security-review"}, "foreman-remediate", now_ms=now_ms)
    D.enqueue(state_dir, "payments-svc", "approve_push",
              {"branch": "foreman/security-review-fix", "base": "main",
               "cadence": "security-review", "ahead": 3,
               "title": "Foreman: security-review fix (patch 2 high-severity findings)"},
              "foreman-remediate", now_ms=now_ms + 1)


def build(outdir):
    import shutil
    os.makedirs(outdir, exist_ok=True)
    state_dir = os.path.join(outdir, "state")
    # Start clean so re-runs are reproducible.
    shutil.rmtree(state_dir, ignore_errors=True)
    os.makedirs(os.path.join(state_dir, "receipts"), exist_ok=True)
    os.makedirs(os.path.join(state_dir, "queue"), exist_ok=True)

    idx = os.path.join(outdir, "index.db")
    if os.path.exists(idx):
        os.remove(idx)
    conn = db.open_index(idx)
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

    runs = _write_receipts(state_dir)
    ingest.ingest_receipts(conn, state_dir)            # receipts -> run table (sparklines, spend)
    _seed_drift(conn)

    for d in range(14):                                # telemetry spend, last 14 days per project
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
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("cadences", "prompts", "schema"):
        dst = os.path.join(outdir, sub)
        if os.path.isdir(os.path.join(repo, sub)):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(os.path.join(repo, sub), dst)
    _enable_remediation(os.path.join(outdir, "cadences", "security-review.yaml"))
    _seed_queue(state_dir)
    print(f"demo instance -> {outdir}  ({len(PROJECTS)} projects, {runs} runs, "
          "drift + remediation flow seeded)")


def _enable_remediation(cadence_path):
    """Make the demo internally coherent: the loop payments-svc auto-remediates declares it."""
    if not os.path.exists(cadence_path):
        return
    import yaml
    cad = yaml.safe_load(open(cadence_path).read())
    cad["remediation"] = {"enabled": True, "max_attempts": 2}
    with open(cadence_path, "w") as fh:
        yaml.safe_dump(cad, fh, sort_keys=False)


def _write_registry(path):
    lines = ["# Demo registry — synthetic projects for the public board. No real data.\n",
             "host_default: demo\n",
             "defaults:\n",
             '  marketplace_pin: "1.4.0"        # payments-svc runs 1.2.0 -> pin drift\n',
             "  context_budget_tokens: 2400      # always-on plugin cost ceiling\n",
             "projects:\n"]
    for slug, _g, _h in PROJECTS:
        lines += [f"  - slug: {slug}\n",
                  f"    repo: acme-demo/{slug}\n",
                  "    default_branch: main\n",
                  "    worktree:\n",
                  f"      demo: /demo/{slug}\n",
                  "    tier_default: cloud\n",
                  f"    cadences: [{', '.join(LOOPS)}]\n"]
        if slug == "payments-svc":                      # the one project opted into auto-remediation
            lines.append("    auto_remediate: [security-review]\n")
        lines.append("    escalation:\n      github_issues: false\n")
    with open(path, "w") as fh:
        fh.writelines(lines)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "demo")
    build(out)

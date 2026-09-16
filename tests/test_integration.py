"""Full end-to-end: real git repos, sessions, receipts, telemetry, and the whole pipeline.

Emit receipts through the hooks, run every collector via collect.run_all against real
worktrees + synthetic transcripts + a telemetry spool + a fake GitHub, then render the brief
and exercise the operator surface (dispatch -> drain) and retention. This is the "does the
app cohere" test.
"""

import datetime as dt
import json
import os
import subprocess

import yaml

from collectors import db, collect, brief, decisions as D, dispatch, quota, retention


def _emit_receipt(repo_root, spool, state, *, run_id, project, cadence, tier, host,
                  ended, partial):
    (spool / f"{run_id}.partial.json").write_text(json.dumps(partial))
    env = {**os.environ, "FOREMAN_RUN_ID": run_id, "FOREMAN_CADENCE": cadence,
           "FOREMAN_PROJECT": project, "FOREMAN_HOST": host, "FOREMAN_TIER": tier,
           "FOREMAN_STARTED": ended, "FOREMAN_ENDED": ended, "FOREMAN_CC_VERSION": "2.0",
           "FOREMAN_SPOOL": str(spool), "FOREMAN_STATE_DIR": str(state),
           "FOREMAN_DIR": str(repo_root), "PYTHONPATH": str(repo_root)}
    out = subprocess.run(["bash", str(repo_root / "hooks" / "session_end.sh")],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_full_pipeline(foreman_dir, state_dir, spool_dir, index_path, git_repo,
                       make_transcript, fake_github, otlp_metric, now, tmp_path):
    from tests.conftest import REPO

    # --- 1. real worktrees for all three projects, wired into the registry (host mbp) ---
    repos = {slug: git_repo(settings={"model": "sonnet"}, agent_branch=(slug == "sentrygw"))
             for slug in ("acmeapi", "sentrygw", "paysvc")}
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    # Isolate the pipeline to this test's three projects (the live registry carries more).
    reg["projects"] = [p for p in reg["projects"] if p["slug"] in repos]
    for p in reg["projects"]:
        p["worktree"] = {"mbp": str(repos[p["slug"]])}
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))

    # --- 2. receipts via the real hook: acmeapi green, sentrygw red ---
    _emit_receipt(REPO, spool_dir, state_dir, run_id="01HZY0R4M8K3V9WTE2N6QGDCB1",
                  project="acmeapi", cadence="docs-sync", tier="cloud", host="cloud",
                  ended="2026-09-06T08:00:00Z",
                  partial={"metrics": {"undocumented_public_symbols": 0,
                                       "guide_sections_stale_days_max": 3,
                                       "changelog_commit_delta": 2},
                           "next_action": "Document new symbols"})
    _emit_receipt(REPO, spool_dir, state_dir, run_id="01HZY0R4M8K3V9WTE2N6QGDCB2",
                  project="sentrygw", cadence="quality-review", tier="cloud", host="cloud",
                  ended="2026-09-05T05:23:00Z",
                  partial={"metrics": {"review_debt_prs": 9, "coverage_pct": 40,
                                       "complexity_hotspots": 5},
                           "next_action": "Review and merge the stuck PRs"})

    # --- 3. a CLI transcript for acmeapi with a skill fire ---
    tv = str(repos["acmeapi"])
    root, _ = make_transcript(tv, "sess-tv", [
        {"type": "user", "cwd": tv, "message": {"content": "hi"}},
        {"type": "assistant", "timestamp": "2026-09-06T09:00:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"command": "/brief"}}]}},
    ])

    # --- 4. a telemetry payload with an edit accept/reject rate for acmeapi ---
    payload = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{"metrics": [
        otlp_metric("claude_code.code_edit_tool.decision", 6,
                    {"foreman.project": "acmeapi", "foreman.host": "mbp", "decision": "accept"}),
        otlp_metric("claude_code.code_edit_tool.decision", 1,
                    {"foreman.project": "acmeapi", "foreman.host": "mbp", "decision": "reject"}),
    ]}]}]}
    from collectors import telemetry
    telemetry.append_spool(spool_dir, payload)

    # --- 5. run every collector in one pass ---
    result = collect.run_all(
        index_path=index_path, state_dir=str(state_dir), foreman_dir=str(foreman_dir),
        host="mbp", now=now, gh=lambda args: None, github=fake_github,
        transcript_roots=[(str(root), "cli")], spool_dir=str(spool_dir),
        probe_fn=lambda project_dir: None,   # no live claude -p in tests
        quota_fetch=lambda: {"pct_used": 40, "plan": "max"}, routine_history=[
            {"project": "paysvc", "cadence": "docs-sync",
             "started": "2026-09-06T03:17:00Z", "status": "failed"}])

    assert result["runs"] == 2
    assert result["git_state"] == 3           # all three worktrees present
    assert result["config_snapshots"] == 3    # C6 ran per worktree
    assert result["sessions"] >= 1            # C1 found the transcript
    assert result["escalations_open"] == 1    # sentrygw quality-review red
    assert result["quota"] is True
    assert result["runs_recorded"] == 1       # C7 recorded the receipt-less paysvc run

    conn = db.connect(index_path)

    # runs + metrics ingested
    assert conn.execute("SELECT COUNT(*) c FROM run").fetchone()["c"] == 3  # 2 receipts + 1 C7
    # C1 skill fire
    assert conn.execute("SELECT skill FROM skill_fire").fetchone()["skill"] == "brief"
    # C2 edit rate per project, non-zero
    td = conn.execute("SELECT edit_accept,edit_reject FROM telemetry_day WHERE project='acmeapi'").fetchone()
    assert td["edit_accept"] == 6 and td["edit_reject"] == 1
    # C4 git_state has the sentrygw agent branch
    gb = conn.execute("SELECT unmerged_agent_branches FROM git_state WHERE project='sentrygw'").fetchone()
    assert gb["unmerged_agent_branches"] == 1
    # escalation opened + linked to a GitHub issue
    esc = conn.execute("SELECT github_issue FROM escalation WHERE project='sentrygw'").fetchone()
    assert esc["github_issue"] is not None and fake_github.issues[1]["state"] == "open"

    # --- 6. the brief renders the whole board off the index ---
    board = brief.render(state_dir, foreman_dir, now=now, index_path=index_path)
    assert "quota 60%" in board.splitlines()[0]
    assert "sentrygw / quality-review" in board.split("NEEDS YOU")[1]
    assert "CADENCES" in board and "DRIFT" in board and "QUEUED" in board and "QUIET" in board

    # --- 7. operator surface: dispatch a cadence, drain it in the worktree ---
    res = dispatch.dispatch(conn, reg, state_dir, "acmeapi", "docs-sync")
    assert res["deferred"] is False
    drained = D.drain_session(state_dir, foreman_dir, repos["acmeapi"], host="mbp", session_uuid="s")
    assert drained["project"] == "acmeapi" and "docs-sync" in drained["context"]

    # --- 8. retention runs clean over the populated index ---
    r = retention.run_all(conn, now=now, state_dir=str(state_dir), spool_dir=str(spool_dir))
    assert "config_snapshots_pruned" in r and "decisions" in r

    # a green run for sentrygw closes its escalation with a receipt link
    from collectors import escalations
    (state_dir / "receipts" / "sentrygw" / "quality-review").mkdir(parents=True, exist_ok=True)
    green = {"schema_version": 1, "run_id": "01HZY0R4M8K3V9WTE2N6QGDCB9",
             "cadence": "quality-review", "project": "sentrygw", "host": "cloud", "tier": "cloud",
             "started": "2026-09-06T05:23:00Z", "ended": "2026-09-06T05:23:00Z", "status": "ok",
             "verdict": "green", "cc_version": "2.0", "metrics": {}, "next_action": "clear"}
    (state_dir / "receipts" / "sentrygw" / "quality-review" /
     "20260906T052300Z-01HZY0R4M8K3V9WTE2N6QGDCB9.json").write_text(json.dumps(green))
    escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)
    assert fake_github.issues[1]["state"] == "closed"

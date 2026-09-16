"""Index collectors: ingest + lock (M3), git/GitHub (C4/C5), config (C6),
transcripts (C1), telemetry (C2), runstate (C7)."""

import datetime as dt
import json

import pytest

from collectors import (db, ingest, lock, escalations, git_state, github_state,
                        config_resolve as C, transcripts, telemetry, runstate, quota)

ISO = "%Y-%m-%dT%H:%M:%SZ"


# ------------------------------------------------------------------ ingest + lock

def _hist(make_receipt):
    make_receipt("acmeapi", "docs-sync", "2026-09-01T10:00:00Z", "green",
                 metrics={"undocumented_public_symbols": 0})
    make_receipt("sentrygw", "docs-sync", "2026-09-05T10:00:00Z", "red",
                 escalations=[{"severity": "red", "summary": "9", "evidence": "e"}])
    make_receipt("sentrygw", "docs-sync", "2026-09-06T10:00:00Z", "green",
                 status="locked", lock="held", notes="dup")


def test_ingest_runs_and_metrics(conn, make_receipt, state_dir):
    _hist(make_receipt)
    out = ingest.ingest_receipts(conn, state_dir)
    assert out["runs"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM run").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM metric").fetchone()["c"] == 1


def test_drop_reingest_reproduces_rows(conn, make_receipt, state_dir, tmp_path):
    _hist(make_receipt)
    ingest.ingest_receipts(conn, state_dir)
    snap = [tuple(r) for r in conn.execute("SELECT run_id,status,verdict FROM run ORDER BY run_id")]
    conn2 = db.open_index(":memory:")
    ingest.ingest_receipts(conn2, state_dir)
    snap2 = [tuple(r) for r in conn2.execute("SELECT run_id,status,verdict FROM run ORDER BY run_id")]
    assert snap == snap2


def test_lock_acquire_and_steal(conn):
    T = lambda m: dt.datetime(2026, 9, 6, 10, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=m)
    key = lock.lock_key("docs-sync:{project}", "sentrygw")
    assert lock.acquire(conn, key, "mbp", "A", timeout_minutes=40, now=T(0)) is True
    assert lock.acquire(conn, key, "vps", "B", timeout_minutes=40, now=T(1)) is False
    assert lock.holder(conn, key)["run_id"] == "A"
    assert lock.acquire(conn, key, "vps", "B", timeout_minutes=40, now=T(44)) is False
    assert lock.acquire(conn, key, "vps", "B", timeout_minutes=40, now=T(46)) is True


def test_locked_receipt_and_brief_filter(state_dir):
    from collectors import brief
    p = lock.emit_locked_receipt(state_dir, run_id="01HZY0R4M8K3V9WTE2N6QGDCBF",
        cadence="docs-sync", project="sentrygw", host="cloud", tier="cloud",
        started="2026-09-06T10:00:00Z", ended="2026-09-06T10:00:00Z", cc_version="2.0",
        holder_host="mbp")
    r = json.loads(p.read_text())
    assert r["status"] == "locked" and r["lock"] == "held"
    assert brief.load_receipts(state_dir) == {}  # coordination artifact filtered


# --------------------------------------------------------------------- C4 / C5

def test_git_state(git_repo):
    repo = git_repo(agent_branch=True, stale_branch=True, dirty=True)
    now = dt.datetime.now(dt.timezone.utc)
    row = git_state.collect(repo, "acmeapi", "mbp", default_branch="main",
                            taken="2026-09-06T12:00:00Z", now=now)
    assert row["branch"] == "main"
    assert row["dirty_files"] >= 1
    assert row["unmerged_agent_branches"] == 1
    assert row["stale_branches"] == 1
    assert git_state.collect(repo, "acmeapi", "mbp", default_branch="main",
                             taken="2026-09-06T12:00:00Z", now=now) == row  # deterministic


def test_github_state_compute():
    now = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
    raw = {"prs": [
        {"author_login": "dependabot[bot]", "is_bot": True,
         "created_at": "2026-09-04T12:00:00Z", "checks": []},
        {"author_login": "alice", "is_bot": False, "created_at": "2026-08-28T12:00:00Z",
         "checks": [{"name": "unit-tests", "state": "FAILURE"},
                    {"name": "eslint", "state": "FAILURE"}]}],
        "issues_open": 4, "security_alerts": 1, "dependabot_open": 2, "actions_minutes_month": 137}
    gh = github_state.compute(raw, project="acmeapi", taken="t", now=now)
    assert gh["open_prs"] == 2 and gh["agent_prs"] == 1 and gh["oldest_pr_days"] == 9
    assert gh["failing_checks"] == 2
    assert json.loads(gh["failure_classes"]) == {"lint": 1, "test": 1}


# --------------------------------------------------------------------- C6 config

def test_config_merge_and_env():
    layers = {"local": {"permissions": {"deny": ["Bash(rg:*)"]}, "model": "haiku"},
              "project": {"model": "sonnet"},
              "user": {"permissions": {"deny": ["Curl"]}, "model": "opus"}}
    res = C.merge_layers(layers)
    assert res["model"]["winning_layer"] == "local"  # settings.local outranks project+user
    assert res["permissions.deny"]["winning_layer"] == "merged"
    assert res["permissions.deny"]["effective"] == ["Bash(rg:*)", "Curl"]
    C.apply_env_overlay(res, {"ANTHROPIC_MODEL": "opus-hard"})
    assert res["model"]["winning_layer"] == "env:hard"


def test_config_mcp_and_skills():
    mcp = C.resolve_mcp(user_servers={"a": {}, "b": {}}, project_disabled=["b"],
                        mcpjson_servers={"c": {}, "d": {}}, enable_all=False,
                        enabled_json=["c"], disabled_json=[])
    assert mcp["a"]["enabled"] and not mcp["b"]["enabled"]
    assert mcp["c"]["enabled"] and not mcp["d"]["enabled"]
    skills = C.resolve_skills(
        [{"name": "ok", "frontmatter": {}}, {"name": "off", "frontmatter": {"disable-model-invocation": True}},
         {"name": "denied", "frontmatter": {}}], deny_rules=["Skill(denied)"], skill_overrides={})
    inv = {s["name"]: s["invocable"] for s in skills}
    assert inv == {"ok": True, "off": False, "denied": False}


def test_config_probe_and_explain(conn):
    res = C.merge_layers({"user": {"model": "sonnet"}, "local": {"permissions": {"deny": ["X"]}}})
    C.reconcile_probe(res, {"model": "opus"})
    assert res["model"]["winning_layer"] == "probe" and res["model"]["disagrees_with_probe"]
    C.write_snapshot(conn, project="paysvc", host="mbp", taken="2026-09-06T00:00:00Z",
                     cc_version="2.0", resolved=res, components=[], probe_ok=True)
    ex = C.explain(conn, "permissions.deny", "paysvc")
    assert ex["winning_layer"] == "merged" and ex["effective"] == ["X"]


def test_reconcile_components_flags_silent_no_op():
    # predicted invocable, but the probe reports the skill will not fire unprompted
    components = [
        {"kind": "skill", "name": "docs-sync", "invocable": True},
        {"kind": "skill", "name": "quiet", "invocable": True},
        {"kind": "mcp", "name": "gh", "invocable": True},
        {"kind": "plugin", "name": "foreman-ops"},
    ]
    probe = {"skills": [{"name": "docs-sync", "invocable": False},
                        {"name": "quiet", "invocable": True}],
             "mcp": [{"name": "other"}],
             "plugins": [{"name": "foreman-ops"}]}
    drift = C.reconcile_components(components, probe)
    assert any("docs-sync" in d and "silent no-op" in d for d in drift)
    assert any("gh" in d and "connected" in d for d in drift)   # predicted on, probe omits it
    assert not any("quiet" in d for d in drift)                 # probe confirms it fires
    assert not any("foreman-ops" in d for d in drift)           # probe confirms the plugin
    assert components[0]["disagrees_with_probe"] and "disagrees_with_probe" not in components[1]


def test_reconcile_components_conservative_when_probe_silent():
    # a probe that does not enumerate a kind must not manufacture drift for it
    components = [{"kind": "skill", "name": "s", "invocable": True},
                 {"kind": "mcp", "name": "m", "invocable": True}]
    assert C.reconcile_components(components, {}) == []
    assert C.reconcile_components(components, None) == []
    assert C.reconcile_components(components, {"model": "opus"}) == []   # no skills/mcp lists


def test_component_drift_surfaces_in_drift_lines(conn):
    res = C.merge_layers({"user": {"model": "opus"}})
    components = [{"kind": "skill", "name": "docs-sync", "scope": "user",
                  "source": "s", "version": None, "invocable": True, "always_on_tokens": None}]
    C.reconcile_components(components, {"skills": [{"name": "docs-sync", "invocable": False}]})
    C.write_snapshot(conn, project="paysvc", host="mbp", taken="2026-09-06T00:00:00Z",
                     cc_version="2.0", resolved=res, components=components, probe_ok=True)
    registry = {"projects": [{"slug": "paysvc"}]}
    lines = C.drift_lines(conn, registry)
    assert any(scope == "paysvc" and "docs-sync" in msg and "silent no-op" in msg
               for scope, msg in lines)


def test_fleet_diff_cross_host(conn):
    def snap(host, model, components):
        res = C.merge_layers({"user": {"model": model}})
        C.write_snapshot(conn, project="paysvc", host=host, taken=f"2026-09-06T00:00:00Z",
                         cc_version="2.0", resolved=res, components=components, probe_ok=True)
    common = {"kind": "skill", "name": "docs-sync", "scope": "user", "source": "s",
              "version": None, "always_on_tokens": None}
    snap("mbp", "opus", [{**common, "invocable": True},
                         {"kind": "mcp", "name": "gh", "scope": None, "source": "s",
                          "version": None, "invocable": True, "always_on_tokens": None}])
    snap("vps", "sonnet", [{**common, "invocable": False}])   # different model; skill inert; no gh

    diff = C.fleet_diff(conn, "paysvc")
    assert diff["hosts"] == ["mbp", "vps"]
    keys = {k["key"] for k in diff["keys"]}
    assert "model" in keys                                    # opus vs sonnet
    comps = {(c["kind"], c["name"]): c["by_host"] for c in diff["components"]}
    assert comps[("skill", "docs-sync")] == {"mbp": "invocable", "vps": "inert"}
    assert comps[("mcp", "gh")] == {"mbp": "invocable", "vps": "absent"}   # activated on mbp only

    out = C.render_fleet_diff(diff)
    assert "mbp vs vps" in out and "gh" in out


def test_connect_self_heals_added_columns(tmp_path):
    # an index built by an older schema (no disagrees_with_probe) must not crash a read path
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(str(p))
    c.executescript("CREATE TABLE component (snapshot_id INTEGER, kind TEXT, name TEXT, "
                    "scope TEXT, invocable INTEGER, PRIMARY KEY(snapshot_id,kind,name,scope));")
    c.commit(); c.close()
    conn = db.connect(p)                                  # read-path connect, no migrate
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(component)")}
    assert "disagrees_with_probe" in cols                 # self-healed on connect


def test_fleet_diff_single_host_is_empty(conn):
    C.write_snapshot(conn, project="solo", host="mbp", taken="2026-09-06T00:00:00Z",
                     cc_version="2.0", resolved=C.merge_layers({"user": {"model": "opus"}}),
                     components=[], probe_ok=True)
    diff = C.fleet_diff(conn, "solo")
    assert diff["keys"] == [] and diff["components"] == []
    assert "nothing to diff" in C.render_fleet_diff(diff)


# --------------------------------------------------------------------- C1 transcripts

def _rec_skill(cmd):
    return {"type": "assistant", "timestamp": "2026-09-06T10:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"command": cmd}}]}}


def test_transcript_tail_and_incremental(conn, make_transcript):
    root, path = make_transcript("/Users/john/code/acmeapi", "s1",
                                 [{"type": "user", "message": {"content": "hi"}}, _rec_skill("/brief")])
    res = transcripts.tail_session(conn, session_uuid="s1", path=path, project="acmeapi",
                                   host="mbp", surface="cli", cwd="/x", cc_version="2.0")
    assert res["skill_fires"] == 1
    assert conn.execute("SELECT skill FROM skill_fire").fetchone()["skill"] == "brief"
    with path.open("a") as fh:
        fh.write(json.dumps(_rec_skill("/explain-config")) + "\n")
    res2 = transcripts.tail_session(conn, session_uuid="s1", path=path, project="acmeapi",
                                    host="mbp", surface="cli", cwd="/x", cc_version="2.0")
    assert res2["skill_fires"] == 1  # only the appended record
    assert conn.execute("SELECT COUNT(*) c FROM skill_fire").fetchone()["c"] == 2


def test_transcript_oversized_skipped(conn, tmp_path):
    big = tmp_path / "huge.jsonl"
    with big.open("wb") as fh:
        fh.seek(200 * 1024 * 1024)
        fh.write(b"\n")
    res = transcripts.tail_session(conn, session_uuid="huge", path=big, project=None,
                                   host="mbp", surface="cli", cwd=None, cc_version="2.0")
    assert res["oversized"] is True and res["read_bytes"] == 0
    row = conn.execute("SELECT oversized,last_offset FROM session WHERE session_uuid='huge'").fetchone()
    assert row["oversized"] == 1 and row["last_offset"] == 0


def test_transcript_unknown_envelope_halts(conn, tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps(_rec_skill("/brief")) + "\n"
                 + json.dumps({"type": "quantum_flux", "payload": 1}) + "\n")
    with pytest.raises(transcripts.EnvelopeShapeError):
        transcripts.tail_session(conn, session_uuid="bad", path=p, project=None,
                                 host="mbp", surface="cli", cwd=None, cc_version="2.0")
    assert conn.execute("SELECT COUNT(*) c FROM skill_fire").fetchone()["c"] == 0


# --------------------------------------------------------------------- C2 telemetry

def test_telemetry_rollup_edit_rate(conn, spool_dir, otlp_metric):
    payload = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{"metrics": [
        otlp_metric("claude_code.code_edit_tool.decision", 7,
                    {"foreman.project": "acmeapi", "foreman.host": "mbp", "decision": "accept", "model": "opus"}),
        otlp_metric("claude_code.code_edit_tool.decision", 2,
                    {"foreman.project": "acmeapi", "foreman.host": "mbp", "decision": "reject", "model": "opus"}),
    ]}]}]}
    telemetry.append_spool(spool_dir, payload)
    telemetry.drain(conn, spool_dir)
    row = conn.execute("SELECT edit_accept,edit_reject FROM telemetry_day WHERE project='acmeapi'").fetchone()
    assert row["edit_accept"] == 7 and row["edit_reject"] == 2
    telemetry.drain(conn, spool_dir)  # idempotent
    assert conn.execute("SELECT edit_accept FROM telemetry_day").fetchone()["edit_accept"] == 7


def test_telemetry_receiver_units():
    plist = telemetry.receiver_launchd_plist(spool="/tmp/foreman/spool", port=4318)
    assert telemetry.LABEL in plist and "collectors.telemetry" in plist and "serve" in plist
    assert "<key>KeepAlive</key><true/>" in plist         # persists across restarts
    unit = telemetry.receiver_systemd_unit(spool="/tmp/foreman/spool", port=4318)
    assert "Restart=always" in unit and "serve --spool /tmp/foreman/spool --port 4318" in unit


# --------------------------------------------------------------------- C7 runstate

def test_runstate_quota_and_runs(conn):
    now = dt.datetime(2026, 9, 6, 12, tzinfo=dt.timezone.utc)
    out = runstate.collect(conn, quota_fetch=lambda: {"pct_used": 88, "plan": "max"},
                           routine_history=[{"project": "sentrygw", "cadence": "docs-sync",
                                             "started": "2026-09-06T03:17:00Z", "status": "failed"}],
                           now=now)
    assert out["runs_recorded"] == 1 and quota.low(conn) is True
    runstate.reconcile_runs(conn, [{"project": "sentrygw", "cadence": "docs-sync",
                                    "started": "2026-09-06T03:17:00Z", "status": "failed"}])
    assert conn.execute("SELECT COUNT(*) c FROM run").fetchone()["c"] == 1  # idempotent

"""Supervisor surface: brief, decisions, escalations, dispatch, promote, retention, secrets."""

import datetime as dt
import json
import shutil

import pytest
import yaml

from collectors import (brief, decisions as D, escalations, dispatch, promote, quota,
                        retention, secrets, db, discover)

ISO = "%Y-%m-%dT%H:%M:%SZ"


def ago(now, days=0, hours=0):
    return (now - dt.timedelta(days=days, hours=hours)).strftime(ISO)


# --------------------------------------------------------------------------- brief

def test_brief_grid_staleness_amber(make_receipt, foreman_dir, state_dir, now):
    make_receipt("acmeapi", "docs-sync", ago(now, days=10), "green")
    make_receipt("acmeapi", "docs-sync", ago(now, hours=3), "green")
    make_receipt("sentrygw", "docs-sync", ago(now, days=2), "red",
                 artifacts=[{"kind": "pr", "ref": "#204"}], next_action="Triage tests")
    for d_ in (21, 14, 7):
        make_receipt("paysvc", "docs-sync", ago(now, days=d_), "amber")
    make_receipt("paysvc", "docs-sync", ago(now, hours=1), "amber", next_action="Rewrite deploy docs")

    out = brief.render(state_dir, foreman_dir, now=now)
    assert "acmeapi  green 3h" in out.replace("  ", " ").replace("acmeapi ", "acmeapi  ") or "green 3h" in out
    assert "red 2d" in out and "PR #204" in out
    assert "ages to red today" in out
    ny = out.split("NEEDS YOU")[1]
    assert ny.index("sentrygw") < ny.index("paysvc")  # red before amber

    # deleting the latest acmeapi receipt makes it stale, not gone
    tv = sorted((state_dir / "receipts" / "acmeapi" / "docs-sync").glob("*.json"))[-1]
    tv.unlink()
    out2 = brief.render(state_dir, foreman_dir, now=now)
    row = [l for l in out2.split("CADENCES")[1].splitlines() if l.strip().startswith("acmeapi")][0]
    assert "stale" in row


def test_brief_drift_is_the_headline(make_receipt, foreman_dir, state_dir, index_path, now):
    from collectors import config_resolve as C
    make_receipt("paysvc", "docs-sync", ago(now, hours=1), "green")
    conn = db.open_index(index_path)
    res = C.merge_layers({"user": {"model": "sonnet"}})
    C.reconcile_probe(res, {"model": "opus"})                 # a real disagreement -> drift
    C.write_snapshot(conn, project="paysvc", host="mbp", taken=ago(now, hours=1),
                     cc_version="2.0", resolved=res, components=[], probe_ok=True)
    conn.commit()

    out = brief.render(state_dir, foreman_dir, now=now, index_path=index_path)
    # DRIFT leads the board: it appears before NEEDS YOU and the CADENCES grid.
    assert out.index("DRIFT") < out.index("NEEDS YOU") < out.index("CADENCES")
    assert "model resolved by probe" in out


# ---------------------------------------------------------------------- decisions

def test_decision_lifecycle(state_dir, foreman_dir, tmp_path):
    proj = tmp_path / "paysvc_wt"
    proj.mkdir()
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    for p in reg["projects"]:
        if p["slug"] == "paysvc":
            p["worktree"] = {"mbp": str(proj)}
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))

    D.enqueue(state_dir, "paysvc", "note", {"text": "beta Friday"}, "op")
    D.enqueue(state_dir, "paysvc", "apply_setting",
              {"path": "permissions.deny", "op": "append", "value": ["Bash(rm:*)"]}, "op")
    out = D.drain_session(state_dir, foreman_dir, proj, host="mbp", session_uuid="s1")
    assert out["project"] == "paysvc" and len(out["applied"]) == 2
    assert "beta Friday" in out["context"]
    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["deny"] == ["Bash(rm:*)"]
    # idempotent
    assert D.drain_session(state_dir, foreman_dir, proj, host="mbp", session_uuid="s2")["applied"] == []


def test_approve_push_is_manual_only_then_pushes(state_dir, foreman_dir, monkeypatch):
    # the approval gate: an approve_push decision waits for explicit approval, never auto-drains
    _, dec = D.enqueue(state_dir, "paysvc", "approve_push",
                       {"cadence": "quality-review", "branch": "foreman/qr", "base": "main",
                        "ahead": 2}, "scheduler")
    assert dec["requires_session"] is False

    applied = D.drain_supervisor(state_dir, foreman_dir, host="mini")
    assert all(a["decision_id"] != dec["decision_id"] for a in applied)     # not auto-applied
    assert any(d["decision_id"] == dec["decision_id"]                       # still pending
               for _, d in D.load_pending(state_dir, "paysvc"))

    # explicit operator approval pushes the branch + opens the PR (remote stubbed)
    seen = {}
    monkeypatch.setattr(D, "push_branch",
                        lambda repo, payload: seen.update(repo=repo, **payload) or "pushed foreman/qr, PR #7")
    res = D.apply_decision(state_dir, foreman_dir, dec["decision_id"], host="mbp")
    assert res["ok"] and "PR #7" in res["result"]
    assert seen["branch"] == "foreman/qr" and seen["base"] == "main"
    # terminal now, not pending
    assert not any(d["decision_id"] == dec["decision_id"]
                   for _, d in D.load_pending(state_dir, "paysvc"))


def test_approve_push_reject_dismisses(state_dir, foreman_dir):
    _, dec = D.enqueue(state_dir, "paysvc", "approve_push",
                       {"branch": "foreman/x", "base": "main"}, "scheduler")
    assert D.dismiss(state_dir, dec["decision_id"]) is True
    assert not D.load_pending(state_dir, "paysvc")                          # rejected, gone


def test_decision_gc_expiry(state_dir, now):
    D.enqueue(state_dir, "paysvc", "note", {"text": "stale"}, "op", expires="2020-01-01T00:00:00Z")
    res = D.gc(state_dir, now=now)
    assert len(res["expired"]) == 1
    assert "EXPIRED" in D.board(state_dir, now=now)


def test_apply_and_dismiss_decisions(state_dir, foreman_dir, tmp_path):
    proj = tmp_path / "wt"
    proj.mkdir()
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    for p in reg["projects"]:
        if p["slug"] == "paysvc":
            p["worktree"] = {"mbp": str(proj)}
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))

    # headless kind (apply_setting) applies immediately, no window needed -> edits the file
    _, d1 = D.enqueue(state_dir, "paysvc", "apply_setting",
                      {"path": "permissions.deny", "op": "append", "value": ["Bash(rm:*)"]}, "op")
    assert D.can_apply_headless(d1)
    res = D.apply_decision(state_dir, foreman_dir, d1["decision_id"], host="mbp")
    assert res["ok"]
    assert json.loads((proj / ".claude" / "settings.json").read_text())["permissions"]["deny"] == ["Bash(rm:*)"]

    # session-delivery kind (note) is refused with no window -- not faked
    _, d2 = D.enqueue(state_dir, "paysvc", "note", {"text": "hi"}, "op")
    assert not D.can_apply_headless(d2)
    res2 = D.apply_decision(state_dir, foreman_dir, d2["decision_id"], host="mbp")
    assert res2["ok"] is False and "session" in res2["reason"]

    # dismiss clears it from the queue
    assert D.dismiss(state_dir, d2["decision_id"]) is True
    assert "note" not in {dec["kind"] for _, dec in D.load_pending(state_dir)}
    assert D.dismiss(state_dir, "01NOTAREALULID000000000000") is False


# --------------------------------------------------------------------- escalations

def test_escalation_full_lifecycle(make_receipt, foreman_dir, state_dir, fake_github, now):
    make_receipt("sentrygw", "quality-review", ago(now, days=1), "red",
                 escalations=[{"severity": "red", "summary": "3 stuck", "evidence": "e"}],
                 next_action="Merge the stuck PRs")
    conn = db.open_index(":memory:")
    r1 = escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)
    assert r1["opened_issues"] == 1 and fake_github.issues[1]["title"] == "[foreman] quality-review red on sentrygw"

    # repeat reconcile (same run) opens/says nothing
    assert escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)["opened_issues"] == 0

    # a new red run comments
    make_receipt("sentrygw", "quality-review", ago(now, hours=6), "red", next_action="still stuck")
    assert escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)["commented"] == 1

    # a green run closes with the clearing receipt link
    green = make_receipt("sentrygw", "quality-review", ago(now, hours=1), "green")
    escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)
    assert fake_github.issues[1]["state"] == "closed"
    close = [c for c in fake_github.issues[1]["comments"] if c.startswith("CLOSE:")][0]
    assert green["run_id"] in close and "receipts/sentrygw/quality-review/" in close


def test_close_resolves(make_receipt, foreman_dir, state_dir, fake_github, now):
    make_receipt("sentrygw", "quality-review", ago(now, days=1), "red")
    conn = db.open_index(":memory:")
    escalations.reconcile(conn, state_dir, foreman_dir, github=fake_github, now=now)
    eid = conn.execute("SELECT id FROM escalation WHERE project='sentrygw'").fetchone()["id"]
    assert escalations.close(conn, eid, "handled", foreman_dir=foreman_dir, github=fake_github) is True
    assert fake_github.issues[1]["state"] == "closed"
    assert escalations.close(conn, eid, "again", foreman_dir=foreman_dir, github=fake_github) is False


# ------------------------------------------------------------------------ dispatch

def test_dispatch_and_quota_guard(conn, foreman_dir, state_dir, now):
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    quota.record(conn, pct_used=40, taken=now.strftime(ISO))
    res = dispatch.dispatch(conn, reg, state_dir, "acmeapi", "docs-sync")
    assert res["deferred"] is False and "dispatched" in res
    quota.record(conn, pct_used=92, taken=(now + dt.timedelta(minutes=1)).strftime(ISO))
    assert dispatch.dispatch(conn, reg, state_dir, "acmeapi", "quality-review")["deferred"] is True
    # a red cadence is never deferred
    conn.execute("INSERT INTO escalation(id,project,cadence,severity,opened,first_seen) "
                 "VALUES(1,'acmeapi','quality-review','red',?,?)", (ago(now, 2), ago(now, 2)))
    conn.commit()
    assert dispatch.dispatch(conn, reg, state_dir, "acmeapi", "quality-review")["deferred"] is False
    with pytest.raises(ValueError):
        dispatch.dispatch(conn, reg, state_dir, "paysvc", "beta-readiness")


# ------------------------------------------------------------------------ promote

def test_promote_bumps_and_verifies(tmp_path):
    from tests.conftest import REPO
    fm = tmp_path / "fm"
    fm.mkdir()
    shutil.copytree(REPO / ".claude-plugin", fm / ".claude-plugin")
    (fm / "registry.yaml").write_text((REPO / "registry.yaml").read_text())
    src = tmp_path / "src"
    (src / "myskill").mkdir(parents=True)
    (src / "myskill" / "SKILL.md").write_text("---\nname: myskill\ndescription: d\n---\nbody\n")
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    pr = promote.promote(fm, reg, "myskill", source_dir=src,
                         probe_fn=lambda slug: {"skills": [{"name": "myskill", "invocable": True}]})
    assert pr["version"] == "0.1.1" and pr["propagated"] is True
    assert (fm / ".claude-plugin/foreman-ops/skills/myskill/SKILL.md").is_file()
    assert 'marketplace_pin: "0.1.1"' in (fm / "registry.yaml").read_text()
    assert "always-on plugin cost" in (fm / "registry.yaml").read_text()  # comments preserved


# ----------------------------------------------------------------------- retention

def test_retention_prunes(conn, now):
    conn.execute("INSERT INTO session(session_uuid,started) VALUES('old',?)", (ago(now, 200),))
    conn.execute("INSERT INTO session(session_uuid,started) VALUES('new',?)", (ago(now, 10),))
    for h in (5, 3, 1):
        conn.execute("INSERT INTO git_state(project,host,taken,dirty_files) VALUES('p','mbp',?,?)",
                     ((now - dt.timedelta(days=40, hours=h)).strftime(ISO), h))
    conn.execute("INSERT INTO telemetry_day(day,project,host,model,edit_accept) VALUES('2023-01-05','p','m','o',3)")
    conn.execute("INSERT INTO telemetry_day(day,project,host,model,edit_accept) VALUES('2023-01-20','p','m','o',4)")
    conn.commit()
    retention.run_all(conn, now=now)
    assert conn.execute("SELECT COUNT(*) c FROM session").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM git_state").fetchone()["c"] == 1
    td = conn.execute("SELECT day,edit_accept FROM telemetry_day WHERE day LIKE '2023%'").fetchall()
    assert len(td) == 1 and td[0]["day"] == "2023-01" and td[0]["edit_accept"] == 7


# ------------------------------------------------------------------------- secrets

def test_secrets_push_inventory_rotate(tmp_path, conn, now):
    proj = tmp_path / "wt"
    proj.mkdir()
    reg = {"projects": [{"slug": "acmeapi", "worktree": {"mbp": str(proj)},
                         "env_keys": ["ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"]}]}
    backend = secrets.DictBackend({"ANTHROPIC_API_KEY": "sk-abc", "ELEVENLABS_API_KEY": "el'q"})
    res = secrets.push(backend, reg, "acmeapi", "mbp")
    assert res["written"] == ["ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"]
    import os
    import stat
    env = proj / ".env"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert "ANTHROPIC_API_KEY='sk-abc'" in env.read_text()
    assert "el'\\''q" in env.read_text()  # single-quote escaped

    secrets.inventory(conn, backend, reg, expiry={"ANTHROPIC_API_KEY": "2026-09-10T00:00:00Z"}, now=now)
    row = conn.execute("SELECT * FROM credential WHERE name='ANTHROPIC_API_KEY'").fetchone()
    assert "value" not in row.keys() and row["expires"] == "2026-09-10T00:00:00Z"
    assert secrets.expiring_credentials(conn, now=now, days=14)[0]["name"] == "ANTHROPIC_API_KEY"

    rot = secrets.rotate(backend, reg, "ANTHROPIC_API_KEY", "sk-NEW", "mbp")
    assert rot["pushed_to"] == ["acmeapi"] and "sk-NEW" in env.read_text()


def test_secrets_default_and_per_project_override(tmp_path, conn):
    from collectors import secrets
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    reg = {"projects": [
        {"slug": "alpha", "worktree": {"mbp": str(a)}, "env_keys": ["ANTHROPIC_API_KEY"]},
        {"slug": "beta", "worktree": {"mbp": str(b)}, "env_keys": ["ANTHROPIC_API_KEY"]},
    ]}
    # one default, plus a per-project override for beta
    backend = secrets.DictBackend({
        "ANTHROPIC_API_KEY": "sk-default",
        "project:beta/ANTHROPIC_API_KEY": "sk-beta-only",
    })
    # resolution precedence
    assert secrets.resolve_key(backend, "ANTHROPIC_API_KEY", "alpha", "mbp") == ("sk-default", "default")
    assert secrets.resolve_key(backend, "ANTHROPIC_API_KEY", "beta", "mbp") == ("sk-beta-only", "project")

    ra = secrets.push(backend, reg, "alpha", "mbp")
    rb = secrets.push(backend, reg, "beta", "mbp")
    assert "sk-default" in (a / ".env").read_text() and not ra["overrides"]
    assert "sk-beta-only" in (b / ".env").read_text() and rb["overrides"] == {"ANTHROPIC_API_KEY": "project"}

    # host override applies when no project override
    backend.set(secrets.scoped_name("ANTHROPIC_API_KEY", host="vps"), "sk-vps")
    assert secrets.resolve_key(backend, "ANTHROPIC_API_KEY", "alpha", "vps") == ("sk-vps", "host")

    # rotating a project override only touches that project's key
    secrets.rotate(backend, reg, "ANTHROPIC_API_KEY", "sk-beta-2", "mbp", scope_project="beta")
    assert backend.get("project:beta/ANTHROPIC_API_KEY") == "sk-beta-2"
    assert backend.get("ANTHROPIC_API_KEY") == "sk-default"   # default untouched

    # overrides are inventoried too (names only, never values)
    n = secrets.inventory(conn, backend, reg, now=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
    rows = {r["name"]: r["scope"] for r in conn.execute("SELECT name, scope FROM credential")}
    assert rows.get("project:beta/ANTHROPIC_API_KEY") == "project:beta"
    assert "value" not in "".join(rows)  # sanity: only names/scopes stored


# ------------------------------------------------------------------------ discover

def test_discover_from_index_surfaces_unregistered(conn):
    reg = {"projects": [{"slug": "known", "worktree": {"mbp": "/work/known"}}]}
    conn.execute("INSERT INTO session(session_uuid,cwd,started) VALUES('a','/work/known','t')")
    conn.execute("INSERT INTO session(session_uuid,cwd,started) VALUES('b','/work/mystery','t')")
    conn.execute("INSERT INTO session(session_uuid,cwd,started) VALUES('c','/work/mystery','t')")
    conn.commit()
    found = discover.discover_from_index(conn, reg)
    assert [e["cwd"] for e in found] == ["/work/mystery"]        # registered dir excluded
    assert found[0]["sessions"] == 2
    assert "slug: mystery" in discover.suggest_stanza("/work/mystery")


# --------------------------------------------------------------------------- loops

def test_loops_enable_materializes_valid_cadence(foreman_dir):
    from collectors import loops
    from collectors import validate as V

    names = {e["loop"] for e in loops.catalog(foreman_dir)}
    assert {"security-review", "perf-review", "prod-readiness", "test-review", "pen-test"} <= names

    # a materialized loop's generated cadence + prompt pass the contract (pen-test here)
    loops.enable(foreman_dir, "sentrygw", "pen-test")
    from collectors import validate as _V
    assert _V.validate(foreman_dir).ok()
    assert "Authorized scope" in (foreman_dir / "prompts" / "pen-test.md").read_text()

    # prod-readiness is a catalog loop with no cadence file yet -> enabling it materializes one
    # (security-review/test-review/quality-review/perf-review are now committed cadences on
    # foreman, so they're not "created" any more).
    out = loops.enable(foreman_dir, "acmeapi", "prod-readiness")
    assert out["created_cadence"] is True
    assert (foreman_dir / "cadences" / "prod-readiness.yaml").is_file()
    assert (foreman_dir / "prompts" / "prod-readiness.md").is_file()
    assert V.validate(foreman_dir).ok()  # generated cadence + prompt pass the contract
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    tv = next(p for p in reg["projects"] if p["slug"] == "acmeapi")
    assert "prod-readiness" in tv["cadences"]

    # enabling a built-in for a new project just wires applies_to + registry (idempotent)
    loops.enable(foreman_dir, "paysvc", "beta-readiness")
    loops.enable(foreman_dir, "paysvc", "beta-readiness")
    assert V.validate(foreman_dir).ok()
    reg2 = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    sn = next(p for p in reg2["projects"] if p["slug"] == "paysvc")
    assert sn["cadences"].count("beta-readiness") == 1

    with pytest.raises(ValueError):
        loops.enable(foreman_dir, "acmeapi", "no-such-loop")


def test_loops_disable_unwires_project(foreman_dir):
    from collectors import loops, scheduler
    from collectors import validate as V

    loops.enable(foreman_dir, "sentrygw", "security-review")
    scheduler.set_loop_tier(foreman_dir, "sentrygw", "security-review", "local")
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    assert "security-review" in next(p for p in reg["projects"] if p["slug"] == "sentrygw")["cadences"]

    out = loops.disable(foreman_dir, "sentrygw", "security-review")
    assert out["removed"] is True
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    sentrygw = next(p for p in reg["projects"] if p["slug"] == "sentrygw")
    assert "security-review" not in sentrygw["cadences"]         # dropped from the project
    assert "security-review" not in (sentrygw.get("tiers") or {})  # override cleaned up
    cad = yaml.safe_load((foreman_dir / "cadences" / "security-review.yaml").read_text())
    assert "sentrygw" not in cad["applies_to"]                   # removed from applies_to
    assert (foreman_dir / "cadences" / "security-review.yaml").is_file()  # file kept (invariant 1)
    assert V.validate(foreman_dir).ok()
    # idempotent: disabling again is a no-op
    assert loops.disable(foreman_dir, "sentrygw", "security-review")["removed"] is False


def test_loops_customize_builtin(foreman_dir):
    from collectors import loops
    from collectors import validate as V

    # start from a built-in with no materialised cadence file (the customise path only applies
    # before the loop has been enabled/materialised anywhere)
    (foreman_dir / "cadences" / "arch-review.yaml").unlink(missing_ok=True)
    (foreman_dir / "prompts" / "arch-review.md").unlink(missing_ok=True)
    # drop it from any project's cadences so the orphaned reference doesn't fail validate
    reg_text = (foreman_dir / "registry.yaml").read_text().replace(", arch-review]", "]")
    (foreman_dir / "registry.yaml").write_text(reg_text)

    # customising a built-in copies its spec into the editable library and that copy wins
    res = loops.customize(foreman_dir, "arch-review")
    lib_file = foreman_dir / "loops" / "arch-review.yaml"
    assert lib_file.is_file() and res["template"] == str(lib_file)
    assert loops._spec_for(foreman_dir, "arch-review")["title"] == "Architecture and boundaries review"
    # the catalog lists it once, marked edited (not duplicated as a separate library row)
    rows = [e for e in loops.catalog(foreman_dir) if e["loop"] == "arch-review"]
    assert len(rows) == 1 and rows[0]["kind"] == "built-in (edited)"
    # an edit to the library copy takes precedence when the loop is enabled
    spec = yaml.safe_load(lib_file.read_text())
    spec["title"] = "Arch review (customised)"
    lib_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    loops.enable(foreman_dir, "sentrygw", "arch-review")
    assert V.validate(foreman_dir).ok()
    cad = yaml.safe_load((foreman_dir / "cadences" / "arch-review.yaml").read_text())
    assert cad["title"] == "Arch review (customised)"
    # idempotent, and a bare built-in (no spec) can't be customised
    loops.customize(foreman_dir, "arch-review")            # no clobber, no raise
    with pytest.raises(ValueError):
        loops.customize(foreman_dir, "docs-sync")          # bare built-in -> edit its file instead
    with pytest.raises(ValueError):
        loops.customize(foreman_dir, "no-such-loop")


def test_loops_registry_edit_handles_commented_slug():
    from collectors import loops
    text = ("projects:\n"
            "  - slug: acmeapi  # real project, discovered\n"
            "    tier_default: cloud\n"
            "    cadences: [docs-sync]\n")
    out = loops._add_to_project_cadences(text, "acmeapi", "security-review")
    assert "cadences: [docs-sync, security-review]" in out


def test_loops_library_new_enable_create(foreman_dir):
    from collectors import loops
    from collectors import validate as V

    # add a reusable custom loop template to the library
    res = loops.new(foreman_dir, "compliance-check", metrics=["control_gaps", "controls_pct"],
                    title="Compliance review")
    assert (foreman_dir / "loops" / "compliance-check.yaml").is_file()
    assert "compliance-check.yaml" in res["template"]
    cat = {e["loop"]: e["kind"] for e in loops.catalog(foreman_dir)}
    assert cat.get("compliance-check") == "library"

    # enabling a library loop materialises a valid cadence + prompt and wires the registry
    loops.enable(foreman_dir, "acmeapi", "compliance-check")
    assert (foreman_dir / "cadences" / "compliance-check.yaml").is_file()
    assert (foreman_dir / "prompts" / "compliance-check.md").is_file()
    assert V.validate(foreman_dir).ok()
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    assert "compliance-check" in next(p for p in reg["projects"] if p["slug"] == "acmeapi")["cadences"]
    # still one catalog entry (library), not duplicated as "custom"
    assert [e["kind"] for e in loops.catalog(foreman_dir) if e["loop"] == "compliance-check"] == ["library"]

    # create = new template + enable in one step
    loops.create(foreman_dir, "supply-chain", "sentrygw", metrics=["unpinned_deps"])
    assert V.validate(foreman_dir).ok()

    with pytest.raises(ValueError):
        loops.new(foreman_dir, "compliance-check")     # duplicate
    with pytest.raises(ValueError):
        loops.new(foreman_dir, "Bad Name")             # not kebab-case


def test_frequency_preset_cron_is_deterministic_and_jitter_safe():
    from collectors import scheduler as S
    from collectors.validate import Cron
    for preset in S.FREQ_PRESETS:
        a = S.preset_cron(preset, "acmeapi", "arch-review")
        assert a == S.preset_cron(preset, "acmeapi", "arch-review")   # deterministic
        Cron.parse(a)                                                    # valid cron
        minute = int(a.split()[0])
        assert minute not in (0, 30)                                     # dodges one-shot jitter
    # two projects on the same preset don't land on the same minute+day
    assert (S.preset_cron("weekly", "acmeapi", "arch-review")
            != S.preset_cron("weekly", "paysvc", "arch-review"))
    with pytest.raises(ValueError):
        S.preset_cron("hourly", "acmeapi", "arch-review")


def test_effective_schedule_respects_preset_and_autorun_mandate(foreman_dir):
    from collectors import scheduler as S
    reg_path = foreman_dir / "registry.yaml"
    default = "43 8 * * 2"

    # queue-mode project: setting a preset is refused (mandate), effective stays the default
    with pytest.raises(ValueError):
        S.set_loop_schedule(foreman_dir, "acmeapi", "quality-review", "daily")
    reg = yaml.safe_load(reg_path.read_text())
    assert S.effective_schedule(reg, "acmeapi", "quality-review", default) == default

    # turn on auto-run, then a preset is accepted and drives the effective schedule
    S.set_project_autorun(foreman_dir, "acmeapi", True)
    S.set_loop_schedule(foreman_dir, "acmeapi", "quality-review", "daily")
    reg = yaml.safe_load(reg_path.read_text())
    assert S.loop_preset(reg, "acmeapi", "quality-review") == "daily"
    # effective schedule applies the registry's autorun_window (§6) to the preset
    assert S.effective_schedule(reg, "acmeapi", "quality-review", default) \
        == S.preset_cron("daily", "acmeapi", "quality-review", S.autorun_window(reg, "acmeapi"))

    # a second override coexists in the same schedules block, and validate still passes
    S.set_loop_schedule(foreman_dir, "acmeapi", "docs-sync", "weekly")
    from collectors import validate as V
    assert V.validate(foreman_dir).ok()
    reg = yaml.safe_load(reg_path.read_text())
    tv = next(p for p in reg["projects"] if p["slug"] == "acmeapi")
    assert tv["schedules"] == {"quality-review": "daily", "docs-sync": "weekly"}

    # flipping back to queue neutralises presets: effective falls back to the cadence default
    S.set_project_autorun(foreman_dir, "acmeapi", False)
    reg = yaml.safe_load(reg_path.read_text())
    assert S.effective_schedule(reg, "acmeapi", "quality-review", default) == default

    # clearing an override is allowed even in queue mode, and empties the block cleanly
    S.set_project_autorun(foreman_dir, "acmeapi", True)
    S.set_loop_schedule(foreman_dir, "acmeapi", "quality-review", "default")
    S.set_loop_schedule(foreman_dir, "acmeapi", "docs-sync", "")
    reg = yaml.safe_load(reg_path.read_text())
    tv = next(p for p in reg["projects"] if p["slug"] == "acmeapi")
    assert "schedules" not in tv or not tv["schedules"]
    assert V.validate(foreman_dir).ok()


def test_validate_rejects_schedule_override_for_unlisted_loop(foreman_dir):
    from collectors import validate as V
    reg_path = foreman_dir / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text())
    tv = next(p for p in reg["projects"] if p["slug"] == "acmeapi")
    tv["schedules"] = {"perf-review": "daily"}      # perf-review is not in acmeapi's cadences
    reg_path.write_text(yaml.safe_dump(reg))
    rep = V.validate(foreman_dir)
    assert not rep.ok()
    assert any("perf-review" in e for e in rep.errors)


def test_register_project_and_scan(foreman_dir, tmp_path):
    import subprocess
    from collectors import discover as X
    from collectors import validate as V

    assert X._parse_remote("git@github.com:acme-demo/alpha.git") == ("acme-demo", "alpha")
    assert X._parse_remote("https://github.com/other/Beta") == ("other", "Beta")
    # slug must be registry-legal even when the repo name has dots/underscores/caps
    assert X._slugify("autofillapp") == "autofillapp"
    assert X._slugify("DevCompanion") == "devcompanion"
    assert X._slugify("InventoryApp") == "inventoryapp"

    base = tmp_path / "code"; base.mkdir()

    def mkrepo(name, remote):
        p = base / name; p.mkdir()
        subprocess.run(["git", "init", "-q", str(p)], check=True)
        subprocess.run(["git", "-C", str(p), "remote", "add", "origin", remote], check=True)
        return p
    a = mkrepo("alpha-local", "git@github.com:acme-demo/alpha.git")   # folder != repo name
    mkrepo("beta", "https://github.com/other/beta.git")

    # scan owner-filtered surfaces only acme-demo, matched by origin (not folder name)
    hits = X.scan_dir_for_repos(base, owner="acme-demo")
    assert [h["repo"] for h in hits] == ["acme-demo/alpha"]

    # register one: slug follows the GitHub repo, and the registry stays valid
    res = X.register_project(foreman_dir, a, host="mbp")
    assert res["registered"] == "alpha" and res["repo"] == "acme-demo/alpha"
    assert V.validate(foreman_dir).ok()
    assert X.register_project(foreman_dir, a, host="mbp").get("skipped")   # idempotent

    # scan + apply (no owner filter) registers the rest; still valid
    X.register_from_dir(foreman_dir, base, host="mbp", apply=True)
    slugs = {p["slug"] for p in
             yaml.safe_load((foreman_dir / "registry.yaml").read_text())["projects"]}
    assert {"alpha", "beta"} <= slugs
    assert V.validate(foreman_dir).ok()


def test_dashboard_loops_catalog_section(foreman_dir, tmp_path):
    from collectors import web
    h = web.render(web._gather(foreman_dir, tmp_path / "state", None), refresh=0)
    assert "Loop library" in h                           # the catalogue / manage section exists
    assert "/loop-enable" in h and "/loop-new" in h      # enable + add controls
    # a built-in with a cadence file offers an edit button; the New-loop form is present
    assert "/loop-edit" in h and "✎ edit" in h
    assert 'name="metrics"' in h                          # add-loop form fields


def test_board_folds_unregistered_projects(state_dir):
    D.enqueue(state_dir, "acmeapi", "dispatch_cadence", {"cadence": "docs-sync"}, "op")
    D.enqueue(state_dir, "ghost", "dispatch_cadence", {"cadence": "x"}, "op")
    b = D.board(state_dir, known={"acmeapi"})
    assert "acmeapi" in b                               # registered shows normally
    assert "ghost" in b and "unregistered" in b           # orphan folded into the warning line
    assert "ghost        1 pending" not in b              # not an unlabeled peer row
    # without a known set, behaviour is unchanged (both listed as peers)
    b2 = D.board(state_dir)
    assert "ghost" in b2 and "unregistered" not in b2


def test_loops_schedule_cli(foreman_dir):
    from collectors import loops, scheduler
    # queue mode -> clean error, exit 2 (no traceback)
    assert loops.main(["--foreman-dir", str(foreman_dir),
                       "schedule", "acmeapi", "quality-review", "daily"]) == 2
    scheduler.set_project_autorun(foreman_dir, "acmeapi", True)
    assert loops.main(["--foreman-dir", str(foreman_dir),
                       "schedule", "acmeapi", "quality-review", "weekly"]) == 0
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    tv = next(p for p in reg["projects"] if p["slug"] == "acmeapi")
    assert tv["schedules"]["quality-review"] == "weekly"
    # clear the override
    assert loops.main(["--foreman-dir", str(foreman_dir),
                       "schedule", "acmeapi", "quality-review", "default"]) == 0
    from collectors import validate as V
    assert V.validate(foreman_dir).ok()


def test_env_edit_whitelist_blocks_traversal(tmp_path):
    from collectors import secrets
    wt = tmp_path / "proj"; wt.mkdir()
    envf = wt / ".env"; envf.write_text("K=v\n")
    (wt / "notes.txt").write_text("x")
    assert secrets.is_live_env_file(str(envf), [str(wt)]) is True
    assert secrets.is_live_env_file(str(wt / "notes.txt"), [str(wt)]) is False   # not a .env
    assert secrets.is_live_env_file("/etc/passwd", [str(wt)]) is False           # outside worktrees
    assert secrets.is_live_env_file(str(wt / ".." / ".." / "etc" / "passwd"), [str(wt)]) is False


def test_dashboard_refresh_reflects_env_edit(foreman_dir, tmp_path):
    """Re-rendering the board (what the auto-refresh does) recomputes key fingerprints from
    disk, so editing a .env shows up in the highlighting on the next refresh."""
    from collectors import web
    wt = tmp_path / "paysvc_wt"; wt.mkdir()
    (wt / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-xxxxxOLD12\n")
    reg_path = foreman_dir / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text())          # point paysvc's worktree at our tmp .env
    next(p for p in reg["projects"] if p["slug"] == "paysvc")["worktree"] = {"mbp": str(wt)}
    reg_path.write_text(yaml.safe_dump(reg))

    first = web.render(web._gather(foreman_dir, tmp_path / "state", None), refresh=30)
    assert "/env-edit" in first and "✎ .env" in first          # edit button for the real file
    assert "OLD12" in first                                      # fingerprint = last 5 chars

    (wt / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-xxxxxNEW99\n")   # operator edits the .env
    second = web.render(web._gather(foreman_dir, tmp_path / "state", None), refresh=30)
    assert "NEW99" in second and "OLD12" not in second          # refresh picked up the new value


def test_loops_last_run_status(conn, foreman_dir):
    from collectors import loops
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r1','docs-sync','acmeapi','c','cloud','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','ok','green')")
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r2','docs-sync','acmeapi','c','cloud','2026-09-05T00:00:00Z','2026-09-05T00:00:00Z','ok','amber')")
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r3','docs-sync','acmeapi','c','cloud','2026-09-06T00:00:00Z','2026-09-06T00:00:00Z','locked','green')")
    conn.commit()
    lr = loops.last_runs(conn)
    assert lr[("acmeapi", "docs-sync")]["ended"] == "2026-09-05T00:00:00Z"   # latest real run
    assert lr[("acmeapi", "docs-sync")]["verdict"] == "amber"                # locked run ignored
    st = {s["loop"]: s for s in loops.status(foreman_dir, "acmeapi", conn)}
    assert st["docs-sync"]["last_run"] == "2026-09-05T00:00:00Z"
    assert st["beta-readiness"]["last_run"] is None and st["beta-readiness"]["status"] == "never run"

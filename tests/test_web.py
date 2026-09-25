"""The web dashboard renders the board as HTML."""

from collectors import web, quota, keyscan


def test_keyscan_groups_only_matching_values(foreman_dir):
    # a and b share the SAME value hash for SHARED_KEY; c has a different value -> not grouped
    dbp = {"a": {"SHARED_KEY": "h1", "SOLO": "x"}, "b": {"SHARED_KEY": "h1"},
           "c": {"SHARED_KEY": "h2"}}
    ks = keyscan.build_keyshare({}, "mbp", digests_by_project=dbp)
    assert ks["groups"] == [{"key": "SHARED_KEY", "projects": ["a", "b"]}]
    # the artifact carries no value/hash — only key + project membership
    assert "h1" not in __import__("json").dumps(ks)
    keyscan.write_keyshare(foreman_dir, ks)
    lk = keyscan.load_lookup(foreman_dir)
    assert lk[("a", "SHARED_KEY")] == ["b"] and lk[("b", "SHARED_KEY")] == ["a"]


def test_read_only_secrets_shared_badge(foreman_dir, state_dir, index_path):
    from collectors import db
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    data["keyshare"] = {("acmeapi", "ANTHROPIC_API_KEY"): ["sentrygw"]}  # pretend a shared value
    try:
        out = web.render(data, refresh=0, read_only=True)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "ANTHROPIC_API_KEY" in out and "same value in: sentrygw" in out


def test_read_only_render_hides_actions(foreman_dir, state_dir, make_receipt, index_path):
    from collectors import db
    make_receipt("acmeapi", "docs-sync", "2026-09-06T08:00:00Z", "green")
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data, refresh=0, read_only=True)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "read-only hosted view" in out                 # the banner
    assert "form,.acts{display:none" in out                # action controls hidden
    assert 'http-equiv="refresh"' not in out               # no auto-refresh when refresh=0
    assert "Loops" in out and "acmeapi" in out           # the status still renders


def test_loop_editor_prefilled_when_edit_param(foreman_dir, state_dir, index_path):
    from collectors import loops
    loops.author(foreman_dir, "link-check", title="Link check", metrics=["broken_links"],
                 green="broken_links == 0", amber="broken_links < 5",
                 prompt="Crawl the docs and follow every link.")
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        blank = web.render(data, refresh=0)
        edit = web.render(data, refresh=0, edit="link-check")
    finally:
        if data["conn"]:
            data["conn"].close()
    # the blank board shows the create form; the ?edit= board shows the pre-filled editor
    assert 'action="/loop-author"' in blank and "Create a new loop" in blank
    assert "Edit loop" in edit and "editing <b>link-check</b>" in edit
    assert 'value="Link check"' in edit and 'value="broken_links"' in edit
    assert "Crawl the docs and follow every link." in edit
    assert "## Report" not in edit                    # contract hidden; only the authored body shows


def test_dashboard_renders_core_sections(foreman_dir, state_dir, make_receipt, index_path):
    from collectors import db
    make_receipt("acmeapi", "docs-sync", "2026-09-06T08:00:00Z", "green")
    make_receipt("sentrygw", "quality-review", "2026-09-05T05:23:00Z", "red",
                 next_action="Merge the stuck PRs")
    # an index with a quota row so the header + banner render
    conn = db.open_index(index_path)
    quota.record(conn, pct_used=90, taken="2026-09-06T12:00:00Z")
    conn.close()

    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data, refresh=15)
    finally:
        if data["conn"]:
            data["conn"].close()

    for section in ("FOREMAN", "Needs you", "Loops", "Loop library", "Drift", "Queued",
                    "Repo status", "API keys"):
        assert section in out
    assert 'content="15"' in out                     # configurable refresh present
    assert "quota 10%" in out and "non-red cadences deferred" in out
    assert "/dispatch" in out                          # dispatch buttons wired
    assert "return confirm" in out                     # action buttons guarded by a confirm

    # the failing loop is a real to-do in Needs you; the whole per-project surface (incl.
    # never-run loops) now lives in the single Loops panel below it
    ny = out.split("Needs you")[1].split(">Loops<")[0]
    assert "sentrygw / quality-review" in ny            # red is in the to-do list (Needs you)
    loops_panel = out.split(">Loops<")[1].split(">Loop library<")[0]
    assert 'data-acc="loops"' in loops_panel and 'data-repo="acmeapi"' in loops_panel
    assert "quality-review" in loops_panel             # never-run loop shown with its verdict
    # a loop that has never produced a receipt reads "never run", not the misleading "stale"
    # (was-running-then-stopped) — the top intuitive-ux friction fix
    assert 'class="pill never"' in loops_panel and ">never run</span>" in loops_panel
    assert "not yet run" in out                        # and the state is explained in the legend key


def test_recent_activity_and_section(foreman_dir, state_dir, index_path):
    import datetime as dt
    from collectors import db, decisions as D
    conn = db.open_index(index_path)
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r1','docs-sync','acmeapi','mbp','cloud','2026-09-10T08:00:00Z',"
                 "'2026-09-10T08:05:00Z','ok','green')")
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r0','docs-sync','acmeapi','mbp','cloud','2020-01-01T00:00:00Z',"
                 "'2020-01-01T00:05:00Z','ok','green')")   # old -> excluded from the 7d window
    conn.execute("INSERT INTO escalation(id,run_id,project,cadence,severity,opened,first_seen) "
                 "VALUES(1,'r2','paysvc','quality-review','red','2026-09-09T10:00:00Z',"
                 "'2026-09-09T10:00:00Z')")
    conn.commit()
    D.enqueue(state_dir, "sentrygw", "dispatch_cadence", {"cadence": "docs-sync"}, "op")
    now = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)
    ev = web._recent_activity(conn, D.load_pending(state_dir), now, days=7)
    texts = [e["text"] for e in ev]
    assert any("acmeapi / docs-sync ran green" in t for t in texts)
    assert any("paysvc / quality-review escalated" in t for t in texts)
    # humanised, not the raw decision kind ("queued dispatch_cadence")
    assert any("sentrygw: queued a run (docs-sync)" in t for t in texts)
    assert ev == sorted(ev, key=lambda e: e["ts"], reverse=True)   # newest first
    assert not any(e["ts"].startswith("2020") for e in ev)         # window excludes old run
    # non-green events carry a detail (escalation summary / analysis reason); green don't
    conn2 = db.open_index(":memory:")
    conn2.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                  "VALUES('x','quality-review','paysvc','mbp','cloud','2026-09-10T08:00:00Z',"
                  "'2026-09-10T08:05:00Z','ok','red')")
    conn2.commit()
    a = {("paysvc", "quality-review"): {"reason": "2 high-severity findings",
                                         "latest": {"next_action": "rotate the leaked token"}}}
    ev2 = web._recent_activity(conn2, [], now, analysis=a)
    red = next(e for e in ev2 if e["lvl"] == "red")
    assert red["detail"] == "rotate the leaked token"
    conn2.close()
    conn.close()
    # renders as the top "Since you were away" section
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "Since you were away" in out and 'class="activity' in out
    assert 'id="newcount"' in out and 'data-now=' in out


def test_recent_verdicts_trend(index_path):
    from collectors import db, loops
    conn = db.open_index(index_path)
    for i, v in enumerate(["red", "red", "amber", "green"]):
        conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"r{i}", "docs-sync", "acmeapi", "mbp", "cloud",
                      f"2026-09-0{i+1}T08:00:00Z", f"2026-09-0{i+1}T08:05:00Z", "ok", v))
    # a failed run with no verdict counts as red on the trend
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r9','docs-sync','acmeapi','mbp','cloud','2026-09-05T08:00:00Z',"
                 "'2026-09-05T08:05:00Z','failed',NULL)")
    conn.commit()
    hist = loops.recent_verdicts(conn)
    assert hist[("acmeapi", "docs-sync")] == ["red", "red", "amber", "green", "red"]


def test_spend_and_sparkline_render(foreman_dir, state_dir, index_path):
    from collectors import db
    conn = db.open_index(index_path)
    db.upsert(conn, "telemetry_day", {
        "day": "2026-09-06", "project": "acmeapi", "host": "mbp", "model": "opus",
        "sessions": 3, "cost_usd": 4.25, "tokens": 1_200_000, "lines_added": 0,
        "lines_removed": 0, "commits": 0, "prs": 0, "edit_accept": 0, "edit_reject": 0,
        "active_seconds": 0}, keys=["day", "project", "host", "model"])
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('rr','docs-sync','acmeapi','mbp','cloud','2026-09-06T08:00:00Z',"
                 "'2026-09-06T08:05:00Z','ok','green')")
    conn.commit()
    conn.close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "Spend &amp; usage" in out
    assert "$4.25" in out                       # per-project cost rendered
    assert 'class="sparkline"' in out           # verdict trend rendered in the Loops panel


def test_repo_status_shows_stats_and_rollup(foreman_dir, state_dir, index_path):
    from collectors import db
    conn = db.open_index(index_path)
    conn.execute("INSERT INTO git_state(project,host,taken,branch,ahead,behind,dirty_files,"
                 "worktrees) VALUES('acmeapi','mbp','2026-09-06T08:00:00Z','main',2,0,3,1)")
    conn.execute("INSERT INTO github_state(project,taken,open_prs,failing_checks,"
                 "security_alerts,issues_open) VALUES('acmeapi','2026-09-06T08:00:00Z',4,1,0,5)")
    conn.commit()
    conn.close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "Repo status" in out
    assert 'class="stat' in out                 # always-on per-project stat tiles
    assert "dirty files" in out and "open PRs" in out and "CI checks" in out
    assert '<span class="n">1</span> failing checks' in out   # fleet roll-up total


def test_loops_panel_rows_have_action_buttons(foreman_dir, state_dir):
    # regression: the actions cell must NOT be a <td class="acts"> (display:flex collapses the
    # table column, hiding the buttons) — it wraps the forms in an inner div instead
    data = web._gather(foreman_dir, state_dir, None)
    out = web.render(data)
    panel = out.split(">Loops<")[1].split(">Loop library<")[0]
    assert 'td class="acts"' not in panel        # the layout-breaking form must be gone
    assert '<td><div class="acts"' in panel      # actions wrapped in an inner div
    assert 'action="/dispatch"' in panel and 'action="/loop-disable"' in panel  # run + remove


def test_refresh_can_be_disabled(foreman_dir, state_dir):
    data = web._gather(foreman_dir, state_dir, None)
    out = web.render(data, refresh=0)
    assert "http-equiv=\"refresh\"" not in out


def test_dashboard_exposes_management_controls(foreman_dir, state_dir):
    # the settings audit added: per-loop tier flip, remove-loop, and the time-of-day window —
    # all now inside the single Loops panel
    data = web._gather(foreman_dir, state_dir, None)
    out = web.render(data)
    for route in ('action="/loop-tier"', 'action="/loop-disable"', 'action="/autorun-window"'):
        assert route in out
    assert "window" in out                                # per-project window editor in Loops
    # loop library surfaces the safety envelope (writes / caps) as read-only chips
    assert ("writes:" in out) or ("read-only" in out)


def test_secret_fingerprint_match_coloring(foreman_dir, state_dir, tmp_path):
    import re
    import yaml
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
    (a / ".env").write_text("ANTHROPIC_API_KEY=sk-shared-TAIL5\n")
    (b / ".env").write_text("ANTHROPIC_API_KEY=sk-shared-TAIL5\n")   # same value -> a match
    (c / ".env").write_text("ANTHROPIC_API_KEY=sk-different-OTHER\n")  # unique
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    for p, wt in zip(reg["projects"], (a, b, c)):
        p["worktree"] = {"mbp": str(wt)}
        p["env_keys"] = ["ANTHROPIC_API_KEY"]
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))

    out = web.render(web._gather(foreman_dir, state_dir, None), fingerprints=True)
    # the shared-value alert is folded into the "API keys & secrets" section (no longer a
    # separate top-level "Shared keys" section)
    assert "shared across projects" in out and "Shared values" in out
    # the two matching projects show the same colour for …TAIL5, matched by full-value hash
    colored = re.findall(r'style="color:(#[0-9a-f]+)[^"]*" title="same value as: [^"]*">…TAIL5', out)
    assert len(colored) == 2 and colored[0] == colored[1]
    # the unique key is not given a match colour
    assert 'title="last 5 chars of the value in .env (unique here)">…OTHER' in out


def test_key_match_is_collision_proof(foreman_dir, state_dir, tmp_path):
    import yaml
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    # identical last-5 (SAME5) but different full values -> must NOT be treated as a match
    (a / ".env").write_text("KEY=aaaaaaaa-SAME5\n")
    (b / ".env").write_text("KEY=bbbbbbbb-SAME5\n")
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    # keep ONLY the two test projects so real on-host worktrees/.env files aren't scanned
    reg["projects"] = reg["projects"][:2]
    for p, wt in zip(reg["projects"], (a, b)):
        p["worktree"] = {"mbp": str(wt)}
        p["env_keys"] = ["KEY"]
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))

    out = web.render(web._gather(foreman_dir, state_dir, None), fingerprints=True)
    assert out.count("…SAME5") == 2               # both shown
    assert "Shared values" not in out             # but not matched (different full values)
    assert "same value as" not in out

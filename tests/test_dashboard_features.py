"""Thorough sweep of the dashboard features built this morning: the display helpers, the
"Since you were away" feed, Spend & usage, verdict sparklines, the "where it runs" relabel,
the Loop-library fleet dropdown, the handover-refresh loop, Run-now dispatch, and render
smoke tests across modes (local / read-only / no-index / empty)."""
import datetime as dt

from collectors import web, db, recent

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- pure display helpers

def test_money_formatting():
    assert web._money(0) == '<td class="money zero">$0</td>'
    assert web._money(4.25) == '<td class="money">$4.25</td>'
    assert "k" in web._money(1500) and "$1.5k" in web._money(1500)


def test_tokens_formatting():
    assert "0" in web._tokens(0)
    assert web._tokens(500) == "500"
    assert web._tokens(1500) == "2k"
    assert web._tokens(1_200_000) == "1.2M"


def test_ago():
    assert web._ago("2026-09-10T11:59:30Z", NOW) == "just now"
    assert web._ago("2026-09-10T10:00:00Z", NOW) == "2h ago"
    assert web._ago("2026-09-10T11:20:00Z", NOW) == "40m ago"
    assert web._ago("2026-09-08T12:00:00Z", NOW) == "2d ago"
    assert web._ago("not-a-date", NOW) == "not-a-date"          # tolerant


def test_sparkline():
    assert web._sparkline([]) == ""
    s = web._sparkline(["green", "red", "amber", "failed"])
    assert s.count('class="spark ') == 4      # one bar per verdict
    assert "spark green" in s and "spark red" in s and "spark amber" in s
    assert "spark stale" in s                 # unknown verdict -> stale bar


def test_gi_tolerates_missing():
    assert web._gi({"x": 5}, "x") == 5
    assert web._gi({"x": None}, "x") == 0
    assert web._gi({}, "x") == 0
    assert web._gi(None, "x") == 0


def test_dispatch_launchable(tmp_path):
    wt = tmp_path / "proj"
    wt.mkdir()
    reg = {"projects": [{"slug": "a", "worktree": {"mbp": str(wt)}},
                        {"slug": "b", "worktree": {"mbp": str(tmp_path / "gone")}},
                        {"slug": "c", "worktree": {"vps": str(wt)}}]}
    assert web._dispatch_launchable(reg, "a", "mbp") is True     # worktree exists on host
    assert web._dispatch_launchable(reg, "b", "mbp") is False    # path missing
    assert web._dispatch_launchable(reg, "c", "mbp") is False    # worktree only on another host
    assert web._dispatch_launchable(reg, "ghost", "mbp") is False


# ---------------------------------------------------------------- "Since you were away"

def test_recent_activity_cleared_and_cap():
    conn = db.open_index(":memory:")
    conn.execute("INSERT INTO escalation(id,run_id,project,cadence,severity,opened,first_seen,"
                 "resolved,summary) VALUES(1,'r','a','docs-sync','red','2026-09-09T10:00:00Z',"
                 "'2026-09-09T10:00:00Z','2026-09-10T09:00:00Z','was flaky')")
    conn.commit()
    ev = web._recent_activity(conn, [], NOW, days=7)
    assert any("cleared" in e["text"] for e in ev)              # resolved -> cleared event
    assert any("escalated" in e["text"] for e in ev)           # opened -> escalated event
    # limit is honoured
    for i in range(40):
        conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict)"
                     " VALUES(?,?,?,?,?,?,?,?,?)", (f"r{i}", "docs-sync", "a", "mbp", "cloud",
                     "2026-09-10T08:00:00Z", f"2026-09-10T08:{i:02d}:00Z", "ok", "green"))
    conn.commit()
    assert len(web._recent_activity(conn, [], NOW, days=7, limit=10)) == 10


def test_recent_activity_none_conn():
    assert web._recent_activity(None, [], NOW) == []


def test_activity_feed_is_capped_and_scrollable(foreman_dir, state_dir, index_path):
    # a busy week must not push the rest of the board down: the feed sits in a height-capped,
    # scrollable container (.actfeed) rather than rendering every row inline
    # timestamps must be within the feed's 7-day window, so anchor them to *now* rather than a
    # fixed date that ages out of the window as the calendar advances
    base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    conn = db.open_index(index_path)
    for i in range(25):
        ts = (base + dt.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict)"
                     " VALUES(?,?,?,?,?,?,?,?,?)", (f"r{i}", "docs-sync", "foreman", "mbp", "local",
                     ts, ts, "ok", "green"))
    conn.commit()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert 'class="card actfeed"' in out                 # feed rows live in the capped container
    assert ".actfeed{" in out and "overflow-y:auto" in out  # capped height + scroll


def test_top_sections_are_foldable(foreman_dir, state_dir, index_path):
    # the orientation/action sections above the fold are collapsible <details> (remembered per
    # browser) so a returning operator can compact the top of the board
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    # every top-level section folds, not just the top few — one uniform mechanism
    for sid in ("sec-away", "sec-needs", "sec-queued", "sec-loops", "sec-library",
                "sec-discovered", "sec-repo", "sec-spend", "sec-drift", "sec-secrets"):
        assert f'<details class="sec" id="{sid}"' in out          # foldable section present
    assert '<details class="sec" id="sec-away" open>' in out      # the catch-up feed always opens
    assert "<section" not in out.split("</head>")[1]              # no raw <section> left in the body
    assert "details.sec" in out                                   # the fold styling
    assert 'localStorage.setItem(sk,s.open' in out                # collapsed state remembered
    # the "since you were away" count badge stays in the summary, not the body
    away_summary = out.split('id="sec-away"')[1].split("</summary>")[0]
    assert 'id="newcount"' in away_summary


def test_collapsed_sections_show_a_peek_summary(foreman_dir, state_dir, index_path):
    # a folded section still gives the operator a reason to open it: a right-aligned digest in
    # the header, shown only while collapsed
    conn = db.open_index(index_path)
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict)"
                 " VALUES('r1','docs-sync','foreman','mbp','local','2026-09-10T08:00:00Z',"
                 "'2026-09-10T08:01:00Z','ok','green')")
    conn.commit()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "details.sec[open] .sec-sum{display:none}" in out       # peek hidden once expanded
    # each header carries a digest span inside its summary
    for sid in ("sec-away", "sec-needs", "sec-queued", "sec-loops", "sec-library",
                "sec-discovered"):
        summary = out.split(f'id="{sid}"')[1].split("</summary>")[0]
        assert 'class="sec-sum"' in summary


def _sec_open(out, sid):
    return f'<details class="sec" id="{sid}" open>' in out


def test_sections_default_open_on_signal(foreman_dir, state_dir, index_path):
    # calm board: only the always-open catch-up feed is open; the rest fold to their peek
    db.open_index(index_path).close()
    calm = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(calm)
    finally:
        if calm["conn"]:
            calm["conn"].close()
    assert _sec_open(out, "sec-away")                    # orientation always opens
    assert not _sec_open(out, "sec-loops")               # nothing red -> stays folded
    assert not _sec_open(out, "sec-spend")               # reference section -> folded
    assert not _sec_open(out, "sec-secrets")

    # a red repo verdict opens Repo status; a failing loop opens Loops
    conn = db.open_index(index_path)
    conn.execute("INSERT INTO git_state(project,host,taken,branch,dirty_files,largest_blob_mb) "
                 "VALUES('foreman','mbp','2026-09-10T08:00:00Z','main',0,120)")   # 120MB blob -> red
    conn.commit()
    conn.close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert _sec_open(out, "sec-repo")                     # red repo signal -> opens


def test_recently_worked_on_is_local_only(foreman_dir, state_dir, index_path, tmp_path, monkeypatch):
    import json, yaml
    # point a registered project's worktree at a cwd we have a transcript for
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    reg["projects"][0]["worktree"] = {"mbp": "/Users/x/proj0"}
    slug = reg["projects"][0]["slug"]
    (foreman_dir / "registry.yaml").write_text(yaml.safe_dump(reg))
    tr = tmp_path / "transcripts"
    (tr / recent.encode_cwd("/Users/x/proj0")).mkdir(parents=True)
    (tr / recent.encode_cwd("/Users/x/proj0") / "s.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/Users/x/proj0",
                    "message": {"role": "user", "content": "fix the CI failures"}}) + "\n")
    monkeypatch.setenv("FOREMAN_TRANSCRIPTS_ROOT", str(tr))
    monkeypatch.setenv("FOREMAN_HOST", "mbp")

    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        local = web.render(data)
        hosted = web.render(data, read_only=True)
    finally:
        if data["conn"]:
            data["conn"].close()
    # local dashboard shows the section with the prompt + source project
    assert 'id="sec-recent"' in local
    assert "fix the CI failures" in local and slug in local
    # hosted/read-only board must NOT leak prompt text
    assert 'id="sec-recent"' not in hosted
    assert "fix the CI failures" not in hosted


def test_recently_worked_on_search_and_week(foreman_dir, state_dir, index_path, tmp_path, monkeypatch):
    from collectors import prompts
    monkeypatch.setenv("FOREMAN_PROMPT_STORE", str(tmp_path / "p.db"))
    store = tmp_path / "p.db"
    # timestamps must land inside the "this week" window, so anchor to *now* rather than a fixed
    # date that ages out (the tally + roster only render for recent prompts)
    now = dt.datetime.now(dt.timezone.utc)
    def _ts(h):
        return (now - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = prompts.connect(store)
    conn.executemany("INSERT INTO prompt(project, ts, text, session) VALUES(?,?,?,?)", [
        ("foreman", _ts(2), "fix the scheduler bug", "s"),
        ("paysvc", _ts(3), "scheduler review", "s"),
        ("foreman", _ts(1), "add a dark mode toggle", "s")])
    conn.commit(); conn.close()
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        default = web.render(data)
        found = web.render(data, search="scheduler")
    finally:
        if data["conn"]:
            data["conn"].close()
    assert 'name="pq"' in default and "this week:" in default        # search box + tally
    # both matching prompts appear; the non-matching one does not (search replaces the roster)
    assert "matches for" in found and "fix the scheduler bug" in found and "scheduler review" in found
    assert "add a dark mode toggle" not in found


# ---------------------------------------------------------------- render smoke across modes

def test_hosted_verdict_fallback_from_run_table(foreman_dir, state_dir, index_path):
    # the hosted board has no receipts (state branch isn't deployed), only the committed
    # index.db — so a loop's verdict must fall back to the run table instead of reading "stale"
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.open_index(index_path)
    conn.execute("INSERT INTO run(run_id,cadence,project,host,tier,started,ended,status,verdict) "
                 "VALUES('r1','docs-sync','acmeapi','cloud','cloud',?,?,'ok','green')",
                 (recent, recent))
    conn.commit()
    conn.close()
    data = web._gather(foreman_dir, state_dir, index_path)     # state_dir has no receipts
    try:
        assert data["analysis"][("acmeapi", "docs-sync")]["effective"] == "green"
    finally:
        if data["conn"]:
            data["conn"].close()


def test_render_no_index(foreman_dir, state_dir):
    data = web._gather(foreman_dir, state_dir, None)           # conn is None
    out = web.render(data)
    for s in ("Since you were away", "Needs you", "Queued", "Loops", "Loop library"):
        assert s in out


def test_render_read_only_hides_actions(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data, read_only=True)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "form,.acts{display:none" in out                    # action controls hidden
    assert "Since you were away" not in out.split("<script>")[-1]  # personalise script gated off
    assert "Spend &amp; usage" in out


def test_where_it_runs_relabel(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data)
    finally:
        if data["conn"]:
            data["conn"].close()
    assert "where it runs" in out                              # the relabel
    assert "<th>tier</th>" not in out                          # no stray raw "tier" column


def test_loop_library_dropdown_shows_whole_fleet(foreman_dir, state_dir):
    # docs-sync is enabled widely; the enable dropdown should still list every project,
    # greying out the already-enabled ones rather than hiding them
    out = web.render(web._gather(foreman_dir, state_dir, None))
    lib = out.split(">Loop library<")[1].split(">Repo status<")[0]
    assert "· ✓ enabled" in lib and "disabled" in lib


# ---------------------------------------------------------------- handover-refresh loop

def test_handover_refresh_loop_registered(foreman_dir):
    from collectors import loops
    from collectors import validate as V
    cat = {e["loop"]: e for e in loops.catalog(foreman_dir)}
    assert "handover-refresh" in cat and "foreman" in cat["handover-refresh"]["enabled_for"]
    assert (foreman_dir / "cadences" / "handover-refresh.yaml").is_file()
    assert (foreman_dir / "prompts" / "handover-refresh.md").is_file()
    assert V.validate(foreman_dir).ok()


# ---------------------------------------------------------------- intuitive-ux loop

def test_intuitive_ux_loop_registered(foreman_dir):
    from collectors import loops
    from collectors import validate as V
    cat = {e["loop"]: e for e in loops.catalog(foreman_dir)}
    assert "intuitive-ux" in cat and "foreman" in cat["intuitive-ux"]["enabled_for"]
    assert (foreman_dir / "cadences" / "intuitive-ux.yaml").is_file()
    assert (foreman_dir / "prompts" / "intuitive-ux.md").is_file()
    assert V.validate(foreman_dir).ok()


def test_ops_readiness_loop_registered_and_gates(foreman_dir):
    import yaml
    from collectors import loops, build_receipt
    from collectors import validate as V
    cat = {e["loop"]: e for e in loops.catalog(foreman_dir)}
    assert "ops-readiness" in cat and "foreman" in cat["ops-readiness"]["enabled_for"]
    assert (foreman_dir / "cadences" / "ops-readiness.yaml").is_file()
    assert (foreman_dir / "prompts" / "ops-readiness.md").is_file()
    assert V.validate(foreman_dir).ok()
    cad = yaml.safe_load((foreman_dir / "cadences" / "ops-readiness.yaml").read_text())
    v = lambda m: build_receipt.compute_verdict(cad, m)
    clear = {"deploy_ops_gaps": 0, "pii_masking_gap": 0, "loops_unrun": 0, "approval_gate_unverified": 0}
    assert v(clear) == "green"
    assert v({**clear, "deploy_ops_gaps": 5, "approval_gate_unverified": 1}) == "amber"  # host-bound only
    assert v({**clear, "pii_masking_gap": 1}) == "red"                                    # local item open
    assert v({**clear, "loops_unrun": 1}) == "red"


def test_intuitive_ux_verdict_gates_on_every_metric(foreman_dir):
    # the loop must not go green while any core-job failure / friction pile / low score stands
    import yaml
    from collectors import build_receipt
    cad = yaml.safe_load((foreman_dir / "cadences" / "intuitive-ux.yaml").read_text())
    v = lambda m: build_receipt.compute_verdict(cad, m)
    assert v({"task_completion_failures": 0, "friction_points": 0, "intuitiveness_score": 90}) == "green"
    assert v({"task_completion_failures": 1, "friction_points": 0, "intuitiveness_score": 90}) == "amber"  # a failure blocks green
    assert v({"task_completion_failures": 0, "friction_points": 6, "intuitiveness_score": 90}) == "amber"  # friction pile blocks green
    assert v({"task_completion_failures": 0, "friction_points": 0, "intuitiveness_score": 70}) == "amber"  # low score blocks green
    assert v({"task_completion_failures": 3, "friction_points": 20, "intuitiveness_score": 40}) == "red"


# ---------------------------------------------------------------- orientation legend

def test_orientation_legend_decodes_colours_and_terms(foreman_dir, state_dir):
    out = web.render(web._gather(foreman_dir, state_dir, None))
    legend = out.split('id="legend"')[1].split("</details>")[0]
    # colour language decoded
    assert "healthy" in legend and "needs attention soon" in legend and "act now" in legend
    assert "dot green" in legend and "dot amber" in legend and "dot red" in legend
    # the house terms a newcomer needs, in plain language
    for term in ("Loop", "Verdict", "Where it runs", "Queued", "Drift"):
        assert f"<dt>{term}</dt>" in legend
    # open by default for a first-timer, and it sits above the fold (before the first section)
    assert 'id="legend" open' in out
    assert out.index('id="legend"') < out.index("Since you were away")


def test_orientation_legend_present_in_read_only(foreman_dir, state_dir, index_path):
    db.open_index(index_path).close()
    data = web._gather(foreman_dir, state_dir, index_path)
    try:
        out = web.render(data, read_only=True)     # native <details>, no JS needed
    finally:
        if data["conn"]:
            data["conn"].close()
    assert 'id="legend"' in out and "How to read this board" in out

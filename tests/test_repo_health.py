"""Raw git/GitHub counts -> actionable signals, and .env key discovery."""

from collectors import repo_health, secrets


def test_signals_are_actionable():
    g = {"dirty_files": 100, "stale_branches": 1, "unmerged_agent_branches": 2, "behind": 3,
         "ahead": 1, "largest_blob_mb": 80, "force_pushes_7d": 0, "orphan_worktrees": 0}
    h = {"failing_checks": 1, "security_alerts": 0, "oldest_pr_days": 9, "dependabot_open": 0,
         "open_prs": 2, "issues_open": 42}
    sigs = repo_health.signals(g, h)
    texts = [t for _, t in sigs]
    assert any("100 uncommitted files — commit or stash" == t for t in texts)
    assert any("stale branch(es) — prune" in t for t in texts)
    assert any("failing check(s) — fix CI" in t for t in texts)
    assert any("git-lfs" in t for t in texts)          # 80 MB blob
    assert repo_health.level_of(sigs) == "red"          # failing check / big blob
    # red signals sort ahead of amber ahead of info
    levels = [lev for lev, _ in sigs]
    assert levels == sorted(levels, key=lambda lev: {"red": 0, "amber": 1, "info": 2}[lev])


def test_clean_repo():
    assert repo_health.signals({"dirty_files": 0}, None) == []
    assert repo_health.level_of([]) == "green"


def test_suggestions_synthesize_and_prioritize():
    # security alerts + dependabot fold into ONE "patch dependencies" action, not two chips
    g = {"dirty_files": 100, "stale_branches": 3, "unmerged_agent_branches": 2, "behind": 4,
         "ahead": 1, "largest_blob_mb": 0, "force_pushes_7d": 0, "orphan_worktrees": 0}
    h = {"failing_checks": 2, "security_alerts": 1, "dependabot_open": 3, "oldest_pr_days": 12,
         "open_prs": 5, "agent_prs": 2, "issues_open": 10}
    sug = repo_health.suggestions(g, h)
    texts = [s["text"] for s in sug]
    levels = [s["level"] for s in sug]
    # red first, then amber, then info — prioritized
    assert levels == sorted(levels, key=lambda l: {"red": 0, "amber": 1, "info": 2}[l])
    # CI is the lead red action
    assert sug[0]["level"] == "red" and "Fix CI" in sug[0]["text"]
    # one synthesized dependency action mentioning both alerts and dependabot
    dep = [t for t in texts if "Patch dependencies" in t]
    assert len(dep) == 1 and "dependabot" in dep[0]
    # working-tree clutter collapses into a single "Tidy the workspace" suggestion
    tidy = [t for t in texts if "Tidy the workspace" in t]
    assert len(tidy) == 1
    assert "uncommitted files" in tidy[0] and "stale branch" in tidy[0] and "agent branch" in tidy[0]
    # PR backlog surfaced by age
    assert any("PR backlog" in t and "12d" in t for t in texts)
    # sync suggestion mentions behind count
    assert any("Sync with upstream" in t and "4 commit" in t for t in texts)


def test_suggestions_dependabot_only_is_amber():
    # dependabot with no security alert is an amber merge nudge, not a red patch action
    sug = repo_health.suggestions(None, {"dependabot_open": 2, "failing_checks": 0,
                                         "security_alerts": 0, "open_prs": 2, "oldest_pr_days": 1})
    assert any(s["level"] == "amber" and "dependabot" in s["text"] for s in sug)
    assert not any(s["level"] == "red" for s in sug)


def test_suggestions_dependabot_survives_unrelated_reds():
    # paysvc case: failing CI + big blob (reds) but NO security alert -> the 25 dependabot
    # PRs must still surface, not get suppressed by the presence of other reds
    sug = repo_health.suggestions(
        {"largest_blob_mb": 103, "failing_checks": 0, "dirty_files": 0},
        {"failing_checks": 59, "security_alerts": 0, "dependabot_open": 25, "open_prs": 10,
         "oldest_pr_days": 170})
    assert any("25 dependabot" in s["text"] for s in sug)


def test_suggestions_single_stale_branch_is_not_a_chore():
    # 1-2 stale branches is routine and must NOT raise a "tidy" suggestion (avoids flooding
    # every repo with an amber nudge); 3+ does
    quiet = repo_health.suggestions({"stale_branches": 1, "dirty_files": 0, "ahead": 0}, None)
    assert quiet == [{"level": "info", "text": "Healthy — nothing needs attention."}]
    loud = repo_health.suggestions({"stale_branches": 3, "dirty_files": 0, "ahead": 0}, None)
    assert any("Tidy the workspace" in s["text"] and "3 stale" in s["text"] for s in loud)
    # an orphan worktree is real cruft -> triggers even with a lone stale branch, which then
    # rides along as detail
    orphan = repo_health.suggestions({"stale_branches": 1, "orphan_worktrees": 1, "dirty_files": 0}, None)
    tidy = [s for s in orphan if "Tidy" in s["text"]]
    assert tidy and "1 stale branch" in tidy[0]["text"] and "orphan" in tidy[0]["text"]


def test_suggestions_large_unpushed_backlog():
    # 16 commits ahead, none behind -> nudge to push (work not backed up)
    sug = repo_health.suggestions({"ahead": 16, "behind": 0, "dirty_files": 0, "stale_branches": 0}, None)
    assert any(s["level"] == "amber" and "Push your work" in s["text"] and "16" in s["text"]
               for s in sug)


def test_suggestions_healthy_and_nodata():
    assert repo_health.suggestions(None, None)[0]["text"].startswith("No git/GitHub data")
    healthy = repo_health.suggestions({"dirty_files": 0, "ahead": 0}, {"failing_checks": 0,
                                      "security_alerts": 0, "open_prs": 0})
    assert healthy == [{"level": "info", "text": "Healthy — nothing needs attention."}]
    ahead = repo_health.suggestions({"dirty_files": 0, "ahead": 2}, None)
    assert ahead[0]["level"] == "info" and "2 commit" in ahead[0]["text"]


def test_scan_env_files_names_only(tmp_path):
    (tmp_path / ".env").write_text("export FOO=abc\nBAR = 2\n# note\nBAZ=xyz\n")
    (tmp_path / ".env.local").write_text("QUX=topsecret\n")
    keys = secrets.scan_env_files(tmp_path)
    assert set(keys) == {"FOO", "BAR", "BAZ", "QUX"}
    assert ".env.local" in keys["QUX"]
    # unmanaged = present in .env but not declared
    assert secrets.unmanaged_env_keys(tmp_path, ["FOO"]) == ["BAR", "BAZ", "QUX"]
    # values are never captured
    assert "topsecret" not in "".join(keys) and "abc" not in "".join(keys)


def test_env_fingerprints(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-abcXYZ12345\n"
                                   "SHORT=ab\nQUOTED='val-tail99'\n")
    (tmp_path / ".env.example").write_text("ANTHROPIC_API_KEY=changeme-placeholder\n")
    fp = secrets.env_fingerprints(tmp_path)
    assert fp["ANTHROPIC_API_KEY"] == "…12345"     # last 5 chars of the real value
    assert fp["QUOTED"] == "…ail99"                 # surrounding quotes stripped
    assert fp["SHORT"] == "…ab"                      # short value not padded
    # the template (.env.example) value is ignored, not shown
    assert "changeme" not in "".join(fp.values()) and "placeholder" not in "".join(fp.values())


def test_env_parser_strips_comments_and_ignores_backups(tmp_path):
    (tmp_path / ".env").write_text(
        "KEY=abc12345 # inline comment\n"
        "QUOTED=\"p@ss # word\"\n"          # quoted value keeps the hash
        "URL=http://x/y#frag\n")            # no space before # -> part of the value
    (tmp_path / ".env.bak").write_text("STALE=oldkey\n")
    (tmp_path / ".env.backup-2025").write_text("STALE2=x\n")
    (tmp_path / ".env.example").write_text("TEMPLATE=y\n")

    keys = secrets.scan_env_files(tmp_path)
    assert "KEY" in keys and "QUOTED" in keys
    assert "STALE" not in keys and "STALE2" not in keys and "TEMPLATE" not in keys  # skipped

    fp = secrets.env_fingerprints(tmp_path)
    assert fp["KEY"] == "…12345"      # inline comment stripped, not "…mment"
    assert fp["QUOTED"] == "… word"    # quoted value keeps the # and space
    assert fp["URL"] == "…#frag"       # unquoted, no space before # -> kept


def test_level_of_suggestions_matches_the_body():
    # the repo dot/roll-up must agree with the suggestions shown in the card (intuitive-ux fix):
    # a repo with only amber advice rolls up amber, a red one red, a clean one green.
    amber = repo_health.suggestions({"dirty_files": 0, "stale_branches": 3, "ahead": 0},
                                    {"failing_checks": 0, "security_alerts": 0, "open_prs": 0})
    assert repo_health.level_of_suggestions(amber) == "amber"
    red = repo_health.suggestions(None, {"failing_checks": 2, "security_alerts": 0, "open_prs": 0})
    assert repo_health.level_of_suggestions(red) == "red"
    clean = repo_health.suggestions({"dirty_files": 0, "ahead": 0},
                                    {"failing_checks": 0, "security_alerts": 0, "open_prs": 0})
    assert repo_health.level_of_suggestions(clean) == "green"

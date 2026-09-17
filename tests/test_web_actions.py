"""The dashboard's POST action layer (web.apply_action) — every mutating route, in-process.

These exercise the handler body directly (no HTTP server), which the render tests never did:
the effect of each action on the registry / library / decision queue, plus its guardrails.
"""
import pytest
import yaml

from collectors import web, decisions as D, scheduler


def _reg(foreman_dir):
    return yaml.safe_load((foreman_dir / "registry.yaml").read_text())


def _project(foreman_dir, slug):
    return next(p for p in _reg(foreman_dir)["projects"] if p["slug"] == slug)


def _apply(action, form, foreman_dir, state_dir, index_path=None):
    web.apply_action(action, {k: [v] for k, v in form.items()},
                     foreman_dir=foreman_dir, state_dir=state_dir, index_path=index_path)


def test_autorun_toggle(foreman_dir, state_dir):
    _apply("autorun", {"project": "acmeapi", "value": "true"}, foreman_dir, state_dir)
    assert scheduler.autorun_for(_reg(foreman_dir), "acmeapi") is True
    _apply("autorun", {"project": "acmeapi", "value": "false"}, foreman_dir, state_dir)
    assert scheduler.autorun_for(_reg(foreman_dir), "acmeapi") is False


def test_loop_tier_and_schedule_and_window(foreman_dir, state_dir):
    # tier override lands in project.tiers
    _apply("loop-tier", {"project": "acmeapi", "loop": "docs-sync", "tier": "local"},
           foreman_dir, state_dir)
    assert _project(foreman_dir, "acmeapi")["tiers"]["docs-sync"] == "local"
    # a frequency preset needs auto-run; without it the action raises and nothing is written
    with pytest.raises(ValueError):
        _apply("loop-schedule", {"project": "acmeapi", "loop": "docs-sync", "preset": "daily"},
               foreman_dir, state_dir)
    _apply("autorun", {"project": "acmeapi", "value": "true"}, foreman_dir, state_dir)
    _apply("loop-schedule", {"project": "acmeapi", "loop": "docs-sync", "preset": "weekly"},
           foreman_dir, state_dir)
    assert _project(foreman_dir, "acmeapi")["schedules"]["docs-sync"] == "weekly"
    # the "every N days" number input maps to an every-<N>d preset (clamped to >=1)
    _apply("loop-schedule", {"project": "acmeapi", "loop": "docs-sync", "days": "4"},
           foreman_dir, state_dir)
    assert _project(foreman_dir, "acmeapi")["schedules"]["docs-sync"] == "every-4d"
    # time-of-day window: project-scoped and fleet default
    _apply("autorun-window", {"project": "acmeapi", "start_hour": "16", "span_hours": "1",
                              "start_minute": "20"}, foreman_dir, state_dir)
    assert scheduler.autorun_window(_reg(foreman_dir), "acmeapi") == (16, 1, 20)
    _apply("autorun-window", {"project": "", "start_hour": "7", "span_hours": "2",
                              "start_minute": ""}, foreman_dir, state_dir)
    assert _reg(foreman_dir)["defaults"]["autorun_window"]["start_hour"] == 7


def test_loop_enable_then_disable(foreman_dir, state_dir):
    _apply("loop-enable", {"project": "sentrygw", "loop": "security-review"}, foreman_dir, state_dir)
    assert "security-review" in _project(foreman_dir, "sentrygw")["cadences"]
    _apply("loop-disable", {"project": "sentrygw", "loop": "security-review"}, foreman_dir, state_dir)
    assert "security-review" not in _project(foreman_dir, "sentrygw")["cadences"]


def test_loop_enable_with_settings(foreman_dir, state_dir):
    # the rich add-loop form can set tier + frequency in the same POST (foreman is auto-run,
    # so the preset applies rather than being ignored)
    _apply("loop-enable", {"project": "foreman", "loop": "docs-sync",
                           "tier": "cloud", "preset": "weekly"}, foreman_dir, state_dir)
    p = _project(foreman_dir, "foreman")
    assert "docs-sync" in p["cadences"]
    assert p["tiers"]["docs-sync"] == "cloud"
    assert p["schedules"]["docs-sync"] == "weekly"


def test_loop_new_and_customize(foreman_dir, state_dir, monkeypatch):
    # loop-customize / loop-new both open an editor; stub it so the test doesn't spawn one
    opened = []
    monkeypatch.setattr(web, "_open_env_in_editor", lambda p: opened.append(p))
    _apply("loop-new", {"name": "my-check", "title": "My check", "metrics": "a, b"},
           foreman_dir, state_dir)
    assert (foreman_dir / "loops" / "my-check.yaml").is_file()
    _apply("loop-customize", {"loop": "arch-review"}, foreman_dir, state_dir)
    assert (foreman_dir / "loops" / "arch-review.yaml").is_file()
    assert opened and opened[-1].endswith("arch-review.yaml")


def test_loop_author_creates_loop_and_prompt(foreman_dir, state_dir):
    # in-dashboard author: no external editor, writes both the spec and the runnable prompt
    _apply("loop-author", {"name": "link-check", "title": "Link check",
                           "metrics": "broken_links, checked", "green": "broken_links == 0",
                           "amber": "broken_links < 5",
                           "prompt": "Crawl the docs and follow every link."},
           foreman_dir, state_dir)
    spec = yaml.safe_load((foreman_dir / "loops" / "link-check.yaml").read_text())
    assert spec["metrics"] == ["broken_links", "checked"]
    assert spec["green"] == "broken_links == 0" and spec["amber"] == "broken_links < 5"
    md = (foreman_dir / "prompts" / "link-check.md").read_text()
    assert "Crawl the docs and follow every link." in md
    assert "## Report — do this last" in md          # runnable reporting contract appended
    assert "`broken_links`" in md                     # metric names wired into the contract


def test_loop_author_edit_roundtrip_no_doubled_contract(foreman_dir, state_dir):
    from collectors import loops
    _apply("loop-author", {"name": "link-check", "title": "Link check",
                           "metrics": "broken_links", "green": "", "amber": "",
                           "prompt": "First pass instructions."}, foreman_dir, state_dir)
    # editing pre-fills from the loop's current state, showing the body only (not the contract)
    d = loops.editable(foreman_dir, "link-check")
    assert d["prompt"].strip() == "First pass instructions."
    assert "## Report" not in d["prompt"]
    # save an edit; the contract must appear exactly once, not be doubled
    _apply("loop-author", {"name": "link-check", "title": "Link check v2",
                           "metrics": "broken_links, warnings", "green": "", "amber": "",
                           "prompt": d["prompt"] + "\n\nSecond pass added."}, foreman_dir, state_dir)
    md = (foreman_dir / "prompts" / "link-check.md").read_text()
    assert md.count("## Report — do this last") == 1
    assert "Second pass added." in md
    spec = yaml.safe_load((foreman_dir / "loops" / "link-check.yaml").read_text())
    assert spec["title"] == "Link check v2"
    assert spec["metrics"] == ["broken_links", "warnings"]


def test_loop_author_rejects_bad_name(foreman_dir, state_dir):
    with pytest.raises(ValueError):
        _apply("loop-author", {"name": "Bad Name", "metrics": "a"}, foreman_dir, state_dir)
    with pytest.raises(ValueError):
        _apply("loop-author", {"name": "ok-name", "metrics": ""}, foreman_dir, state_dir)


def test_dispatch_enqueues_decision(foreman_dir, state_dir, index_path):
    from collectors import db
    db.open_index(index_path).close()
    _apply("dispatch", {"project": "acmeapi", "cadence": "docs-sync"},
           foreman_dir, state_dir, index_path)
    pending = D.load_pending(state_dir, "acmeapi")
    assert any(dec["kind"] == "dispatch_cadence" for _, dec in pending)


def test_dispatch_run_launches_and_clears(foreman_dir, state_dir, index_path, monkeypatch):
    from collectors import db, scheduler
    db.open_index(index_path).close()
    _, dec = D.enqueue(state_dir, "acmeapi", "dispatch_cadence", {"cadence": "docs-sync"}, "op")

    calls = {}

    def fake_launch(fd, sd, spool, conn, *, project, cadence, tier, host, now):
        calls.update(project=project, cadence=cadence, tier=tier)
        return {"project": project, "cadence": cadence, "launched": True}

    monkeypatch.setattr(scheduler, "_launch_run", fake_launch)      # don't spawn a real claude
    _apply("dispatch-run", {"id": dec["decision_id"]}, foreman_dir, state_dir, index_path)
    assert calls["project"] == "acmeapi" and calls["cadence"] == "docs-sync"
    assert not D.load_pending(state_dir, "acmeapi")              # cleared from the queue

    # a launch that the scheduler skips (no on-host worktree, locked) raises and stays queued
    _, dec2 = D.enqueue(state_dir, "acmeapi", "dispatch_cadence", {"cadence": "docs-sync"}, "op")
    monkeypatch.setattr(scheduler, "_launch_run", lambda *a, **k: {"skipped": "no worktree"})
    with pytest.raises(ValueError):
        _apply("dispatch-run", {"id": dec2["decision_id"]}, foreman_dir, state_dir, index_path)
    assert D.load_pending(state_dir, "acmeapi")

    # a non-dispatch id is rejected
    _, note = D.enqueue(state_dir, "acmeapi", "note", {"text": "x"}, "op")
    with pytest.raises(ValueError):
        _apply("dispatch-run", {"id": note["decision_id"]}, foreman_dir, state_dir, index_path)


def test_decision_dismiss(foreman_dir, state_dir):
    D.enqueue(state_dir, "acmeapi", "dispatch_cadence", {"cadence": "docs-sync"}, "op")
    did = D.load_pending(state_dir, "acmeapi")[0][1]["decision_id"]
    _apply("decision-dismiss", {"id": did}, foreman_dir, state_dir)
    assert not D.load_pending(state_dir, "acmeapi")


def test_env_edit_rejects_arbitrary_path(foreman_dir, state_dir):
    # traversal guard: a path that isn't a live .env under a project worktree must be refused
    with pytest.raises(ValueError):
        _apply("env-edit", {"path": "/etc/passwd"}, foreman_dir, state_dir)


def test_loop_edit_rejects_path_outside_instance(foreman_dir, state_dir):
    with pytest.raises(ValueError):
        _apply("loop-edit", {"path": "/etc/hosts"}, foreman_dir, state_dir)


def test_unknown_action_raises(foreman_dir, state_dir):
    with pytest.raises(ValueError):
        _apply("bogus-action", {}, foreman_dir, state_dir)

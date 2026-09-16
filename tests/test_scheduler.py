"""The scheduler fires cadences that are due."""

import datetime as dt

import pytest
import yaml

from collectors import scheduler, decisions as D
from collectors.validate import Cron


def test_cron_fires_between():
    c = Cron.parse("17 3 * * 1")  # Mondays 03:17
    assert c.fires_between(dt.datetime(2026, 9, 7, 3, 16), dt.datetime(2026, 9, 7, 3, 18))
    assert not c.fires_between(dt.datetime(2026, 9, 7, 4, 0), dt.datetime(2026, 9, 7, 5, 0))
    # capped lookback still catches a recent fire even from a long-idle scheduler
    assert c.fires_between(dt.datetime(2020, 1, 1, 0, 0), dt.datetime(2026, 9, 7, 3, 18))


def _mini_foreman(tmp_path):
    fm = tmp_path / "fm"
    (fm / "cadences").mkdir(parents=True)
    (fm / "prompts").mkdir()
    (fm / "prompts" / "tick-test.md").write_text("do the work; write the partial\n")
    (fm / "cadences" / "tick-test.yaml").write_text(
        "slug: tick-test\ntier: local\nschedule: \"17 3 * * 1\"\napplies_to: [p]\n"
        "allowed_hosts: [mbp]\nprompt_ref: prompts/tick-test.md\nmetrics: [m]\n"
        "verdict: {green: 'm == 0', amber: 'm < 5', red: otherwise}\n")
    # a cloud cadence the local scheduler must NOT fire
    (fm / "cadences" / "cloud-only.yaml").write_text(
        "slug: cloud-only\ntier: cloud\nschedule: \"17 3 * * 1\"\napplies_to: [p]\n"
        "prompt_ref: prompts/tick-test.md\nmetrics: [m]\n"
        "verdict: {green: 'm == 0', amber: 'm < 5', red: otherwise}\n")
    # canonical hand-written layout (dash + slug, 4-space content) -- what the editors target
    (fm / "registry.yaml").write_text(
        "version: 1\n"
        "hosts:\n  mbp:\n    os: darwin\n    role: interactive\n"
        "defaults:\n  marketplace: x\n  marketplace_pin: \"0\"\n"
        "  context_budget_tokens: 100\n  receipt_branch: state\n  spool_dir: /tmp\n"
        "projects:\n"
        "  - slug: p\n"
        "    repo: o/p\n"
        "    default_branch: main\n"
        f"    worktree:\n      mbp: {tmp_path}\n"
        "    tier_default: local\n"
        "    cadences: [tick-test, cloud-only]\n"
        "    escalation:\n      github_issues: false\n")
    return fm


def test_autorun_window_controls_time_of_day():
    # default window: hour in 08..11, jittered minute
    m, h, _, _ = scheduler._slot("p", "loop")
    assert 8 <= h <= 11 and m not in (0, 30)
    # pinned window (the 4:20 PM setting): exact 16:20
    reg = {"defaults": {"autorun_window": {"start_hour": 16, "start_minute": 20, "span_hours": 1}},
           "projects": [{"slug": "p"}]}
    assert scheduler.autorun_window(reg, "p") == (16, 1, 20)
    assert scheduler.preset_cron("daily", "p", "loop", scheduler.autorun_window(reg)) == "20 16 * * *"
    # a project-level window overrides the fleet default
    reg["projects"][0]["autorun_window"] = {"start_hour": 2, "span_hours": 3}
    start, span, minute = scheduler.autorun_window(reg, "p")
    assert start == 2 and span == 3 and minute is None
    _m, hh, _, _ = scheduler._slot("p", "loop", scheduler.autorun_window(reg, "p"))
    assert 2 <= hh <= 4                                   # spread across 02..04, jittered minute


def test_every_n_days_interval_schedule():
    import datetime as dt
    S = scheduler
    assert S.interval_days("every-3d") == 3
    assert S.interval_days("every-1d") == 1
    assert S.interval_days("weekly") is None and S.interval_days("daily") is None
    now = dt.datetime(2026, 9, 13, 15, 0)
    tok = "@every 3d 7:30"
    prev, nxt = S.previous_fire(tok, now), S.next_fire(tok, now)
    assert prev <= now < nxt
    assert nxt - prev == dt.timedelta(days=3)             # true 3-day spacing (no month reset)
    assert prev.hour == 7 and prev.minute == 30
    # the dispatcher still handles ordinary cron
    assert S.next_fire("27 9 * * 4", now) == dt.datetime(2026, 9, 17, 9, 27)


def test_effective_schedule_emits_every_token_and_setter_accepts_it(foreman_dir):
    S = scheduler
    S.set_project_autorun(foreman_dir, "acmeapi", True)
    S.set_loop_schedule(foreman_dir, "acmeapi", "quality-review", "every-2d")
    reg = __import__("yaml").safe_load((foreman_dir / "registry.yaml").read_text())
    assert S.loop_preset(reg, "acmeapi", "quality-review") == "every-2d"
    sched = S.effective_schedule(reg, "acmeapi", "quality-review", "23 5 * * 2")
    assert sched.startswith("@every 2d ")                 # interval token, not a cron
    # an unknown interval is rejected; a valid one round-trips
    import pytest
    with pytest.raises(ValueError):
        S.set_loop_schedule(foreman_dir, "acmeapi", "quality-review", "every-0")


def test_launchd_plist_command_parses():
    # the plist's inner command must parse with the real CLI: --host is a PARENT arg, so it has
    # to come before the `tick` subcommand. Regression guard for the crash-loop bug where the
    # installed agent errored "unrecognized arguments: --host".
    plist = scheduler.launchd_plist(foreman_dir="/fm", state_dir="/st", index_path="/i.db",
                                    spool="/sp", host="mbp", mode="auto", interval=300)
    assert "--host mbp tick" in plist and "tick --host" not in plist
    # extract the args after the module and confirm argparse accepts them
    args = plist.split("-m collectors.scheduler ", 1)[1].split("</string>", 1)[0].split()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host"); ap.add_argument("--foreman-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pt = sub.add_parser("tick"); pt.add_argument("--mode")
    ns = ap.parse_args(args)                      # raises SystemExit if the order is wrong
    assert ns.cmd == "tick" and ns.host == "mbp" and ns.mode == "auto"


def test_previous_fire_catch_up():
    c = Cron.parse("17 3 * * 1")  # Mondays 03:17
    mon = dt.datetime(2026, 9, 7, 3, 17)
    # exactly on the fire minute
    assert c.previous_fire(dt.datetime(2026, 9, 7, 3, 17)) == mon
    # three days later (Thu) the most recent fire is still that Monday -- caught, where the
    # 2-day fires_between cap would have missed it
    assert c.previous_fire(dt.datetime(2026, 9, 10, 12, 0)) == mon
    assert not c.fires_between(dt.datetime(2026, 9, 8, 12, 0),
                               dt.datetime(2026, 9, 10, 12, 0))  # old cap: silently skipped


def test_next_fire():
    c = Cron.parse("17 3 * * 1")  # Mondays 03:17
    # from mid-week Thursday, the next fire is the coming Monday
    assert c.next_fire(dt.datetime(2026, 9, 10, 12, 0)) == dt.datetime(2026, 9, 14, 3, 17)
    # strictly after: standing exactly on a fire minute returns the NEXT one, not this one
    assert c.next_fire(dt.datetime(2026, 9, 14, 3, 17)) == dt.datetime(2026, 9, 21, 3, 17)
    # a cadence sparser than the horizon returns None rather than scanning forever
    assert Cron.parse("17 3 1 1 *").next_fire(dt.datetime(2026, 2, 1, 0, 0), horizon_days=10) is None


def test_backfill_fires_most_recent_missed_only(tmp_path):
    """A daily cadence slept through for a week backfills exactly one run, not seven."""
    fm = _mini_foreman(tmp_path)
    # rewrite tick-test to a daily schedule
    (fm / "cadences" / "tick-test.yaml").write_text(
        "slug: tick-test\ntier: local\nschedule: \"17 3 * * *\"\napplies_to: [p]\n"
        "allowed_hosts: [mbp]\nprompt_ref: prompts/tick-test.md\nmetrics: [m]\n"
        "verdict: {green: 'm == 0', amber: 'm < 5', red: otherwise}\n")
    now = dt.datetime(2026, 9, 14, 10, 0)          # a week after the last tick
    since = dt.datetime(2026, 9, 7, 10, 0)         # scheduler was idle 7 days
    d = [x for x in scheduler.due(fm, "mbp", since=since, now=now) if x["cadence"] == "tick-test"]
    assert len(d) == 1                              # exactly one catch-up run, not seven
    assert d[0]["scheduled_for"] == "2026-09-14T03:17"   # the most recent occurrence


def test_due_skips_cloud_and_off_host(tmp_path):
    fm = _mini_foreman(tmp_path)
    now = dt.datetime(2026, 9, 7, 3, 18)      # Monday 03:18
    since = dt.datetime(2026, 9, 7, 3, 16)
    d = scheduler.due(fm, "mbp", since=since, now=now)
    kinds = {x["cadence"] for x in d}
    assert "tick-test" in kinds          # local, on this host, due
    assert "cloud-only" not in kinds     # cloud fired by the routine, not here
    # a host the cadence does not allow sees nothing
    assert scheduler.due(fm, "vps", since=since, now=now) == []


def test_effective_tier_override_runs_cloud_cadence_locally(tmp_path):
    """A per-project tiers override flips a shared cloud cadence onto this project's local
    scheduler, without touching the cadence file (so the fleet keeps it on cloud)."""
    reg = {"projects": [{"slug": "p", "tiers": {"cloud-only": "local"}}, {"slug": "q"}]}
    # override wins for p; q (no override) falls back to the cadence's own tier
    assert scheduler.effective_tier(reg, "p", "cloud-only", "cloud") == "local"
    assert scheduler.effective_tier(reg, "q", "cloud-only", "cloud") == "cloud"
    # end to end: with the override, due() now surfaces the otherwise-cloud cadence locally
    fm = _mini_foreman(tmp_path)
    text = (fm / "registry.yaml").read_text().replace(
        "    cadences: [tick-test, cloud-only]\n",
        "    tiers:\n      cloud-only: local\n    cadences: [tick-test, cloud-only]\n")
    (fm / "registry.yaml").write_text(text)
    now = dt.datetime(2026, 9, 7, 3, 18)
    since = dt.datetime(2026, 9, 7, 3, 16)
    kinds = {x["cadence"] for x in scheduler.due(fm, "mbp", since=since, now=now)}
    assert "cloud-only" in kinds


def test_set_loop_tier_roundtrips(tmp_path):
    fm = _mini_foreman(tmp_path)
    scheduler.set_loop_tier(fm, "p", "cloud-only", "local")
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert reg["projects"][0]["tiers"] == {"cloud-only": "local"}
    assert scheduler.effective_tier(reg, "p", "cloud-only", "cloud") == "local"
    # "default" clears the override and drops the now-empty tiers block
    scheduler.set_loop_tier(fm, "p", "cloud-only", "default")
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert "tiers" not in reg["projects"][0]
    assert "tiers:" not in (fm / "registry.yaml").read_text()
    with pytest.raises(ValueError):
        scheduler.set_loop_tier(fm, "p", "cloud-only", "bogus")


def test_set_autorun_window_project_and_default(tmp_path):
    fm = _mini_foreman(tmp_path)
    # per-project pinned window (the 4:20 PM case)
    scheduler.set_autorun_window(fm, "p", 16, 1, 20)
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert reg["projects"][0]["autorun_window"] == {"start_hour": 16, "span_hours": 1, "start_minute": 20}
    assert scheduler.autorun_window(reg, "p") == (16, 1, 20)
    # replacing it (no minute) rewrites the block cleanly rather than duplicating
    scheduler.set_autorun_window(fm, "p", 2, 3, None)
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert reg["projects"][0]["autorun_window"] == {"start_hour": 2, "span_hours": 3}
    assert (fm / "registry.yaml").read_text().count("autorun_window:") == 1
    # fleet default lands in the defaults block
    scheduler.set_autorun_window(fm, None, 7, 1, 30)
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert reg["defaults"]["autorun_window"] == {"start_hour": 7, "span_hours": 1, "start_minute": 30}


def test_tick_dispatch_enqueues(tmp_path):
    fm = _mini_foreman(tmp_path)
    state = tmp_path / "state"
    (state / "queue").mkdir(parents=True)
    now = dt.datetime(2026, 9, 7, 3, 17)     # the fire minute (no prior tick => 1-min window)
    out = scheduler.tick(foreman_dir=fm, state_dir=state, spool=tmp_path / "spool",
                         index_path=":memory:", host="mbp", mode="dispatch", now=now)
    fired = [f for f in out["fired"] if f["cadence"] == "tick-test"]
    assert fired and fired[0].get("dispatched")
    pending = D.load_pending(state, "p")
    assert any(dec["kind"] == "dispatch_cadence" for _, dec in pending)
    # last-tick persisted so the next tick won't re-fire the same minute
    assert scheduler._last_tick(tmp_path / "spool") is not None


def test_autorun_setting(tmp_path):
    fm = _mini_foreman(tmp_path)
    reg = yaml.safe_load((fm / "registry.yaml").read_text())
    assert scheduler.autorun_for(reg, "p") is False        # default: queue

    scheduler.set_project_autorun(fm, "p", True)
    reg2 = yaml.safe_load((fm / "registry.yaml").read_text())
    assert scheduler.autorun_for(reg2, "p") is True         # per-project override

    scheduler.set_project_autorun(fm, "p", False)           # replace, not duplicate
    reg3 = yaml.safe_load((fm / "registry.yaml").read_text())
    assert scheduler.autorun_for(reg3, "p") is False
    assert sum("autorun:" in ln for ln in (fm / "registry.yaml").read_text().splitlines()) == 1

    # defaults.autorun is the fallback when a project doesn't set its own
    reg3["defaults"]["autorun"] = True
    del reg3["projects"][0]["autorun"]
    assert scheduler.autorun_for(reg3, "p") is True


def test_tick_auto_honours_queue(tmp_path):
    fm = _mini_foreman(tmp_path)
    state = tmp_path / "state"
    (state / "queue").mkdir(parents=True)
    now = dt.datetime(2026, 9, 7, 3, 17)
    # autorun unset -> defaults to queue -> auto mode dispatches (does not launch claude)
    out = scheduler.tick(foreman_dir=fm, state_dir=state, spool=tmp_path / "spool",
                         index_path=":memory:", host="mbp", mode="auto", now=now)
    fired = [f for f in out["fired"] if f["cadence"] == "tick-test"]
    assert fired and fired[0].get("dispatched")


def test_tick_launch_mode_spawns(tmp_path, monkeypatch):
    """mode='launch' routes a due cadence through _launch_run -> _spawn (stubbed, no claude)."""
    from types import SimpleNamespace
    fm = _mini_foreman(tmp_path)
    spool = tmp_path / "spool"; spool.mkdir()
    state = tmp_path / "state"; (state / "queue").mkdir(parents=True); (state / "receipts").mkdir()
    calls = []
    monkeypatch.setattr(scheduler, "_spawn",
                        lambda argv, *, cwd, env: (calls.append(cwd), SimpleNamespace(pid=4321))[1])
    now = dt.datetime(2026, 9, 7, 3, 17)          # tick-test fires Mon 03:17
    out = scheduler.tick(foreman_dir=fm, state_dir=state, spool=spool, index_path=":memory:",
                         host="mbp", mode="launch", now=now)
    fired = [f for f in out["fired"] if f.get("cadence") == "tick-test"]
    assert fired and fired[0].get("launched")
    assert calls                                   # _spawn was invoked (no real run)


def test_launch_run_skips_absent_worktree(tmp_path):
    from collectors import db
    fm = _mini_foreman(tmp_path)
    (fm / "registry.yaml").write_text(
        (fm / "registry.yaml").read_text().replace(str(tmp_path), str(tmp_path / "gone")))
    res = scheduler._launch_run(fm, tmp_path / "state", tmp_path / "spool", db.open_index(":memory:"),
                                project="p", cadence="tick-test", tier="local", host="mbp",
                                now=dt.datetime(2026, 9, 7, 3, 17, tzinfo=dt.timezone.utc))
    assert res.get("skipped") == "no worktree"


def test_launch_run_skips_when_locked(tmp_path):
    from collectors import db, lock
    fm = _mini_foreman(tmp_path)
    conn = db.open_index(":memory:")
    now = dt.datetime(2026, 9, 7, 3, 17, tzinfo=dt.timezone.utc)
    assert lock.acquire(conn, "tick-test:p", "mbp", "held-run", timeout_minutes=40, now=now)
    res = scheduler._launch_run(fm, tmp_path / "state", tmp_path / "spool", conn,
                                project="p", cadence="tick-test", tier="local", host="mbp", now=now)
    assert res.get("skipped") == "locked"

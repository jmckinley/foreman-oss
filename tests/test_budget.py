"""Per-run budgets and the status=stopped receipt (BUILD.md M8.1)."""

import datetime as dt
import json
import os

from collectors import budget, brief, lock
from collectors.build_receipt import _validate

ISO = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------- pure core

def test_limits_for_reads_budget_block():
    cad = {"budget": {"wall_clock_minutes": 40, "max_cost_usd": 2.5}}
    lim = budget.limits_for(cad)
    assert lim.wall_clock_minutes == 40 and lim.max_cost_usd == 2.5
    assert lim.max_tokens is None and lim.any()
    assert lim.as_dict() == {"wall_clock_minutes": 40, "max_cost_usd": 2.5}


def test_limits_for_no_budget():
    assert budget.limits_for({}).any() is False
    assert budget.limits_for({"budget": {}}).any() is False


def test_exceeded_each_dimension():
    lim = budget.Limits(max_tokens=1000, max_cost_usd=5.0, max_iterations=10,
                        wall_clock_minutes=40)
    assert budget.exceeded(lim, budget.Usage(elapsed_minutes=41)).startswith("wall-clock")
    assert budget.exceeded(lim, budget.Usage(tokens=1000)).startswith("tokens")
    assert budget.exceeded(lim, budget.Usage(cost_usd=6.0)).startswith("cost")
    assert budget.exceeded(lim, budget.Usage(iterations=12)).startswith("iterations")
    # under every ceiling -> no stop
    assert budget.exceeded(lim, budget.Usage(elapsed_minutes=39, tokens=999,
                                             cost_usd=4.9, iterations=9)) is None


def test_exceeded_never_guesses_a_missing_number():
    # A dimension with a limit but no observed usage figure must not trip a stop.
    lim = budget.Limits(max_tokens=1000, wall_clock_minutes=40)
    assert budget.exceeded(lim, budget.Usage()) is None
    assert budget.exceeded(lim, budget.Usage(tokens=None, elapsed_minutes=None)) is None


def test_exceeded_at_the_boundary_is_a_stop():
    lim = budget.Limits(wall_clock_minutes=40)
    assert budget.exceeded(lim, budget.Usage(elapsed_minutes=40))  # >= is a hit
    assert budget.exceeded(lim, budget.Usage(elapsed_minutes=39.9)) is None


# ------------------------------------------------------------- launch registry

def _launch(started, *, pid, run_id="01J000000000000000000000AA", budget_=None,
            lock_key="tick-test:p"):
    return {"run_id": run_id, "pid": pid, "project": "p", "cadence": "tick-test",
            "tier": "local", "host": "mbp", "started": started, "lock_key": lock_key,
            "budget": budget_ or {"wall_clock_minutes": 40}}


def test_record_and_load_launches(spool_dir):
    assert budget.load_launches(spool_dir) == []
    budget.record_launch(spool_dir, _launch("2026-09-06T11:00:00Z", pid=os.getpid()))
    got = budget.load_launches(spool_dir)
    assert len(got) == 1 and got[0]["cadence"] == "tick-test"
    # recording the same run_id again replaces, never duplicates
    budget.record_launch(spool_dir, _launch("2026-09-06T11:00:00Z", pid=os.getpid()))
    assert len(budget.load_launches(spool_dir)) == 1


# --------------------------------------------------------------------- reaping

def test_reap_stops_over_budget_launch(spool_dir, state_dir, conn):
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
    # started 90 min ago, alive (this process), 40-min wall-clock budget -> must be stopped
    started = (now - dt.timedelta(minutes=90)).strftime(ISO)
    lock.acquire(conn, "tick-test:p", "mbp", _launch(started, pid=1)["run_id"],
                 timeout_minutes=40, now=now)
    budget.record_launch(spool_dir, _launch(started, pid=os.getpid()))

    killed = []
    stopped = budget.reap(spool_dir, state_dir, conn, now=now,
                          terminate=lambda pid: killed.append(pid))

    assert len(stopped) == 1 and stopped[0]["reason"].startswith("wall-clock")
    assert killed == [os.getpid()]                       # the runaway was signalled
    assert budget.load_launches(spool_dir) == []         # dropped from the registry
    assert lock.holder(conn, "tick-test:p") is None      # lock released -> re-runnable

    receipt = json.loads(open(stopped[0]["receipt"]).read())
    assert receipt["status"] == "stopped" and receipt["verdict"] == "amber"
    assert receipt["metrics"] == {}
    _validate(receipt)                                   # honours the contract


def test_reap_keeps_under_budget_launch(spool_dir, state_dir, conn):
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
    started = (now - dt.timedelta(minutes=5)).strftime(ISO)   # young run
    budget.record_launch(spool_dir, _launch(started, pid=os.getpid()))
    stopped = budget.reap(spool_dir, state_dir, conn, now=now,
                          terminate=lambda pid: None)
    assert stopped == []
    assert len(budget.load_launches(spool_dir)) == 1         # still watched


def test_reap_drops_finished_launch(spool_dir, state_dir, conn):
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
    started = (now - dt.timedelta(minutes=90)).strftime(ISO)
    # a pid that is not alive: the run finished; its own hook owns the receipt
    dead_pid = 2_000_000_000
    budget.record_launch(spool_dir, _launch(started, pid=dead_pid))
    stopped = budget.reap(spool_dir, state_dir, conn, now=now,
                          terminate=lambda pid: (_ for _ in ()).throw(AssertionError("must not kill")))
    assert stopped == []
    assert budget.load_launches(spool_dir) == []             # dropped, no receipt written


def test_stopped_receipt_renders_amber_in_brief(spool_dir, state_dir, conn):
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
    started = (now - dt.timedelta(minutes=90)).strftime(ISO)
    budget.record_launch(spool_dir, _launch(started, pid=os.getpid()))
    stopped = budget.reap(spool_dir, state_dir, conn, now=now, terminate=lambda pid: None)
    receipt = json.loads(open(stopped[0]["receipt"]).read())
    cell = brief.analyze_pair([receipt], {"schedule": "7 8 * * *"}, now)
    assert cell["effective"] == "amber"
    assert "budget" in cell["reason"].lower()

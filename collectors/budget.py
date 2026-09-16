"""Per-run budgets and the ``status: stopped`` receipt (BUILD.md M8.1, SPEC §6/§16).

A cadence may declare a ``budget``: ``max_tokens``, ``max_cost_usd``, ``max_iterations``,
``wall_clock_minutes``. When a launch-mode run breaches any of them it is terminated and gets
a ``status: stopped`` receipt -- deliberately distinct from ``timeout`` (a lock-expiry /
unresponsive run) and from the global quota guard (headroom across *all* work, §16.3). A
budget is a per-run ceiling; the quota guard is a fleet-wide floor.

Enforcement is tick-based, not a daemon. Foreman runs no long-lived process: the launchd
agent calls ``scheduler tick`` on an interval, so the same interval reaps in-flight launches.
``_launch_run`` records each headless run it starts (only when the cadence has a budget); the
next tick evaluates every recorded launch and stops the ones over budget.

Wall-clock is always observable from ``now - started``. Token / cost / iteration budgets need
a usage source, so the reaper takes an injected ``usage_fn(launch, now) -> Usage`` (default:
wall-clock only). A dimension with no observed number is *not* enforced -- Foreman never
guesses a usage figure, the same discipline as the quota fetch in ``runstate.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from collectors.build_receipt import write_receipt

_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Cadence budget keys -> Limits fields. Kept explicit so an unknown key in a cadence is caught
# by the schema (additionalProperties:false), never silently dropped here.
BUDGET_KEYS = ("max_tokens", "max_cost_usd", "max_iterations", "wall_clock_minutes")


@dataclass
class Limits:
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_iterations: int | None = None
    wall_clock_minutes: int | None = None

    def any(self) -> bool:
        return any(getattr(self, k) is not None for k in BUDGET_KEYS)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in BUDGET_KEYS if getattr(self, k) is not None}


@dataclass
class Usage:
    tokens: int | None = None
    cost_usd: float | None = None
    iterations: int | None = None
    elapsed_minutes: float | None = None


def limits_for(cadence: dict) -> Limits:
    """Extract a cadence's budget block into Limits (all-None if it declares no budget)."""
    b = cadence.get("budget") or {}
    return Limits(**{k: b[k] for k in BUDGET_KEYS if b.get(k) is not None})


def exceeded(limits: Limits, usage: Usage) -> str | None:
    """The first budget dimension breached, as a human reason, or None.

    A dimension is enforced only when both a limit *and* an observed usage number exist:
    a missing usage figure means "unknown", never "zero", so it can never trip a stop.
    Wall-clock is checked first because it is the one dimension always available.
    """
    if limits.wall_clock_minutes is not None and usage.elapsed_minutes is not None \
            and usage.elapsed_minutes >= limits.wall_clock_minutes:
        return f"wall-clock {usage.elapsed_minutes:.0f}m >= {limits.wall_clock_minutes}m"
    if limits.max_tokens is not None and usage.tokens is not None \
            and usage.tokens >= limits.max_tokens:
        return f"tokens {usage.tokens} >= {limits.max_tokens}"
    if limits.max_cost_usd is not None and usage.cost_usd is not None \
            and usage.cost_usd >= limits.max_cost_usd:
        return f"cost ${usage.cost_usd:.2f} >= ${limits.max_cost_usd:.2f}"
    if limits.max_iterations is not None and usage.iterations is not None \
            and usage.iterations >= limits.max_iterations:
        return f"iterations {usage.iterations} >= {limits.max_iterations}"
    return None


# ------------------------------------------------------- in-flight launch registry

def _launches_path(spool) -> Path:
    return Path(os.path.expanduser(str(spool))) / "launches.json"


def load_launches(spool) -> list[dict]:
    path = _launches_path(spool)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_launches(spool, launches: list[dict]) -> None:
    path = _launches_path(spool)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(launches, indent=2) + "\n")


def record_launch(spool, launch: dict) -> None:
    """Append one supervised launch to the registry. Only budgeted runs are recorded, so
    launches.json holds exactly the runs the reaper needs to watch."""
    launches = [lc for lc in load_launches(spool) if lc.get("run_id") != launch.get("run_id")]
    launches.append(launch)
    save_launches(spool, launches)


# ----------------------------------------------------------------------- reaping

def _parse(iso: str) -> dt.datetime:
    return dt.datetime.strptime(iso, _ISO).replace(tzinfo=dt.timezone.utc)


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _terminate(pid) -> None:
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        pass


def default_usage(launch: dict, now: dt.datetime) -> Usage:
    """Wall-clock only -- the one dimension observable without a telemetry source."""
    try:
        elapsed = (now - _parse(launch["started"])).total_seconds() / 60.0
    except (KeyError, ValueError):
        elapsed = None
    return Usage(elapsed_minutes=elapsed)


def write_stopped_receipt(state_dir, launch: dict, reason: str, now: dt.datetime) -> Path:
    """Write the ``status: stopped`` receipt for a run the budget guard terminated.

    verdict is amber, not red: the guard fired as configured, the run did not fail on its own
    terms -- but it produced no trustworthy verdict, so metrics are empty and the next_action
    points the operator at the budget. Idempotent per run_id via ``write_receipt``, so if the
    Stop/SessionEnd hook races us to a receipt, whichever lands first wins and the other no-ops.
    """
    ended = now.astimezone(dt.timezone.utc).strftime(_ISO)
    cadence, project = launch["cadence"], launch["project"]
    receipt = {
        "schema_version": 1,
        "run_id": launch["run_id"],
        "cadence": cadence,
        "project": project,
        "host": launch["host"],
        "tier": launch["tier"],
        "started": launch["started"],
        "ended": ended,
        "status": "stopped",
        "verdict": "amber",
        "cc_version": launch.get("cc_version", "unknown"),
        "metrics": {},
        "next_action": f"Budget stopped {cadence} on {project} ({reason}); raise the budget or fix the slow run."[:200],
        "notes": f"run stopped by budget guard: {reason}",
    }
    return write_receipt(Path(state_dir), receipt)


def _cleanup_worktree(lc: dict) -> None:
    """Remove a finished/stopped run's throwaway worktree, if it had one."""
    wt, repo = lc.get("worktree"), lc.get("repo")
    if wt and repo:
        from collectors import scheduler
        scheduler.remove_worktree(repo, wt)


def _maybe_request_approval(lc: dict, state_dir) -> str | None:
    """A finished writes.open_pr run that left commits on its branch is not pushed: enqueue an
    approve_push decision so a human approves the push/PR (the approval gate). Returns the new
    decision id or None. Branch refs survive worktree removal, so the push can happen later."""
    if not lc.get("open_pr"):
        return None
    branch, base, repo = lc.get("branch"), lc.get("base"), lc.get("repo")
    if not (branch and base and repo):
        return None
    from collectors import scheduler, decisions
    ahead = scheduler.branch_ahead(repo, base, branch)
    if ahead <= 0:
        return None
    try:
        _path, dec = decisions.enqueue(
            Path(state_dir), lc["project"], "approve_push",
            {"cadence": lc["cadence"], "branch": branch, "base": base, "ahead": ahead,
             "run_id": lc["run_id"]},
            actor="scheduler")
        return dec["decision_id"]
    except Exception:
        return None  # an approval-request failure must never break the reaper


def reap(spool, state_dir, conn, *, now: dt.datetime, usage_fn=default_usage,
         terminate=_terminate) -> list[dict]:
    """Evaluate every recorded launch; stop the ones over budget, clean up finished ones.
    Called at the top of a tick.

    A launch whose process has already exited is dropped -- its hook wrote the real receipt --
    and its throwaway worktree (if any) is removed. A launch over budget is terminated, gets a
    stopped receipt, its lock released so the pair is immediately re-runnable, and its worktree
    removed too. Survivors are kept for the next tick.
    """
    from collectors import lock

    launches = load_launches(spool)
    keep: list[dict] = []
    stopped: list[dict] = []
    for lc in launches:
        if not _pid_alive(lc.get("pid")):
            _maybe_request_approval(lc, state_dir)   # a writes.open_pr run awaits approval
            _cleanup_worktree(lc)     # finished on its own; the hook owns the receipt
            continue
        limits = Limits(**(lc.get("budget") or {}))
        reason = exceeded(limits, usage_fn(lc, now))
        if not reason:
            keep.append(lc)
            continue
        terminate(lc.get("pid"))
        path = write_stopped_receipt(state_dir, lc, reason, now)
        if lc.get("lock_key"):
            lock.release(conn, lc["lock_key"], lc["run_id"])
        _cleanup_worktree(lc)
        stopped.append({"run_id": lc["run_id"], "project": lc["project"],
                        "cadence": lc["cadence"], "reason": reason, "receipt": str(path)})
    save_launches(spool, keep)
    return stopped

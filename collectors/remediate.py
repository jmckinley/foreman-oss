"""Bounded auto-remediation -- cross from "tells you" to "fixes it, with receipts".

A supervisor that only reports is a nag board. The highest-value thing Foreman can do when a
loop goes red is dispatch a *scoped fix* -- but without ever breaking the invariant that
**Foreman never does the work**. It doesn't: this module only *enqueues a dispatch* for a fix
loop. The fix loop (a cadence prompt, worktree-isolated, ``writes.open_pr``) does the work, and
its PR waits on the existing ``approve_push`` human gate before anything is pushed. Three safety
rails, all opt-in:

1. **Off by default.** A project must opt in via ``auto_remediate`` in the registry, and the
   cadence must declare ``remediation.enabled``. Absent either, scan is a no-op for that pair.
2. **Attempt-capped.** Foreman dispatches at most ``remediation.max_attempts`` (default 2) fix
   loops per red streak. When the cap is hit it stops and files a ``note`` for the operator --
   it never loops forever throwing agents at a stuck red.
3. **Human-gated.** The fix loop opens a PR; pushing it is an ``approve_push`` decision, which
   is ``MANUAL_ONLY`` -- it is never auto-applied. Nothing reaches a remote without a click.

``plan()`` is the pure, testable core (what *would* be dispatched, and why). ``scan()`` executes
it against the decision queue. Both read receipts directly; no index required.

CLI::

    foreman remediate plan                # show what remediation would dispatch (reads only)
    foreman remediate scan                # enqueue the dispatches (honours caps + idempotency)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import yaml

from collectors import decisions
from collectors.brief import load_receipts, _cadence_meta

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_ATTEMPTS = 2


# ----------------------------------------------------------------- opt-in resolve

def resolve_auto_remediate(project: dict, defaults: dict):
    """A project's remediation opt-in: True (all capable loops), a set of loop slugs, or False.

    The project value overrides defaults; absent/false everywhere means off (the safe default).
    """
    val = project.get("auto_remediate")
    if val is None:
        val = defaults.get("auto_remediate")
    if val is True:
        return True
    if isinstance(val, list):
        return set(val)
    return False


def _opted_in(auto, cadence: str) -> bool:
    return auto is True or (isinstance(auto, set) and cadence in auto)


def remediation_of(cadence_meta: dict | None) -> dict | None:
    rem = (cadence_meta or {}).get("remediation")
    return rem if isinstance(rem, dict) and rem.get("enabled") else None


def fix_cadence_name(cadence: str, rem: dict) -> str:
    return rem.get("fix_cadence") or f"{cadence}-fix"


# ------------------------------------------------------------------- red + history

def _latest(history: list[dict]) -> dict | None:
    return history[-1] if history else None


def _is_red(receipt: dict | None) -> bool:
    if not receipt:
        return False
    return receipt.get("verdict") == "red" or receipt.get("status") in ("failed", "timeout")


def _last_green_ended(history: list[dict]) -> str | None:
    for r in reversed(history):
        if r.get("verdict") == "green":
            return r.get("ended")
    return None


# -------------------------------------------------------------- existing attempts

def _fix_decisions(state_dir: Path, project: str, parent: str) -> list[dict]:
    """All remediation dispatch decisions this project already holds for ``parent``.

    Reads the queue directly so idempotency survives an index wipe -- the decisions on the
    state branch are the record of what Foreman has already tried.
    """
    qdir = Path(state_dir) / "queue" / project
    out = []
    if not qdir.is_dir():
        return out
    for path in qdir.glob("*.json"):
        try:
            dec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        payload = dec.get("payload") or {}
        if dec.get("kind") == "dispatch_cadence" and payload.get("remediation") \
                and payload.get("parent") == parent:
            out.append(dec)
    out.sort(key=lambda d: d.get("decision_id", ""))
    return out


def _attempts_since(decs: list[dict], since_iso: str | None) -> list[dict]:
    """Non-cancelled fix decisions created after the last parent-green (the current streak)."""
    live = [d for d in decs if d.get("status") in ("pending", "applied")]
    if since_iso is None:
        return live
    return [d for d in live if (d.get("created") or "") >= since_iso]


# ------------------------------------------------------------------------- plan

def plan(state_dir: Path, foreman_dir: Path, now: dt.datetime | None = None) -> list[dict]:
    """What remediation would do right now. Pure: reads receipts + queue, writes nothing."""
    now = now or dt.datetime.now(dt.timezone.utc)
    registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
    defaults = registry.get("defaults") or {}
    cadences = _cadence_meta(Path(foreman_dir))
    groups = load_receipts(Path(state_dir))

    actions: list[dict] = []
    for proj in registry.get("projects") or []:
        slug = proj["slug"]
        auto = resolve_auto_remediate(proj, defaults)
        if not auto:
            continue
        for cadence in proj.get("cadences") or []:
            if not _opted_in(auto, cadence):
                continue
            rem = remediation_of(cadences.get(cadence))
            if rem is None:
                continue
            history = groups.get((slug, cadence), [])
            latest = _latest(history)
            if not _is_red(latest):
                continue
            fix = fix_cadence_name(cadence, rem)
            cap = int(rem.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
            since = _last_green_ended(history)
            prior = _fix_decisions(state_dir, slug, cadence)
            attempts = _attempts_since(prior, since)
            pending = [d for d in attempts if d.get("status") == "pending"]
            base = {"project": slug, "parent": cadence, "fix_cadence": fix,
                    "source_run": (latest or {}).get("run_id"), "attempt": len(attempts) + 1,
                    "max_attempts": cap}
            if pending:
                actions.append({**base, "action": "skip-pending",
                                "reason": f"a fix dispatch for {cadence} is already queued"})
            elif len(attempts) >= cap:
                actions.append({**base, "action": "capped",
                                "reason": f"{len(attempts)} fix attempt(s) since last green "
                                          f"reached max_attempts={cap}; escalating to operator"})
            else:
                actions.append({**base, "action": "dispatch",
                                "reason": f"{cadence} is red; dispatching {fix} "
                                          f"(attempt {len(attempts) + 1}/{cap})"})
    return actions


# ------------------------------------------------------------------------- scan

def scan(state_dir, foreman_dir, now: dt.datetime | None = None, *,
         actor: str = "foreman-remediate") -> dict:
    """Execute the plan: enqueue fix dispatches, and a note when a pair hits its cap."""
    state_dir = Path(state_dir)
    actions = plan(state_dir, Path(foreman_dir), now)
    dispatched, capped, skipped = [], [], []
    for a in actions:
        if a["action"] == "dispatch":
            payload = {"cadence": a["fix_cadence"], "remediation": True,
                       "parent": a["parent"], "attempt": a["attempt"],
                       "source_run": a["source_run"]}
            _path, dec = decisions.enqueue(state_dir, a["project"], "dispatch_cadence",
                                           payload, actor)
            dispatched.append({"project": a["project"], "fix_cadence": a["fix_cadence"],
                               "decision_id": dec["decision_id"], "attempt": a["attempt"]})
        elif a["action"] == "capped":
            # Stop and hand it back to the human -- idempotent via a stable note per streak.
            if not _capped_note_exists(state_dir, a["project"], a["parent"], a["source_run"]):
                decisions.enqueue(state_dir, a["project"], "note",
                                  {"text": f"Auto-remediation for {a['parent']} stopped after "
                                           f"{a['max_attempts']} attempts without clearing the "
                                           f"red. Needs you.",
                                   "remediation_capped": a["parent"],
                                   "source_run": a["source_run"]}, actor)
            capped.append({"project": a["project"], "parent": a["parent"]})
        else:
            skipped.append({"project": a["project"], "parent": a["parent"],
                            "reason": a["reason"]})
    return {"dispatched": dispatched, "capped": capped, "skipped": skipped}


def _capped_note_exists(state_dir: Path, project: str, parent: str, source_run) -> bool:
    qdir = Path(state_dir) / "queue" / project
    if not qdir.is_dir():
        return False
    for path in qdir.glob("*.json"):
        try:
            dec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        payload = dec.get("payload") or {}
        if dec.get("kind") == "note" and payload.get("remediation_capped") == parent \
                and payload.get("source_run") == source_run:
            return True
    return False


# ------------------------------------------------------------------------- CLI

def _render(actions: list[dict]) -> str:
    if not actions:
        return "REMEDIATION\n  (nothing to remediate — no opted-in loop is red)"
    lines = [f"REMEDIATION ({len(actions)})"]
    for a in actions:
        mark = {"dispatch": "→", "capped": "✋", "skip-pending": "·"}.get(a["action"], " ")
        lines.append(f"  {mark} {a['project']} / {a['parent']}: {a['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="remediate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "scan"):
        sp = sub.add_parser(name)
        sp.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
        sp.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
    args = ap.parse_args(argv)
    if not args.state_dir:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        return 2
    sd, fd = Path(os.path.expanduser(args.state_dir)), Path(os.path.expanduser(args.foreman_dir))
    if args.cmd == "plan":
        print(_render(plan(sd, fd)))
        return 0
    out = scan(sd, fd)
    print(f"dispatched {len(out['dispatched'])} fix loop(s), "
          f"{len(out['capped'])} capped, {len(out['skipped'])} already-queued")
    for d in out["dispatched"]:
        print(f"  → {d['project']} / {d['fix_cadence']} (attempt {d['attempt']}) "
              f"{d['decision_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

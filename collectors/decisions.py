"""Operator decision queue -- Foreman's forward channel.

Receipts flow app -> Foreman (a run reports a verdict). Decisions flow the other way:
the operator makes a call in the supervisor, and if the target project has no live
Claude Code session the decision is queued durably and drained the next time a session
for that project starts.

Design (SPEC.md section 18):

- A decision is a durable artifact, like a receipt. It lives on the ``state`` branch at
  ``queue/<project>/<decision_id>.json`` (``state/queue/...`` from the main checkout), so
  it survives deletion of the index -- the index is a cache (invariant 1).
- Two drain points, routed by ``requires_session``:
    * supervisor-side (``requires_session: false``): applied on ``mini`` during the normal
      drain -- ``set_pin`` edits ``registry.yaml``; ``close_escalation`` /
      ``pause_cadence`` / ``resume_cadence`` record a resolution.
    * app-side (``requires_session: true``): drained by ``hooks/session_start.sh`` at the
      next SessionStart for the project -- ``apply_setting`` edits the project's settings;
      ``dispatch_cadence`` / ``answer_question`` / ``note`` surface as session context.
- Applying is idempotent and ordered: decisions apply in ULID order, and a non-pending
  decision is skipped. Writing the terminal status back is the claim.

CLI::

    python -m collectors.decisions enqueue --project P --kind K --payload '{...}'
    python -m collectors.decisions drain-session      # app-side, from a project cwd
    python -m collectors.decisions drain-supervisor    # supervisor-side, on mini
    python -m collectors.decisions board               # QUEUED render for the brief
    python -m collectors.decisions gc                  # expire + prune
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import sys
import time
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Which side drains each kind, and the payload keys it requires.
APP_SIDE = {"dispatch_cadence", "answer_question", "apply_setting", "note"}
SUPERVISOR_SIDE = {"close_escalation", "set_pin", "pause_cadence", "resume_cadence",
                   "approve_push"}
# Kinds that must NOT auto-apply on the normal drain -- they wait for an explicit operator
# approval (the dashboard Apply / `decisions approve`). The approval gate for a writes.open_pr
# loop is the case: the branch is pushed and the PR opened only when a human says so.
MANUAL_ONLY = {"approve_push"}
REQUIRED_PAYLOAD = {
    "dispatch_cadence": ["cadence"],
    "answer_question": ["question_ref", "answer"],
    "apply_setting": ["path", "value"],
    "note": ["text"],
    "close_escalation": ["escalation", "reason"],
    "set_pin": ["scope", "marketplace_pin"],
    "pause_cadence": ["cadence"],
    "resume_cadence": ["cadence"],
    "approve_push": ["branch", "base"],
}


# --------------------------------------------------------------------------- ULID

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid(now_ms: int | None = None) -> str:
    """A ULID: 48-bit ms timestamp + 80 random bits, Crockford base32, lexically sortable."""
    ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    return _b32(ms, 10) + _b32(secrets.randbits(80), 16)


# ------------------------------------------------------------------------- schema

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO)


def _iso_from_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(_ISO)


def _schema() -> Draft202012Validator:
    with (ROOT / "schema" / "decision.schema.json").open() as fh:
        return Draft202012Validator(json.load(fh))


def validate_decision(dec: dict) -> list[str]:
    """Schema errors plus per-kind payload-key checks."""
    errs = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in _schema().iter_errors(dec)
    ]
    kind = dec.get("kind")
    if kind in REQUIRED_PAYLOAD:
        payload = dec.get("payload") or {}
        for key in REQUIRED_PAYLOAD[kind]:
            if key not in payload:
                errs.append(f"payload: kind {kind!r} requires key {key!r}")
    if kind in APP_SIDE and dec.get("requires_session") is False:
        errs.append(f"kind {kind!r} is app-side but requires_session is false")
    if kind in SUPERVISOR_SIDE and dec.get("requires_session") is True:
        errs.append(f"kind {kind!r} is supervisor-side but requires_session is true")
    return errs


# --------------------------------------------------------------------------- paths

def queue_dir(state_dir: Path, project: str) -> Path:
    return state_dir / "queue" / project


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _write(path: Path, dec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dec, indent=2, sort_keys=True) + "\n")


def load_pending(state_dir: Path, project: str | None = None,
                 requires_session: bool | None = None) -> list[tuple[Path, dict]]:
    root = state_dir / "queue"
    if not root.is_dir():
        return []
    projects = [project] if project else [p.name for p in root.iterdir() if p.is_dir()]
    out: list[tuple[Path, dict]] = []
    for proj in projects:
        for path in queue_dir(state_dir, proj).glob("*.json"):
            dec = _load(path)
            if dec.get("status") != "pending":
                continue
            if requires_session is not None and dec.get("requires_session") != requires_session:
                continue
            out.append((path, dec))
    # ULID order == chronological order.
    out.sort(key=lambda pd: pd[1]["decision_id"])
    return out


# ------------------------------------------------------------------------ enqueue

def enqueue(state_dir: Path, project: str, kind: str, payload: dict, actor: str,
            *, requires_session: bool | None = None, expires: str | None = None,
            supersedes: str | None = None, now_ms: int | None = None) -> tuple[Path, dict]:
    if requires_session is None:
        requires_session = kind in APP_SIDE
    dec = {
        "schema_version": 1,
        "decision_id": ulid(now_ms),
        "project": project,
        "created": _iso_from_ms(now_ms) if now_ms is not None else _now_iso(),
        "actor": actor,
        "kind": kind,
        "payload": payload,
        "requires_session": requires_session,
        "status": "pending",
    }
    if expires:
        dec["expires"] = expires
    if supersedes:
        dec["supersedes"] = supersedes
    errs = validate_decision(dec)
    if errs:
        raise ValueError("invalid decision: " + "; ".join(errs))
    path = queue_dir(state_dir, project) / f"{dec['decision_id']}.json"
    _write(path, dec)
    return path, dec


def _mark(path: Path, dec: dict, status: str, host: str, result: str,
          session_uuid: str | None) -> None:
    dec["status"] = status
    if status in ("applied", "failed"):
        dec["applied"] = {
            "at": _now_iso(),
            "host": host,
            "session_uuid": session_uuid,
            "result": result[:500],
        }
    _write(path, dec)


# ---------------------------------------------------------------- project resolve

def resolve_project(registry: dict, cwd: Path) -> tuple[str, Path] | None:
    """Map a working directory to (project slug, worktree root) via the registry."""
    cwd = cwd.resolve()
    best: tuple[int, str, Path] | None = None
    for proj in registry.get("projects", []):
        for wt in proj.get("worktree", {}).values():
            wtp = Path(os.path.expanduser(wt)).resolve()
            try:
                cwd.relative_to(wtp)
            except ValueError:
                continue
            depth = len(wtp.parts)
            if best is None or depth > best[0]:  # deepest (most specific) match wins
                best = (depth, proj["slug"], wtp)
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------- apply: app

def _apply_setting(project_dir: Path, payload: dict) -> str:
    which = payload.get("settings_file", "project")
    fname = "settings.local.json" if which == "local" else "settings.json"
    target = project_dir / ".claude" / fname
    data = json.loads(target.read_text()) if target.is_file() else {}
    keys = payload["path"].split(".")
    op = payload.get("op", "set")
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    leaf = keys[-1]
    if op == "append":
        cur = node.setdefault(leaf, [])
        if not isinstance(cur, list):
            raise ValueError(f"cannot append: {payload['path']} is not a list")
        add = payload["value"] if isinstance(payload["value"], list) else [payload["value"]]
        cur.extend(add)
    else:
        node[leaf] = payload["value"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return f"{op} {payload['path']} in .claude/{fname}"


def apply_app_side(dec: dict, project_dir: Path) -> tuple[str, str | None]:
    """Return (session-context line, side-effect result). Context is surfaced to the session."""
    kind, payload = dec["kind"], dec["payload"]
    if kind == "apply_setting":
        result = _apply_setting(project_dir, payload)
        return f"Foreman applied a setting: {result}.", result
    if kind == "dispatch_cadence":
        return (f"Foreman requests you run the '{payload['cadence']}' cadence now "
                f"(off-cycle dispatch)."), None
    if kind == "answer_question":
        return (f"Foreman operator answered {payload['question_ref']}: "
                f"{payload['answer']}"), None
    if kind == "note":
        return f"Foreman note: {payload['text']}", None
    raise ValueError(f"{kind!r} is not an app-side kind")


def drain_session(state_dir: Path, foreman_dir: Path, cwd: Path, *,
                  host: str, session_uuid: str | None) -> dict:
    """Apply pending app-side decisions for the project owning cwd.

    Returns a dict with the resolved project, applied ids, and the additionalContext
    string to hand back to the SessionStart hook.
    """
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    resolved = resolve_project(registry, cwd)
    if resolved is None:
        return {"project": None, "applied": [], "context": ""}
    project, project_dir = resolved
    lines, applied = [], []
    for path, dec in load_pending(state_dir, project, requires_session=True):
        try:
            ctx, result = apply_app_side(dec, project_dir)
            _mark(path, dec, "applied", host, result or ctx, session_uuid)
            lines.append(ctx)
            applied.append(dec["decision_id"])
        except Exception as exc:  # a bad decision must not block the session
            _mark(path, dec, "failed", host, f"{type(exc).__name__}: {exc}", session_uuid)
    context = ""
    if lines:
        context = ("Foreman delivered queued operator decisions for this project:\n- "
                   + "\n- ".join(lines))
    return {"project": project, "applied": applied, "context": context}


# ------------------------------------------------------------- apply: supervisor

def _edit_registry_pin(text: str, scope: str, pin: str) -> str:
    """Set marketplace_pin under defaults or a named project, preserving comments.

    pyyaml round-tripping would strip the file's comments, so this edits by line with
    indentation awareness. Raises if the target block is not found.
    """
    lines = text.splitlines(keepends=True)

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    if scope == "defaults":
        start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == "defaults:"), None)
        if start is None:
            raise ValueError("no defaults: block in registry")
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and indent_of(ln) == 0:
                break
            if ln.lstrip().startswith("marketplace_pin:"):
                ind = " " * indent_of(ln)
                lines[i] = f'{ind}marketplace_pin: "{pin}"\n'
                return "".join(lines)
        raise ValueError("no marketplace_pin under defaults")

    # project scope: find the list item whose slug matches, edit within its block. The
    # slug may share the list-item dash line ("- slug: x"), so strip a leading dash first.
    start = None
    item_indent = 0
    for i, ln in enumerate(lines):
        body = ln.lstrip()
        if body.startswith("- "):
            body = body[2:].lstrip()
        if body.startswith("slug:") and body.split("slug:", 1)[1].split("#")[0].strip() == scope:
            start = i
            item_indent = indent_of(ln)
            break
    if start is None:
        raise ValueError(f"no project with slug {scope!r}")
    # The block runs until the next list item at the same indent or a shallower key.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        ind = indent_of(ln)
        if ind < item_indent or (ind == item_indent and ln.lstrip().startswith("-")):
            end = i
            break
    for i in range(start, end):
        if lines[i].lstrip().startswith("marketplace_pin:"):
            ind = " " * indent_of(lines[i])
            lines[i] = f'{ind}marketplace_pin: "{pin}"\n'
            return "".join(lines)
    # absent: insert right after the slug line at the item's content indentation.
    ind = " " * (item_indent + 2)
    lines.insert(start + 1, f'{ind}marketplace_pin: "{pin}"\n')
    return "".join(lines)


def push_branch(repo: str, payload: dict) -> str:
    """Push a run's branch and open its PR -- the effect of approving an ``approve_push``.

    Isolated in a module function so a test can stub the remote side; the real path shells
    ``git push`` then ``gh pr create``. Raises on failure so the decision is marked failed,
    never silently 'applied'.
    """
    import subprocess
    branch, base = payload["branch"], payload["base"]
    title = payload.get("title") or f"Foreman: {payload.get('cadence', 'change')} ({branch})"
    push = subprocess.run(["git", "-C", repo, "push", "-u", "origin", branch],
                          capture_output=True, text=True, timeout=120)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()[:200]}")
    pr = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch,
                         "--title", title, "--body", "Opened by Foreman after operator approval."],
                        cwd=repo, capture_output=True, text=True, timeout=120)
    if pr.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {pr.stderr.strip()[:200]}")
    return f"pushed {branch}, opened PR: {pr.stdout.strip()}"


def _repo_for(foreman_dir: Path, project: str, host: str | None) -> str:
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    if proj is None:
        raise ValueError(f"unknown project {project!r}")
    wt = proj.get("worktree", {}) or {}
    target = wt.get(host) or (next(iter(wt.values())) if wt else None)
    if not target:
        raise ValueError(f"no worktree for {project!r}")
    return os.path.expanduser(target)


def apply_supervisor_side(dec: dict, foreman_dir: Path, *, host: str | None = None) -> str:
    kind, payload = dec["kind"], dec["payload"]
    if kind == "set_pin":
        reg = foreman_dir / "registry.yaml"
        reg.write_text(_edit_registry_pin(reg.read_text(), payload["scope"],
                                          payload["marketplace_pin"]))
        return f"set marketplace_pin {payload['scope']} -> {payload['marketplace_pin']}"
    if kind == "approve_push":
        # Operator approved a writes.open_pr run: push its branch and open the PR now.
        return push_branch(_repo_for(foreman_dir, dec["project"], host), payload)
    if kind == "close_escalation":
        # The escalation store (index) and GitHub issue close land in M3/M6. Record the
        # operator's resolution durably now; the drain that owns those surfaces honours it.
        return f"recorded close of {payload['escalation']}: {payload['reason']}"
    if kind in ("pause_cadence", "resume_cadence"):
        return f"recorded {kind} for {payload['cadence']}"
    raise ValueError(f"{kind!r} is not a supervisor-side kind")


def drain_supervisor(state_dir: Path, foreman_dir: Path, *, host: str) -> list[dict]:
    applied = []
    for path, dec in load_pending(state_dir, requires_session=False):
        if dec["kind"] in MANUAL_ONLY:
            continue  # waits for explicit operator approval, never auto-applied
        try:
            result = apply_supervisor_side(dec, foreman_dir, host=host)
            _mark(path, dec, "applied", host, result, None)
            applied.append({"decision_id": dec["decision_id"], "result": result})
        except Exception as exc:
            _mark(path, dec, "failed", host, f"{type(exc).__name__}: {exc}", None)
            applied.append({"decision_id": dec["decision_id"], "error": str(exc)})
    return applied


def _find_pending(state_dir: Path, decision_id: str):
    for path, dec in load_pending(state_dir):
        if dec["decision_id"] == decision_id:
            return path, dec
    return None, None


# Decisions with a real effect that doesn't need a live session. Everything else
# (note, answer_question, dispatch_cadence) is delivered INTO a session at SessionStart and
# cannot be force-applied when no window is open -- offering "Apply" for those would just
# mark them done and drop the message. approve_push is here because approving it IS the
# headless effect (push + PR), even though it is MANUAL_ONLY (never auto-drained).
HEADLESS_APPLY = {"apply_setting", "set_pin", "close_escalation", "pause_cadence",
                  "resume_cadence", "approve_push"}


def can_apply_headless(dec: dict) -> bool:
    return dec.get("kind") in HEADLESS_APPLY


def apply_decision(state_dir: Path, foreman_dir: Path, decision_id: str, *, host: str) -> dict:
    """Apply one pending decision now -- only kinds with a headless effect (HEADLESS_APPLY).
    Session-delivery kinds are refused, not faked: they need an open window (or a Dismiss)."""
    path, dec = _find_pending(Path(state_dir), decision_id)
    if path is None or dec is None:
        return {"ok": False, "reason": "not found or not pending"}
    if not can_apply_headless(dec):
        return {"ok": False, "reason": f"{dec['kind']} is delivered to a session; open the "
                                       "project or dismiss it"}
    try:
        if dec["requires_session"]:      # apply_setting: edits the worktree's settings on disk
            registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
            proj = next((p for p in registry.get("projects", []) if p["slug"] == dec["project"]), None)
            wt = (proj.get("worktree", {}) if proj else {})
            project_dir = Path(os.path.expanduser(wt.get(host) or next(iter(wt.values()), ".")))
            _ctx, result = apply_app_side(dec, project_dir)
            _mark(path, dec, "applied", host, result or "applied", None)
            return {"ok": True, "result": result}
        result = apply_supervisor_side(dec, Path(foreman_dir), host=host)  # set_pin/close/pause/approve_push
        _mark(path, dec, "applied", host, result, None)
        return {"ok": True, "result": result}
    except Exception as exc:
        _mark(path, dec, "failed", host, f"{type(exc).__name__}: {exc}", None)
        return {"ok": False, "reason": str(exc)}


def dismiss(state_dir: Path, decision_id: str) -> bool:
    """Cancel a pending decision (the dashboard 'Dismiss' button) -- terminal, not applied."""
    path, dec = _find_pending(Path(state_dir), decision_id)
    if path is None or dec is None:
        return False
    dec["status"] = "superseded"
    dec["notes"] = ((dec.get("notes") or "") + " dismissed by operator").strip()
    _write(path, dec)
    return True


# --------------------------------------------------------------------------- board

def _age(created: str, now: dt.datetime) -> str:
    then = dt.datetime.strptime(created, _ISO).replace(tzinfo=dt.timezone.utc)
    secs = max(0.0, (now - then).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def _scan(state_dir: Path, status: str) -> list[dict]:
    root = state_dir / "queue"
    if not root.is_dir():
        return []
    return [d for d in (_load(p) for p in root.glob("*/*.json")) if d.get("status") == status]


def board(state_dir: Path, now: dt.datetime | None = None, known: set | None = None) -> str:
    """Render the QUEUED section. ``known`` is the set of registered project slugs; queued
    decisions for slugs outside it (leftover queue dirs, renamed/removed projects) are folded
    into one warning line instead of appearing as unlabeled peers of real projects -- they are
    inert (drain resolves cwd->registered worktree, so an orphan slug can never fire)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    pending = load_pending(state_dir)
    expired = _scan(state_dir, "expired")
    if not pending and not expired:
        return "QUEUED\n  (no decisions waiting)"
    lines = [f"QUEUED ({len(pending)})"]
    by_project: dict[str, list[dict]] = {}
    for _, dec in pending:
        by_project.setdefault(dec["project"], []).append(dec)
    orphans: list[str] = []
    for project in sorted(by_project):
        if known is not None and project not in known:
            orphans.append(project)
            continue
        decs = sorted(by_project[project], key=lambda d: d["decision_id"])
        oldest = _age(decs[0]["created"], now)
        awaiting = sum(1 for d in decs if d["requires_session"])
        tag = f"  awaiting session ({awaiting})" if awaiting else ""
        kinds = ", ".join(sorted({d["kind"] for d in decs}))
        lines.append(f"  {project:<12} {len(decs)} pending, oldest {oldest}   {kinds}{tag}")
    if orphans:
        n = sum(len(by_project[p]) for p in orphans)
        lines.append(f"  ! {n} pending for {len(orphans)} unregistered "
                     f"project(s): {', '.join(orphans)} (inert — not in registry.yaml)")
    # Expired decisions stay visible until pruned, so intent is never silently dropped.
    if expired:
        exp_by: dict[str, int] = {}
        for dec in expired:
            exp_by[dec["project"]] = exp_by.get(dec["project"], 0) + 1
        for project in sorted(exp_by):
            mark = "" if known is None or project in known else " (unregistered)"
            lines.append(f"  {project:<12} {exp_by[project]} EXPIRED, never applied{mark}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- gc

def gc(state_dir: Path, *, now: dt.datetime | None = None, retention_days: int = 30) -> dict:
    """Expire pending decisions past their ``expires`` and prune old applied ones.

    Pending decisions are never silently deleted -- only marked expired, so a project that
    never drains its queue stays visible. Applied/expired/superseded decisions older than
    the retention window are removed (they are reflected in state and receipts by then).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    root = state_dir / "queue"
    expired, pruned = [], []
    if not root.is_dir():
        return {"expired": [], "pruned": []}
    for path in root.glob("*/*.json"):
        dec = _load(path)
        status = dec.get("status")
        if status == "pending" and dec.get("expires"):
            exp = dt.datetime.strptime(dec["expires"], _ISO).replace(tzinfo=dt.timezone.utc)
            if now >= exp:
                dec["status"] = "expired"
                _write(path, dec)
                expired.append(dec["decision_id"])
                continue
        if status in ("applied", "expired", "superseded", "failed"):
            stamp = dec.get("applied", {}).get("at") or dec.get("created")
            if not stamp:
                continue
            when = dt.datetime.strptime(stamp, _ISO).replace(tzinfo=dt.timezone.utc)
            if (now - when).days >= retention_days:
                path.unlink()
                pruned.append(dec["decision_id"])
    return {"expired": expired, "pruned": pruned}


# ---------------------------------------------------------------------------- CLI

def _state_dir(args) -> Path:
    val = args.state_dir or os.environ.get("FOREMAN_STATE_DIR")
    if not val:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        raise SystemExit(2)
    return Path(os.path.expanduser(val))


def _foreman_dir(args) -> Path:
    return Path(os.path.expanduser(getattr(args, "foreman_dir", None)
                                   or os.environ.get("FOREMAN_DIR") or str(ROOT)))


def main(argv: list[str] | None = None) -> int:
    # --state-dir / --foreman-dir accepted either before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir")
    common.add_argument("--foreman-dir")

    # Flags live on the subparsers (post-subcommand), the conventional argparse position;
    # FOREMAN_STATE_DIR / FOREMAN_DIR in the environment are the primary path for hooks.
    ap = argparse.ArgumentParser(prog="decisions")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enqueue", parents=[common])
    p.add_argument("--project", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--payload", required=True, help="JSON object")
    p.add_argument("--actor", default=os.environ.get("USER", "operator"))
    p.add_argument("--expires")
    p.add_argument("--supersedes")

    sub.add_parser("drain-session", parents=[common])
    sub.add_parser("drain-supervisor", parents=[common])
    sub.add_parser("board", parents=[common])
    sub.add_parser("gc", parents=[common])
    p = sub.add_parser("list", parents=[common])
    p.add_argument("--project")

    args = ap.parse_args(argv)

    if args.cmd == "enqueue":
        path, dec = enqueue(_state_dir(args), args.project, args.kind,
                            json.loads(args.payload), args.actor,
                            expires=args.expires, supersedes=args.supersedes)
        print(f"queued {dec['decision_id']} ({dec['kind']}) for {args.project}")
        print(str(path))
        return 0

    if args.cmd == "drain-session":
        host = os.environ.get("FOREMAN_HOST", "unknown")
        sid = os.environ.get("FOREMAN_SESSION_UUID")
        cwd = Path(os.environ.get("FOREMAN_CWD") or os.getcwd())
        out = drain_session(_state_dir(args), _foreman_dir(args), cwd,
                            host=host, session_uuid=sid)
        # SessionStart hook contract: additionalContext enters the session.
        if out["context"]:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": out["context"],
            }}))
        return 0

    if args.cmd == "drain-supervisor":
        host = os.environ.get("FOREMAN_HOST", "mini")
        applied = drain_supervisor(_state_dir(args), _foreman_dir(args), host=host)
        print(json.dumps(applied, indent=2))
        return 0

    if args.cmd == "board":
        print(board(_state_dir(args)))
        return 0

    if args.cmd == "gc":
        print(json.dumps(gc(_state_dir(args)), indent=2))
        return 0

    if args.cmd == "list":
        for _, dec in load_pending(_state_dir(args), args.project):
            print(f"{dec['decision_id']}  {dec['project']:<12} {dec['kind']:<16} "
                  f"{'app' if dec['requires_session'] else 'sup'}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""A scheduler tick -- fire the cadences that are due.

Foreman does not run a daemon; a launchd agent (macOS) calls ``scheduler tick`` on an
interval. The tick finds cadences whose cron fired since the last tick and fires them, two
ways:

- ``dispatch`` (default, safe): enqueue a dispatch_cadence decision. The run happens when a
  session next opens in the project (SessionStart drain) -- no unattended spend.
- ``launch``: run ``claude -p`` headless in the worktree now, so the cadence runs unattended
  and the SessionEnd hook writes a receipt. Real work, real cost -- opt in.

Both honour the quota guard (non-red cadences deferred under 15% headroom) and, in launch
mode, the §14 lock. The local scheduler only fires local/session-tier cadences on this host;
cloud cadences are fired by the cloud routine, not here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path

import yaml

from collectors import budget, db, decisions, dispatch, lock
from collectors.brief import _cadence_meta
from collectors.validate import Cron

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def autorun_for(registry: dict, project: str) -> bool:
    """Per-project scheduler behaviour: project.autorun > defaults.autorun > False (queue)."""
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    if proj is not None and "autorun" in proj:
        return bool(proj["autorun"])
    return bool((registry.get("defaults") or {}).get("autorun", False))


def set_project_autorun(foreman_dir: Path, project: str, value: bool) -> None:
    """Set a project's autorun flag in registry.yaml, comment-preserving (dashboard toggle)."""
    path = Path(foreman_dir) / "registry.yaml"
    lines = path.read_text().splitlines(keepends=True)
    val = "true" if value else "false"
    start, indent = None, 0
    for i, ln in enumerate(lines):
        body = ln.lstrip().lstrip("-").split("#")[0].strip()
        if body.startswith("slug:"):
            if body.split("slug:", 1)[1].strip() == project:
                start, indent = i, len(ln) - len(ln.lstrip(" "))
            elif start is not None:
                break  # reached the next project
        elif start is not None and ln.lstrip().startswith("autorun:"):
            ind = " " * (len(ln) - len(ln.lstrip(" ")))
            lines[i] = f"{ind}autorun: {val}\n"
            path.write_text("".join(lines))
            return
    if start is None:
        raise ValueError(f"unknown project {project!r}")
    lines.insert(start + 1, f"{' ' * (indent + 2)}autorun: {val}\n")
    path.write_text("".join(lines))


# --------------------------------------------------------------- frequency presets
#
# A loop's schedule lives in its cadence file and is shared by every project the cadence
# applies to. A project can override that frequency with a friendly preset in the registry
# (project.schedules: {loop: preset}); the override wins for this project only. Presets map
# to canonical crons whose minute/hour/day are derived deterministically from (project, loop)
# so two projects on the same preset don't stampede the same minute, and never land on
# :00/:30 (the one-shot jitter window, per CLAUDE.md).

FREQ_PRESETS = ("daily", "weekdays", "weekly", "monthly")


DEFAULT_WINDOW = (8, 4, None)   # (start_hour, span_hours, fixed_minute); default 08:xx–11:xx


def autorun_window(registry: dict, project: str | None = None) -> tuple[int, int, int | None]:
    """The time-of-day window automated loops fire within (§5 setting).

    `autorun_window: {start_hour, span_hours, start_minute}` in a project (most specific) or in
    `defaults` (fleet-wide) controls when preset-scheduled loops run:
      - {start_hour: 2, span_hours: 3}            -> spread across 02:xx–04:xx (jittered minute)
      - {start_hour: 16, start_minute: 20, span_hours: 1} -> exactly 16:20 (pinned; loops that
        share the window fire the same minute — the operator chose a precise time over spread)
    Falls back to DEFAULT_WINDOW."""
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    w = (proj or {}).get("autorun_window") or (registry.get("defaults") or {}).get("autorun_window")
    if isinstance(w, dict) and w.get("start_hour") is not None:
        start = int(w["start_hour"]) % 24
        span = max(1, min(24, int(w.get("span_hours", 1))))
        minute = None if w.get("start_minute") is None else int(w["start_minute"]) % 60
        return start, span, minute
    return DEFAULT_WINDOW


def _slot(project: str, loop: str, window: tuple = DEFAULT_WINDOW) -> tuple[int, int, int, int]:
    """Deterministic (minute, hour, day-of-week, day-of-month) for a (project, loop).

    The hour is spread across ``window``'s span so loops don't stampede one minute yet stay in
    the operator's chosen band; a pinned ``start_minute`` fires at an exact time instead."""
    start, span, fixed_min = window
    h = zlib.crc32(f"{project}:{loop}".encode())
    if fixed_min is not None:
        minute = fixed_min
    else:
        minute = h % 60
        if minute in (0, 30):
            minute = (minute + 7) % 60
    hour = (start + (h >> 8) % span) % 24
    dow = (h >> 16) % 5 + 1              # Mon-Fri (1-5)
    dom = (h >> 20) % 28 + 1             # 1-28, safe in every month
    return minute, hour, dow, dom


def preset_cron(preset: str, project: str, loop: str,
                window: tuple = DEFAULT_WINDOW) -> str:
    """Expand a frequency preset into a canonical, jitter-safe cron for this (project, loop)."""
    minute, hour, dow, dom = _slot(project, loop, window)
    table = {"daily": f"{minute} {hour} * * *",
             "weekdays": f"{minute} {hour} * * 1-5",
             "weekly": f"{minute} {hour} * * {dow}",
             "monthly": f"{minute} {hour} {dom} * *"}
    if preset not in table:
        raise ValueError(f"unknown frequency preset {preset!r}; use one of {FREQ_PRESETS}")
    return table[preset]


# --------------------------------------------------------------- "every N days" intervals
#
# Cron can't express "every N days" (day-of-month */N resets each month, giving an uneven gap
# at the boundary). So an interval preset -- "every-<N>d" -- is scheduled off a fixed anchor
# instead: it fires every N days at the (jitter-safe) slot time, on the same phase forever. The
# schedule is carried as a token "@every <N>d <hour>:<minute>" so previous_fire/next_fire below
# can recognise and expand it without a cron.

_ANCHOR = dt.datetime(2020, 1, 1)                    # fixed phase reference (local, naive)
_INTERVAL_PRESET = re.compile(r"^every-(\d+)d$")
_INTERVAL_TOKEN = re.compile(r"^@every (\d+)d (\d+):(\d+)$")


def interval_days(preset: str) -> int | None:
    """The N of an ``every-<N>d`` preset, or None if it's not an interval preset. (The legacy
    named ``daily`` preset stays a cron; ``every-1d`` is its interval equivalent.)"""
    m = _INTERVAL_PRESET.match(preset or "")
    return int(m.group(1)) if m else None


def _interval_bounds(n: int, hour: int, minute: int, now: dt.datetime):
    """(previous_fire, next_fire) for 'every n days at hour:minute', anchored at _ANCHOR."""
    base = _ANCHOR.replace(hour=hour, minute=minute)
    step = dt.timedelta(days=n)
    k = (now - base) // step                         # whole steps elapsed (floor)
    prev = base + k * step
    while prev > now:
        prev -= step
    while prev + step <= now:
        prev += step
    return prev, prev + step


def previous_fire(sched: str, now: dt.datetime, *, horizon_days: int = 45):
    """The most recent fire at/before ``now`` for a schedule that is either a cron or an
    ``@every`` interval token. Unifies the two so callers don't branch."""
    m = _INTERVAL_TOKEN.match(sched)
    if m:
        prev, _ = _interval_bounds(int(m.group(1)), int(m.group(2)), int(m.group(3)), now)
        return prev
    return Cron.parse(sched).previous_fire(now, horizon_days=horizon_days)


def next_fire(sched: str, now: dt.datetime, *, horizon_days: int = 45):
    """The next fire strictly after ``now`` for a cron or ``@every`` interval schedule."""
    m = _INTERVAL_TOKEN.match(sched)
    if m:
        _, nxt = _interval_bounds(int(m.group(1)), int(m.group(2)), int(m.group(3)), now)
        return nxt
    return Cron.parse(sched).next_fire(now, horizon_days=horizon_days)


def loop_preset(registry: dict, project: str, loop: str) -> str | None:
    """The frequency preset a project set for a loop, or None (use the cadence default)."""
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    val = ((proj or {}).get("schedules") or {}).get(loop)
    return val or None


def effective_tier(registry: dict, project: str, loop: str, default_tier: str | None) -> str | None:
    """The tier actually used for (project, loop): a per-project override in
    ``project.tiers: {loop: tier}`` if present, else the cadence's own tier.

    This lets one project run a shared cadence on the local scheduler while the rest of the fleet
    keeps it on the cloud routine -- without editing the shared cadence file (which would flip the
    tier for every project that declares it). Mirrors the per-project ``schedules`` override."""
    proj = next((p for p in registry.get("projects", []) if p["slug"] == project), None)
    override = ((proj or {}).get("tiers") or {}).get(loop)
    return override or default_tier


def effective_schedule(registry: dict, project: str, loop: str, default_cron: str) -> str:
    """The cron actually used for (project, loop): the per-project preset if set *and* the
    project is on auto-run, else the cadence's own schedule.

    Gating on auto-run enforces the mandate at the point of effect: a preset only fires a loop
    unattended, so if a project is flipped back to queue mode its presets go inert (falling
    back to the cadence default) rather than quietly enqueuing dispatches every day."""
    preset = loop_preset(registry, project, loop)
    if preset and autorun_for(registry, project):
        window = autorun_window(registry, project)
        n = interval_days(preset)
        if n is not None:                            # "every N days" -> an @every token, not cron
            minute, hour, _dow, _dom = _slot(project, loop, window)
            return f"@every {n}d {hour}:{minute}"
        return preset_cron(preset, project, loop, window)
    return default_cron


def _project_span(lines: list[str], project: str) -> tuple[int, int, int]:
    """(header_index, content_indent, end_index) for a project's block in registry.yaml lines.
    content_indent is the indent of the project's properties (2 past the ``- slug`` dash)."""
    p_start, p_indent, p_end = None, 0, len(lines)
    for i, ln in enumerate(lines):
        body = ln.lstrip().lstrip("-").split("#")[0].strip()
        if body.startswith("slug:"):
            s = body.split("slug:", 1)[1].strip()
            if p_start is None and s == project:
                p_start, p_indent = i, len(ln) - len(ln.lstrip(" "))
            elif p_start is not None:
                p_end = i
                break
    if p_start is None:
        raise ValueError(f"unknown project {project!r}")
    return p_start, p_indent + 2, p_end


def _set_project_map_entry(foreman_dir: Path, project: str, block: str, key: str,
                           value: str, *, remove: bool) -> None:
    """Insert / update / remove ``key: value`` inside a per-project ``block:`` map in
    registry.yaml, preserving comments. Removing the last entry drops the block header too.
    Shared by the ``schedules`` and ``tiers`` per-loop overrides."""
    path = Path(foreman_dir) / "registry.yaml"
    lines = path.read_text().splitlines(keepends=True)
    p_start, prop_indent, p_end = _project_span(lines, project)
    entry_indent = prop_indent + 2
    blk_i = None
    for i in range(p_start + 1, p_end):
        ln = lines[i]
        if ln.strip() and (len(ln) - len(ln.lstrip(" "))) == prop_indent \
                and ln.lstrip().startswith(f"{block}:"):
            blk_i = i
            break

    if blk_i is None:
        if remove:
            return
        lines.insert(p_start + 1, f"{' ' * prop_indent}{block}:\n")
        lines.insert(p_start + 2, f"{' ' * entry_indent}{key}: {value}\n")
        path.write_text("".join(lines))
        return

    block_end, entry_line, n_entries = p_end, None, 0
    for i in range(blk_i + 1, p_end):
        ln = lines[i]
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip(" "))
        if ind <= prop_indent:
            block_end = i
            break
        if ind == entry_indent:
            n_entries += 1
            if ln.lstrip().split(":", 1)[0].strip() == key:
                entry_line = i

    if remove:
        if entry_line is not None:
            del lines[entry_line]
            if n_entries <= 1:            # block is now empty -> drop the header too
                del lines[blk_i]
    elif entry_line is not None:
        lines[entry_line] = f"{' ' * entry_indent}{key}: {value}\n"
    else:
        lines.insert(block_end, f"{' ' * entry_indent}{key}: {value}\n")
    path.write_text("".join(lines))


def set_loop_schedule(foreman_dir: Path, project: str, loop: str, preset: str) -> None:
    """Set (or clear) a project's per-loop frequency preset in registry.yaml, preserving
    comments. A falsy preset or ``"default"`` removes the override so the loop reverts to the
    cadence's own schedule."""
    remove = not preset or preset == "default"
    if not remove and preset not in FREQ_PRESETS and interval_days(preset) is None:
        raise ValueError(f"unknown frequency preset {preset!r}; use one of {FREQ_PRESETS} "
                         "or 'every-<N>d'")
    registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
    if not any(p["slug"] == project for p in registry.get("projects", [])):
        raise ValueError(f"unknown project {project!r}")
    # A preset means "fire this loop unattended on a cadence", which only happens when the
    # project is on auto-run. In queue mode nothing fires by itself, so a preset would be a
    # lie -- require auto-run to set one (clearing is always allowed).
    if not remove and not autorun_for(registry, project):
        raise ValueError(
            f"{project!r} is in queue mode; enable auto-run before scheduling a loop preset")
    _set_project_map_entry(foreman_dir, project, "schedules", loop, preset, remove=remove)


TIER_VALUES = ("cloud", "local", "session")


def set_loop_tier(foreman_dir: Path, project: str, loop: str, tier: str) -> None:
    """Set (or clear) a project's per-loop tier override in registry.yaml (``project.tiers``),
    preserving comments. A falsy tier or ``"default"`` removes the override so the loop reverts
    to the cadence's own tier. See :func:`effective_tier`."""
    remove = not tier or tier == "default"
    if not remove and tier not in TIER_VALUES:
        raise ValueError(f"unknown tier {tier!r}; use one of {TIER_VALUES}")
    registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
    if not any(p["slug"] == project for p in registry.get("projects", [])):
        raise ValueError(f"unknown project {project!r}")
    _set_project_map_entry(foreman_dir, project, "tiers", loop, tier, remove=remove)


def set_autorun_window(foreman_dir: Path, project: str | None,
                       start_hour: int, span_hours: int, start_minute: int | None = None) -> None:
    """Set the time-of-day window automated loops fire within, for one project or (when
    ``project`` is None) the fleet ``defaults``. Comment-preserving: replaces any existing
    ``autorun_window`` block in scope, else inserts a fresh one. See :func:`autorun_window`."""
    start_hour = int(start_hour) % 24
    span_hours = max(1, min(24, int(span_hours)))
    path = Path(foreman_dir) / "registry.yaml"
    lines = path.read_text().splitlines(keepends=True)

    if project is None:
        scope_start = next((i for i, ln in enumerate(lines)
                            if (len(ln) - len(ln.lstrip(" "))) == 0
                            and ln.lstrip().startswith("defaults:")), None)
        if scope_start is None:
            raise ValueError("no defaults block in registry.yaml")
        content_indent = 2
        scope_end = next((j for j in range(scope_start + 1, len(lines))
                         if lines[j].strip() and (len(lines[j]) - len(lines[j].lstrip(" "))) == 0),
                         len(lines))
    else:
        scope_start, content_indent, scope_end = _project_span(lines, project)

    win_i = next((i for i in range(scope_start + 1, scope_end)
                 if lines[i].strip()
                 and (len(lines[i]) - len(lines[i].lstrip(" "))) == content_indent
                 and lines[i].lstrip().startswith("autorun_window:")), None)
    if win_i is not None:
        j = win_i + 1
        while j < scope_end and (not lines[j].strip()
                                 or (len(lines[j]) - len(lines[j].lstrip(" "))) > content_indent):
            j += 1
        del lines[win_i:j]
        insert_at = win_i
    else:
        insert_at = scope_start + 1

    ci, eci = " " * content_indent, " " * (content_indent + 2)
    block = [f"{ci}autorun_window:\n", f"{eci}start_hour: {start_hour}\n",
             f"{eci}span_hours: {span_hours}\n"]
    if start_minute is not None:
        block.append(f"{eci}start_minute: {int(start_minute) % 60}\n")
    lines[insert_at:insert_at] = block
    path.write_text("".join(lines))


def _state_file(spool: Path) -> Path:
    return Path(os.path.expanduser(spool)) / "scheduler.state.json"


def _last_tick(spool: Path) -> dt.datetime | None:
    f = _state_file(spool)
    if not f.is_file():
        return None
    try:
        return dt.datetime.fromisoformat(json.loads(f.read_text())["last_tick"])
    except (OSError, ValueError, KeyError):
        return None


def _save_tick(spool: Path, when: dt.datetime) -> None:
    f = _state_file(spool)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"last_tick": when.isoformat()}))


def due(foreman_dir: Path, host: str, *, since: dt.datetime, now: dt.datetime) -> list[dict]:
    """Local/session cadences on this host whose cron fired in (since, now]."""
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    cadences = _cadence_meta(foreman_dir)
    out = []
    for p in registry.get("projects", []):
        for cadence in p.get("cadences", []):
            cad = cadences.get(cadence)
            if not cad:
                continue
            tier = effective_tier(registry, p["slug"], cadence, cad.get("tier"))
            if tier == "cloud":
                continue  # fired by the cloud routine, not the local scheduler
            allowed = cad.get("allowed_hosts")
            if allowed and host not in allowed:
                continue
            sched = effective_schedule(registry, p["slug"], cadence, cad["schedule"])
            try:
                # Catch-up: fire the most recent scheduled occurrence that falls after the
                # last tick, and only that one. A laptop asleep across several occurrences
                # backfills a single run (the latest), never a stampede of missed ones.
                # Handles both cron and "every N days" (@every) schedules.
                last = previous_fire(sched, now)
            except ValueError:
                continue
            if last is not None and last > since:
                out.append({"project": p["slug"], "cadence": cadence, "tier": tier,
                            "scheduled_for": last.strftime("%Y-%m-%dT%H:%M")})
    return out


def _spawn(argv, *, cwd, env):
    """Launch the headless run, detached into its own session. A thin seam over Popen so a test
    can stub the spawn without disturbing the git subprocess.run calls that set up the worktree.

    ``start_new_session=True`` (setsid) is essential under launchd: the scheduler tick is the
    launchd job's main process, and when it exits right after spawning, launchd tears down the
    job's process group -- which would kill this fire-and-forget child before it does anything
    (the empty per-run log symptom). A new session detaches the run so it survives the tick."""
    return subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def branch_ahead(repo: str, base: str, branch: str) -> int:
    """Commits on ``branch`` not on ``base``, or 0 if the branch is missing / git errors.
    How the reaper decides an isolated run actually produced work worth approving."""
    try:
        p = subprocess.run(["git", "-C", repo, "rev-list", "--count", f"{base}..{branch}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0
    if p.returncode != 0:
        return 0
    try:
        return int(p.stdout.strip())
    except ValueError:
        return 0


def _add_worktree(repo: str, dest: Path) -> bool:
    """Create a detached throwaway worktree at ``dest`` from ``repo``'s HEAD. False on any git
    error (the caller then falls back to an in-place run rather than skipping the cadence)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", str(dest)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def remove_worktree(repo: str, dest) -> None:
    """Remove a throwaway worktree (best-effort). Committed branches live in the shared object
    store and survive this; only the uncommitted working copy is discarded, which is the whole
    point of isolation -- an unattended run never dirties the project's main tree."""
    dest = str(dest)
    try:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", dest],
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass
    if os.path.exists(dest):
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
    try:
        subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _launch_run(foreman_dir: Path, state_dir: Path, spool: Path, conn, *,
                project: str, cadence: str, tier: str, host: str, now: dt.datetime) -> dict:
    cad = yaml.safe_load((foreman_dir / "cadences" / f"{cadence}.yaml").read_text())
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    wt = next((p.get("worktree", {}).get(host) for p in registry["projects"]
               if p["slug"] == project), None)
    if not wt or not Path(os.path.expanduser(wt)).exists():
        return {"project": project, "cadence": cadence, "skipped": "no worktree"}
    base_wt = os.path.expanduser(wt)
    run_id = decisions.ulid()
    key = (cad.get("lock_key") or f"{cadence}:{{project}}").replace("{project}", project)
    if not lock.acquire(conn, key, host, run_id, timeout_minutes=cad.get("timeout_minutes", 40),
                        now=now.astimezone(dt.timezone.utc)):
        return {"project": project, "cadence": cadence, "skipped": "locked"}

    # A cadence that writes runs in a throwaway git worktree so an unattended run never dirties
    # the project's main tree; committed work survives (shared object store), the working copy
    # is reaped when the run ends. A read-only cadence runs in place. If the worktree can't be
    # made (e.g. not a git repo), fall back to in-place rather than skip the run.
    writes = cad.get("writes") or {}
    isolate = bool(writes)
    run_cwd, worktree_dir = base_wt, None
    if isolate:
        cand = Path(os.path.expanduser(str(spool))) / "worktrees" / f"{cadence}-{run_id}"
        if _add_worktree(base_wt, cand):
            run_cwd, worktree_dir = str(cand), cand

    prompt = (foreman_dir / cad["prompt_ref"]).read_text()
    started = now.astimezone(dt.timezone.utc).strftime(_ISO)
    # launchd hands agents a minimal environment: no interactive PATH, no session keychain. Give
    # the run a sane PATH so `claude`/`git`/`python` resolve, and pass ANTHROPIC_API_KEY through
    # when present so `claude -p` can authenticate headlessly (OAuth creds in ~/.claude also work
    # via HOME, which launchd sets). Without this a launchd-spawned run dies before doing anything.
    path = os.environ.get("PATH", "")
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if d not in path.split(":"):
            path = f"{d}:{path}"
    env = {**os.environ, "PATH": path, "FOREMAN_RUN_ID": run_id, "FOREMAN_CADENCE": cadence,
           "FOREMAN_PROJECT": project, "FOREMAN_TIER": tier, "FOREMAN_HOST": host,
           "FOREMAN_STARTED": started,
           "FOREMAN_SPOOL": str(spool), "FOREMAN_STATE_DIR": str(state_dir),
           "FOREMAN_DIR": str(foreman_dir), "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    # Headless run, then build the receipt unconditionally. A launched `claude -p` (print mode)
    # does not reliably fire SessionEnd/Stop hooks, so the scheduler owns receipt emission for
    # runs it spawns -- build_receipt is idempotent per run_id, so a hook that DOES fire (or a
    # user session on the session tier) is harmless. `;` (not `&&`) so a receipt is written even
    # if claude exits non-zero -- a failed run reports red rather than vanishing (red by absence).
    # All output goes to a per-run log so an unattended failure is diagnosable, not swallowed.
    import shlex
    log = Path(os.path.expanduser(str(spool))) / f"launch-{run_id}.log"
    timeout_s = int(cad.get("timeout_minutes", 40)) * 60
    py = shlex.quote(sys.executable)
    # Log the environment + bound claude with a hard timeout (perl alarm; macOS has no
    # `timeout`) so an unattended run that stalls -- e.g. a launchd keychain/auth prompt with no
    # TTY -- can't hang forever and always falls through to build_receipt. Everything is captured
    # to the per-run log so a failure is diagnosable, never silent.
    diag = ('echo "launch $(date -u +%Y-%m-%dT%H:%M:%SZ) whoami=$(whoami) tty=$(tty)"; '
            'echo "PATH=$PATH"; command -v claude || echo "claude: NOT FOUND on PATH"')
    claude_run = (f"perl -e 'alarm shift @ARGV; exec @ARGV' {timeout_s} "
                  f"claude -p {shlex.quote(prompt)}; echo \"[claude rc=$?]\"")
    inner = (f"{{ {diag}; {claude_run}; {py} -m collectors.build_receipt; }} "
             f"> {shlex.quote(str(log))} 2>&1")
    proc = _spawn(["bash", "-lc", inner], cwd=run_cwd, env=env)
    # Register the run for supervision when it has a budget (reaped over its ceiling) or a
    # worktree (reaped for cleanup when the process ends). Everything else runs as before.
    limits = budget.limits_for(cad)
    if limits.any() or worktree_dir is not None:
        default_branch = next((p.get("default_branch") for p in registry["projects"]
                               if p["slug"] == project), None)
        budget.record_launch(spool, {
            "run_id": run_id, "pid": proc.pid, "project": project, "cadence": cadence,
            "tier": tier, "host": host, "started": started, "lock_key": key,
            "budget": limits.as_dict(),
            "worktree": str(worktree_dir) if worktree_dir else None, "repo": base_wt,
            # For the approval gate: a writes.open_pr run's branch is offered for approval
            # (never auto-pushed) once the run leaves commits on it.
            "open_pr": bool(writes.get("open_pr")), "branch": writes.get("branch"),
            "base": default_branch})
    return {"project": project, "cadence": cadence, "launched": run_id,
            "budgeted": limits.any(), "isolated": worktree_dir is not None}


def tick(*, foreman_dir: Path, state_dir: Path, spool: Path, index_path: str, host: str,
         mode: str = "auto", now: dt.datetime | None = None) -> dict:
    """Fire due cadences. mode 'auto' honours each project's autorun setting; 'dispatch' or
    'launch' forces one behaviour for everything (a global override / safety cap)."""
    now = now or dt.datetime.now()
    since = _last_tick(spool) or (now - dt.timedelta(minutes=1))
    conn = db.open_index(index_path)
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    # Reap first: stop any in-flight launch that has breached its budget before firing new
    # work, so a runaway run cannot starve this tick's cadences of headroom.
    stopped = budget.reap(spool, state_dir, conn,
                          now=now.astimezone(dt.timezone.utc))
    fired = []
    for d in due(foreman_dir, host, since=since, now=now):
        m = mode
        if mode == "auto":
            m = "launch" if autorun_for(registry, d["project"]) else "dispatch"
        if m == "launch":
            fired.append(_launch_run(foreman_dir, state_dir, spool, conn,
                                     project=d["project"], cadence=d["cadence"],
                                     tier=d["tier"], host=host, now=now))
        else:
            try:
                res = dispatch.dispatch(conn, registry, state_dir, d["project"], d["cadence"])
                fired.append({**d, "mode": "dispatch", **res})
            except Exception as exc:
                fired.append({**d, "error": str(exc)})
    _save_tick(spool, now)
    return {"mode": mode, "since": since.isoformat(), "now": now.isoformat(),
            "fired": fired, "stopped": stopped}


# ------------------------------------------------------------------------- launchd

LABEL = "com.foreman.scheduler"


def launchd_plist(*, foreman_dir, state_dir, index_path, spool, host, mode, interval,
                  python=None) -> str:
    py = python or sys.executable
    repo = str(Path(__file__).resolve().parent.parent)
    # Source config/foreman.env first (exported) so the tick and any loop it launches inherit the
    # operator-wired vars (e.g. FOREMAN_QUOTA_CMD / FOREMAN_PLAN_TOKEN_LIMIT). Missing file is fine.
    env_file = f"{foreman_dir}/config/foreman.env"
    inner = (f"set -a; [ -f {env_file} ] && . {env_file}; set +a; "
             f"FOREMAN_DIR={foreman_dir} FOREMAN_STATE_DIR={state_dir} "
             f"FOREMAN_INDEX={index_path} FOREMAN_SPOOL={spool} PYTHONPATH={repo} "
             f"{py} -m collectors.scheduler --host {host} tick --mode {mode}")
    log = f"{os.path.expanduser(spool)}/scheduler.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-lc</string><string>{inner}</string></array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def install_launchd(**kw) -> Path:
    path = Path(os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launchd_plist(**kw))
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], capture_output=True)
    return path


def uninstall_launchd() -> None:
    path = Path(os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist"))
    if path.is_file():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()


# ---------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scheduler")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    ap.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL", "~/foreman/spool"))
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    pt = sub.add_parser("tick")
    pt.add_argument("--mode", choices=["auto", "dispatch", "launch"], default="auto")
    pi = sub.add_parser("install")
    pi.add_argument("--mode", choices=["auto", "dispatch", "launch"], default="auto")
    pi.add_argument("--interval", type=int, default=300)
    sub.add_parser("uninstall")
    args = ap.parse_args(argv)

    if args.cmd == "tick":
        out = tick(foreman_dir=Path(args.foreman_dir), state_dir=Path(args.state_dir),
                   spool=Path(args.spool), index_path=args.index or str(db.default_path()),
                   host=args.host, mode=args.mode)
        print(json.dumps(out))
        return 0
    if args.cmd == "install":
        p = install_launchd(foreman_dir=args.foreman_dir, state_dir=args.state_dir,
                            index_path=args.index or str(db.default_path()), spool=args.spool,
                            host=args.host, mode=args.mode, interval=args.interval)
        print(f"installed {LABEL} ({args.mode} every {args.interval}s): {p}")
        return 0
    if args.cmd == "uninstall":
        uninstall_launchd()
        print(f"uninstalled {LABEL}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Local-only "recently worked on" — the most recent *human* prompt in each project's Claude Code
session, so the operator sees a per-project roster of what they were last doing across the fleet.

This is deliberately NOT a collector and writes nothing to the index: prompt text can contain
sensitive content, and the index is committed to git and served on the hosted board (invariant 3).
The dashboard reads this live and local, and hides the section in the read-only hosted view.

Transcript rules still apply (invariant 4 / SPEC §16): we never scan a whole file. Per project we
stat to find its newest transcript, then read a bounded tail (<= TAIL_CAP) by seeking from EOF — a
100 MB transcript costs one seek + a bounded read, never a full scan. We only tail the handful of
most-recently-active projects.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

TAIL_CAP = 512 * 1024        # read at most the last 512 KB of a transcript (the last human turn)
PROMPT_MAX = 200             # truncate each surfaced prompt line
MAX_FILES_PER_PROJECT = 3    # if the newest session is all cadence-noise, look back a couple more


def _root(root=None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.path.expanduser(os.environ.get("FOREMAN_TRANSCRIPTS_ROOT",
                                                  "~/.claude/projects")))


def encode_cwd(cwd: str) -> str:
    """Claude Code's transcript-dir encoding of a launch cwd (non-alphanumerics -> '-'). Mirrors
    collectors.transcripts.encode_cwd for the common (<200 char) case."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _files_by_mtime(d: Path) -> list[tuple[float, Path]]:
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.jsonl"):
        try:
            out.append((f.stat().st_mtime, f))
        except OSError:
            continue
    out.sort(reverse=True)
    return out


def _tail_lines(path: Path, cap: int = TAIL_CAP) -> list[str]:
    """The lines of the last ``cap`` bytes of the file. If the tail starts mid-file the first
    (partial) line is dropped, so every returned line is whole."""
    size = path.stat().st_size
    start = max(0, size - cap)
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").split("\n")
    return lines[1:] if start > 0 else lines


def _looks_like_cadence(first_line: str) -> bool:
    """A foreman-generated headless prompt, not something the operator interactively typed:
    cadence runs open 'You are running the <x> cadence' or a '# heading' from a prompt file, and
    the config-drift probe opens 'Report as JSON only …'. These are foreman driving `claude -p`,
    so they shouldn't show up as 'what you were working on'."""
    fl = first_line.strip()
    low = fl.lower()
    return (fl.startswith("#") or low.startswith("you are running")
            or low.startswith("report as json") or "FOREMAN_RUN_ID" in fl)


def _prompt_text(rec, skip_cadence: bool = True) -> str | None:
    """The first line of a genuine human prompt in a transcript record, or None for anything that
    isn't one (assistant turns, tool results, sidechain/subagent turns, harness-injected meta, and
    — when skip_cadence — foreman's own cadence prompts)."""
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return None
    if rec.get("isMeta") or rec.get("isSidechain"):
        return None
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return None                      # a tool result, not something the human typed
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        text = "\n".join(parts)
    else:
        return None
    text = (text or "").strip()
    if (not text or text[0] == "<" or "<command-name>" in text
            or "<system-reminder>" in text or text.startswith("Caveat:")):
        return None
    first = text.splitlines()[0].strip()
    if not first:
        return None
    if skip_cadence and _looks_like_cadence(first):
        return None
    from collectors import redact                      # mask secrets/emails in the live roster
    return redact.secrets(first[:PROMPT_MAX])


def _last_human_prompt(path: Path):
    """The most recent human (non-cadence) prompt in one transcript, or None. Reads only the tail."""
    try:
        lines = _tail_lines(path, TAIL_CAP)
    except OSError:
        return None
    last = None
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        p = _prompt_text(rec)
        if p:
            last = p                             # keep walking; the final match is the most recent
    return last


def recent_by_project(project_cwds: dict, limit: int = 8, root=None) -> list[dict]:
    """Per-project roster of the last thing typed. ``project_cwds`` maps slug -> launch cwd (the
    project's worktree on this host). Returns [{slug, prompt, mtime}] for the ``limit`` most
    recently-active projects, newest first. Only those top projects are tailed (bounded work)."""
    root = _root(root)
    if not root.is_dir():
        return []
    # cheap pass: newest transcript mtime per project (stat only), then visit projects newest-first
    candidates = []
    for slug, cwd in project_cwds.items():
        if not cwd:
            continue
        files = _files_by_mtime(root / encode_cwd(cwd))
        if files:
            candidates.append((files[0][0], slug, files))
    candidates.sort(reverse=True)

    # tail in recency order, collecting real (non-cadence) prompts until we have `limit`. Projects
    # whose only recent activity is a foreman probe/cadence yield nothing and are skipped, so they
    # don't crowd out a genuinely worked-on project further down.
    out = []
    for _mt, slug, files in candidates:
        prompt, mtime = None, files[0][0]
        for fmt, path in files[:MAX_FILES_PER_PROJECT]:
            p = _last_human_prompt(path)
            if p:
                prompt, mtime = p, fmt
                break
        if prompt:
            out.append({"slug": slug, "prompt": prompt, "mtime": mtime})
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out

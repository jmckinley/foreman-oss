"""A concrete ``FOREMAN_QUOTA_CMD`` — report Claude usage headroom as the JSON the quota guard
(`collectors/runstate.default_quota_fetch`) expects: ``{pct_used, plan, window_resets}``.

There is no documented Claude quota API, so headroom is derived from the local Claude Code
transcripts (the same `~/.claude/projects/**/*.jsonl` the prompt collector reads): sum the tokens
billed in the current **5-hour rolling window** (Claude's rate-limit block) and divide by the
plan's window budget. That budget is the one operator-specific knob — set it in
``FOREMAN_PLAN_TOKEN_LIMIT``. Without it this prints **nothing**, so the guard simply does not fire
rather than deferring real work on a guessed number (SPEC §16.3: a wrong quota number is worse than
none).

Wire it:  export FOREMAN_QUOTA_CMD="python3 -m collectors.quota_probe"
          export FOREMAN_PLAN_TOKEN_LIMIT=<your ~5h total-token budget>

Reads are byte-bounded (invariant 4): only transcripts touched within the window are opened, and
each is tailed for at most the last 25 MB — the in-window usage records are always near the end of
an actively-written session file, so the tail captures them without ever scanning a 100 MB log.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

WINDOW_HOURS = 5
CAP_BYTES = 25 * 1024 * 1024        # invariant 4: never scan more than 25 MB of a transcript
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _root() -> Path:
    return Path(os.path.expanduser(
        os.environ.get("FOREMAN_TRANSCRIPTS_ROOT", "~/.claude/projects")))


def _tail(path: Path, cap: int) -> str:
    """The last ``cap`` bytes of a file, decoded leniently. The recent (in-window) usage records
    live at the end of an actively-written session, so the tail is where they are."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > cap:
                fh.seek(size - cap)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _parse_ts(s: str) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d.astimezone(dt.timezone.utc) if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _billed(usage: dict) -> int:
    """Total tokens billed for one message: fresh input + output + cache writes + cache reads.
    Matches ccusage's "total tokens", so the operator sizes FOREMAN_PLAN_TOKEN_LIMIT to the same
    number a usage tool would show."""
    return sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens"))


def window_usage(root: Path, *, now: dt.datetime) -> tuple[int, dt.datetime | None]:
    """(tokens billed in the last WINDOW_HOURS, earliest in-window activity). Only transcripts
    modified inside the window are opened; each is tailed at most CAP_BYTES."""
    since = now - dt.timedelta(hours=WINDOW_HOURS)
    total, earliest = 0, None
    for jsonl in root.glob("*/*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        if st.st_mtime < since.timestamp():
            continue
        lines = _tail(jsonl, CAP_BYTES).splitlines()
        # only drop the first line when the file was actually tailed (its head is a partial line);
        # a small file is read whole, so its first line is complete and must be kept.
        if st.st_size > CAP_BYTES:
            lines = lines[1:]
        for line in lines:
            line = line.strip()
            if not line or '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            ts = _parse_ts(rec.get("timestamp", ""))
            if ts is None or ts < since:
                continue
            total += _billed(usage)
            if earliest is None or ts < earliest:
                earliest = ts
    return total, earliest


def probe(*, root: Path | None = None, limit: int | None = None,
          now: dt.datetime | None = None) -> dict | None:
    """The quota reading, or None when no plan budget is configured (guard then does not fire)."""
    if not limit or limit <= 0:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    used, earliest = window_usage(root or _root(), now=now)
    pct = round(min(100.0, 100.0 * used / limit), 1)
    out = {"pct_used": pct, "plan": os.environ.get("FOREMAN_PLAN", "claude-code")}
    if earliest is not None:                        # the window clears when the oldest use ages out
        out["window_resets"] = (earliest + dt.timedelta(hours=WINDOW_HOURS)).strftime(_ISO)
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        limit = int(os.environ.get("FOREMAN_PLAN_TOKEN_LIMIT", "") or 0)
    except ValueError:
        limit = 0
    data = probe(limit=limit)
    if data is None:
        # No budget configured: emit nothing. runstate treats absent/blank output as "no reading",
        # so the guard stays inert rather than acting on a guess.
        print(f"quota_probe: set FOREMAN_PLAN_TOKEN_LIMIT to report headroom", file=sys.stderr)
        return 0
    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

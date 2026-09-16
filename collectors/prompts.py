"""Hourly prompt collector -> a LOCAL, out-of-repo store of the human prompts you typed to Claude
Code in each project, so 'Recently worked on' can show a per-project history/timeline, not just the
last thing in the tail window.

Local-only by construction (invariant 3): the store lives at ~/.foreman/prompts.db (override with
FOREMAN_PROMPT_STORE), NEVER in the committed index or the state branch, so prompt text is never
pushed to git or the hosted board. The store is a cache — rebuildable from transcripts — so deleting
it costs only a re-scan.

Transcript rules (invariant 4 / SPEC §16): each session is tailed FORWARD from a saved byte offset,
capped at READ_CAP_BYTES per pass, on line boundaries — never a whole-file scan, and resumable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from collectors import recent, redact

READ_CAP_BYTES = 25 * 1024 * 1024        # never read more than this from one session per pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt (
  project TEXT NOT NULL,
  ts      TEXT NOT NULL,                 -- transcript timestamp (ISO) or file mtime fallback
  text    TEXT NOT NULL,
  session TEXT NOT NULL,                 -- transcript file path
  PRIMARY KEY (session, ts, text)
);
CREATE INDEX IF NOT EXISTS prompt_project_ts ON prompt(project, ts);
CREATE TABLE IF NOT EXISTS scan_offset (
  session TEXT PRIMARY KEY,
  offset  INTEGER NOT NULL
);
"""


def store_path(store=None) -> Path:
    if store is not None:
        return Path(store)
    return Path(os.path.expanduser(os.environ.get("FOREMAN_PROMPT_STORE", "~/.foreman/prompts.db")))


def connect(store=None) -> sqlite3.Connection:
    path = store_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _prompt_with_ts(rec):
    """(iso_ts, text) for a human prompt record, or None. Reuses recent's human-only filter."""
    text = recent._prompt_text(rec)
    if text is None:
        return None
    ts = rec.get("timestamp") if isinstance(rec, dict) else None
    return ts, text


def _scan_file(conn, project: str, path: Path) -> int:
    """Tail one transcript forward from its saved offset, store new human prompts, advance the
    offset to the last whole line consumed. Returns the number of prompts added."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    row = conn.execute("SELECT offset FROM scan_offset WHERE session=?", (str(path),)).fetchone()
    start = row["offset"] if row else 0
    if start > size:                       # file truncated/rotated -> re-read from the top
        start = 0
    if start >= size:
        return 0
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(READ_CAP_BYTES)
    # keep only whole lines; a trailing partial line waits for the next pass
    nl = data.rfind(b"\n")
    if nl == -1:
        return 0
    consumed = data[:nl + 1]
    mtime = path.stat().st_mtime
    fallback_ts = __import__("datetime").datetime.fromtimestamp(
        mtime, tz=__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for line in consumed.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        pt = _prompt_with_ts(rec)
        if pt is None:
            continue
        ts, text = pt[0] or fallback_ts, redact.full(pt[1])   # mask secrets/PII before it lands
        try:
            cur = conn.execute("INSERT OR IGNORE INTO prompt(project, ts, text, session) "
                               "VALUES(?,?,?,?)", (project, ts, text, str(path)))
            added += 1 if cur.rowcount > 0 else 0
        except sqlite3.DatabaseError:
            continue
    conn.execute("INSERT INTO scan_offset(session, offset) VALUES(?,?) "
                 "ON CONFLICT(session) DO UPDATE SET offset=excluded.offset",
                 (str(path), start + len(consumed)))
    return added


def collect(project_cwds: dict, *, root=None, store=None) -> dict:
    """Scan every project's transcripts forward and append new human prompts to the local store.
    ``project_cwds`` maps slug -> the project's launch cwd (worktree on this host). Idempotent and
    resumable via per-session byte offsets."""
    root = recent._root(root)
    conn = connect(store)
    added, scanned = 0, 0
    try:
        for slug, cwd in project_cwds.items():
            if not cwd:
                continue
            for _mt, path in recent._files_by_mtime(root / recent.encode_cwd(cwd)):
                added += _scan_file(conn, slug, path)
                scanned += 1
        conn.commit()
    finally:
        conn.close()
    return {"prompts_added": added, "sessions_scanned": scanned}


def latest_by_project(store=None, limit: int = 12) -> list[dict]:
    """The most recent prompt per project (for the roster), newest first."""
    conn = connect(store)
    try:
        rows = conn.execute(
            "SELECT project, MAX(ts) AS ts, text FROM prompt GROUP BY project "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        # MAX(ts) picks the latest ts, but text must come from that same row -> resolve per project
        out = []
        for r in rows:
            top = conn.execute("SELECT ts, text FROM prompt WHERE project=? ORDER BY ts DESC LIMIT 1",
                               (r["project"],)).fetchone()
            out.append({"slug": r["project"], "prompt": top["text"], "ts": top["ts"]})
        return out
    finally:
        conn.close()


def search(query: str, *, store=None, limit: int = 60) -> list[dict]:
    """Prompts whose (already-redacted) text contains ``query``, newest first, across all projects."""
    q = (query or "").strip()
    if not q:
        return []
    conn = connect(store)
    try:
        rows = conn.execute(
            "SELECT project, ts, text FROM prompt WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY ts DESC LIMIT ?",
            ("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit)
        ).fetchall()
        return [{"slug": r["project"], "ts": r["ts"], "text": r["text"]} for r in rows]
    finally:
        conn.close()


def counts_since(cutoff_iso: str, *, store=None) -> list[dict]:
    """Per-project prompt counts since ``cutoff_iso`` (ISO-UTC string compare), busiest first."""
    conn = connect(store)
    try:
        rows = conn.execute(
            "SELECT project, COUNT(*) AS n FROM prompt WHERE ts >= ? GROUP BY project "
            "ORDER BY n DESC", (cutoff_iso,)).fetchall()
        return [{"slug": r["project"], "count": r["n"]} for r in rows]
    finally:
        conn.close()


def timeline(project: str, store=None, limit: int = 20) -> list[dict]:
    """A project's recent human prompts, newest first — the expandable history."""
    conn = connect(store)
    try:
        rows = conn.execute("SELECT ts, text FROM prompt WHERE project=? ORDER BY ts DESC LIMIT ?",
                            (project, limit)).fetchall()
        return [{"ts": r["ts"], "text": r["text"]} for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------- hourly launchd job

LABEL = "com.foreman.prompts"


def launchd_plist(*, foreman_dir, host, interval=3600, python=None) -> str:
    """A launchd agent that runs ``collectors.prompts collect`` every ``interval`` seconds."""
    import sys
    py = python or sys.executable
    repo = str(Path(__file__).resolve().parent.parent)
    inner = (f"FOREMAN_DIR={foreman_dir} PYTHONPATH={repo} "
             f"{py} -m collectors.prompts collect --host {host}")
    log = os.path.expanduser("~/.foreman/prompts.log")
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
    Path(os.path.expanduser("~/.foreman")).mkdir(parents=True, exist_ok=True)
    path.write_text(launchd_plist(**kw))
    __import__("subprocess").run(["launchctl", "unload", str(path)], capture_output=True)
    __import__("subprocess").run(["launchctl", "load", str(path)], capture_output=True)
    return path


def _cli(argv=None):
    import argparse
    import yaml
    ap = argparse.ArgumentParser(prog="collectors.prompts", description="collect human prompts")
    ap.add_argument("cmd", choices=["collect", "install"])
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    ap.add_argument("--interval", type=int, default=3600, help="install: seconds between runs")
    args = ap.parse_args(argv)
    if args.cmd == "install":
        p = install_launchd(foreman_dir=os.path.abspath(args.foreman_dir), host=args.host,
                            interval=args.interval)
        print(f"installed {LABEL} (every {args.interval}s): {p}")
        return
    reg = yaml.safe_load((Path(args.foreman_dir) / "registry.yaml").read_text())
    cwds = {p["slug"]: (p.get("worktree") or {}).get(args.host) for p in reg.get("projects", [])}
    result = collect(cwds)
    print(f"prompts: +{result['prompts_added']} from {result['sessions_scanned']} session(s) "
          f"-> {store_path()}")


if __name__ == "__main__":
    _cli()

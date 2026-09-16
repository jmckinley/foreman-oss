"""C1 -- session transcripts, tailed by byte offset.

The transcript entry format is internal to Claude Code and changes between versions, and a
single file can be 100+ MB. Both facts drive hard rules (SPEC.md sections 9, 16):

- **Never grep at query time.** Tail forward from ``session.last_offset``, commit the new
  offset. Nothing here scans a whole file to answer a query.
- **25 MB read cap per pass.** If the unread tail exceeds the cap the session is marked
  oversized and skipped -- a 102 MB transcript has hung Claude Code at 90% RAM, and the
  collector must never be the thing that reproduces it.
- **Extract two things only:** decisions and skill fires. Everything numeric comes from C2.
- **Parser version pin.** On an unrecognized envelope type C1 stops and raises red rather
  than write wrong rows: silent partial parsing is worse than no parsing. Rows are buffered
  and written only after a clean pass, so a mid-file halt writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from collectors import db

PARSER_VERSION = 1
READ_CAP_BYTES = 25 * 1024 * 1024

# Top-level record shapes C1 understands structurally. An envelope type outside this set is
# a shape change: halt and raise red (the acceptance's "unknown envelope type"). Unknown
# *content* inside a known envelope (a tool we do not extract) is skipped, not fatal.
KNOWN_ENVELOPE_TYPES = {"user", "assistant", "system", "summary", "file-history-snapshot"}


class EnvelopeShapeError(Exception):
    """A transcript record does not match the known envelope shape. Halts C1, raises red."""


@dataclass
class Extracted:
    decisions: list[dict] = field(default_factory=list)
    skill_fires: list[dict] = field(default_factory=list)


def encode_cwd(cwd: str) -> str:
    """Transcript dir encoding: non-alphanumerics -> '-', truncated to 200 chars with a hash
    of the full path appended when longer (SPEC.md section 9)."""
    enc = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    if len(enc) > 200:
        digest = hashlib.sha1(cwd.encode()).hexdigest()[:12]
        enc = enc[:200] + "-" + digest
    return enc


def _check_envelope(record) -> str:
    if not isinstance(record, dict):
        raise EnvelopeShapeError("transcript line is not a JSON object")
    rtype = record.get("type")
    if not isinstance(rtype, str):
        raise EnvelopeShapeError("transcript record has no string 'type' envelope")
    if rtype not in KNOWN_ENVELOPE_TYPES:
        raise EnvelopeShapeError(f"unknown envelope type {rtype!r} (parser v{PARSER_VERSION})")
    return rtype


def _extract(record: dict, rtype: str, out: Extracted) -> None:
    ts = record.get("timestamp")
    if rtype == "assistant":
        content = (record.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Skill":
                    inp = block.get("input") or {}
                    skill = inp.get("command") or inp.get("skill") or inp.get("name")
                    if skill:
                        out.skill_fires.append({"ts": ts, "skill": str(skill).lstrip("/"),
                                                "invoked_by": "model"})
                # An explicit decision block, when present, is the only structured decision
                # C1 records -- no NLP, no numeric extraction (SPEC.md section 9).
                if block.get("type") == "decision":
                    out.decisions.append({"ts": ts, "kind": block.get("kind"),
                                          "summary": block.get("summary"),
                                          "files": json.dumps(block.get("files") or [])})
    elif rtype == "system" and record.get("subtype") == "decision":
        out.decisions.append({"ts": ts, "kind": record.get("kind"),
                              "summary": record.get("summary"),
                              "files": json.dumps(record.get("files") or [])})


def parse_window(text: str) -> tuple[Extracted, int]:
    """Parse complete newline-terminated lines. Returns (extracted, consumed_bytes).

    A trailing partial line (file written mid-flight) is left unconsumed so the next pass
    picks it up whole. Raises EnvelopeShapeError on the first shape violation.
    """
    out = Extracted()
    consumed = 0
    for line in text.splitlines(keepends=True):
        if not line.endswith("\n"):
            break  # incomplete trailing line; do not consume
        stripped = line.strip()
        if stripped:
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise EnvelopeShapeError(f"transcript line is not valid JSON: {exc}") from exc
            rtype = _check_envelope(record)
            _extract(record, rtype, out)
        consumed += len(line.encode())
    return out, consumed


def tail_session(conn, *, session_uuid: str, path: Path, project: str | None, host: str,
                 surface: str, cwd: str | None, cc_version: str) -> dict:
    """Tail one transcript from its stored offset and write decisions + skill fires.

    Returns a small status dict. On an oversized unread tail the session is marked and
    skipped without reading. On an envelope shape change nothing is written, the offset is
    left where it was, and EnvelopeShapeError propagates (the collector run goes red).
    """
    size = path.stat().st_size
    row = conn.execute("SELECT last_offset, oversized FROM session WHERE session_uuid = ?",
                       (session_uuid,)).fetchone()
    last_offset = row["last_offset"] if row else 0

    base = {"session_uuid": session_uuid, "project": project, "host": host, "surface": surface,
            "cwd": cwd, "transcript_path": str(path), "transcript_bytes": size,
            "cc_version": cc_version, "parser_version": PARSER_VERSION}

    # Oversized: the unread tail exceeds the cap. Mark and skip -- never read it.
    if size - last_offset > READ_CAP_BYTES:
        db.upsert(conn, "session", {**base, "last_offset": last_offset, "oversized": 1},
                  keys=["session_uuid"])
        conn.commit()
        return {"session": session_uuid, "oversized": True, "read_bytes": 0}

    with path.open("rb") as fh:
        fh.seek(last_offset)
        chunk = fh.read(size - last_offset)
    extracted, consumed = parse_window(chunk.decode("utf-8", errors="replace"))

    # Clean pass: write buffered rows, then advance the offset.
    db.upsert(conn, "session", {**base, "last_offset": last_offset + consumed, "oversized": 0},
              keys=["session_uuid"])
    for d in extracted.decisions:
        conn.execute(
            "INSERT INTO session_decision(session_uuid, ts, kind, summary, files) VALUES (?,?,?,?,?)",
            (session_uuid, d["ts"], d["kind"], d["summary"], d["files"]))
    for s in extracted.skill_fires:
        conn.execute(
            "INSERT INTO skill_fire(session_uuid, ts, skill, scope, source, invoked_by) "
            "VALUES (?,?,?,?,?,?)",
            (session_uuid, s["ts"], s["skill"], None, None, s["invoked_by"]))
    conn.commit()
    return {"session": session_uuid, "oversized": False, "read_bytes": consumed,
            "decisions": len(extracted.decisions), "skill_fires": len(extracted.skill_fires)}


def _resolve_project(registry: dict, cwd: str | None) -> str | None:
    if not cwd:
        return None
    from collectors.decisions import resolve_project
    hit = resolve_project(registry, Path(cwd))
    return hit[0] if hit else None


def scan(conn, *, roots: list[tuple[str, str]], host: str, registry: dict,
         cc_version: str = "unknown") -> dict:
    """Walk configured surface roots ``[(path, surface), ...]`` and tail every transcript.

    Surface fragmentation (SPEC.md failure guard 7): the CLI, desktop, web and VS Code keep
    history in different roots. Pass them all or the picture is partial.
    """
    scanned = oversized = 0
    for root, surface in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            continue
        for jsonl in root_path.glob("*/*.jsonl"):
            cwd = _first_cwd(jsonl)
            res = tail_session(conn, session_uuid=jsonl.stem, path=jsonl,
                               project=_resolve_project(registry, cwd), host=host,
                               surface=surface, cwd=cwd, cc_version=cc_version)
            scanned += 1
            oversized += 1 if res["oversized"] else 0
    return {"scanned": scanned, "oversized": oversized}


def _first_cwd(jsonl: Path) -> str | None:
    """Read the cwd from the first record without loading the whole file."""
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("cwd"):
                    return rec["cwd"]
    except OSError:
        return None
    return None

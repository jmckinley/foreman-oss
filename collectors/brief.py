"""Render the consolidated brief straight from receipts on the state branch. No index.

This is the M2 renderer behind the ``/brief`` skill (SPEC.md section 12). It reads
``<state>/receipts/``, groups receipts by (project, cadence), takes the latest per pair,
and renders the board: NEEDS YOU, the CADENCES grid, QUEUED (the decision queue), and QUIET.

Two rules the brief owns, not the runs:

- **Staleness.** A cadence with no receipt within twice its schedule interval renders as
  ``stale`` and enters NEEDS YOU. That is how a dead scheduler becomes visible: absence is a
  verdict (SPEC.md sections 12, 13, 16).
- **Amber aging.** Amber ages from the *first* amber in an unbroken amber run, not the last.
  When it has aged past the cadence's ``amber_ages_to_red_after_days`` it converts to red.

RUNNING and DRIFT need the index / config resolver (M3/M4) and are omitted here rather than
faked -- the brief says only what receipts can support.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import yaml

from collectors import decisions
from collectors.validate import Cron

ROOT = Path(__file__).resolve().parent.parent
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _parse(iso: str) -> dt.datetime:
    return dt.datetime.strptime(iso, _ISO).replace(tzinfo=dt.timezone.utc)


def _age(then: dt.datetime, now: dt.datetime) -> str:
    secs = max(0, (now - then).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


# ------------------------------------------------------------------------- loading

def load_receipts(state_dir: Path,
                  include_coordination: bool = False) -> dict[tuple[str, str], list[dict]]:
    """Receipts grouped by (project, cadence), each list sorted oldest-first.

    By default locked/skipped receipts are dropped: they are coordination artifacts, not
    work verdicts, and must not show as runs in the brief. Ingestion passes
    ``include_coordination=True`` because a locked run is still a run in the index.
    """
    root = state_dir / "receipts"
    groups: dict[tuple[str, str], list[dict]] = {}
    if not root.is_dir():
        return groups
    import json
    for path in root.glob("*/*/*.json"):
        try:
            r = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not include_coordination and r.get("status") in ("locked", "skipped"):
            continue
        key = (r.get("project"), r.get("cadence"))
        groups.setdefault(key, []).append(r)
    for lst in groups.values():
        lst.sort(key=lambda r: (r.get("ended", ""), r.get("run_id", "")))
    return groups


def _cadence_meta(foreman_dir: Path) -> dict[str, dict]:
    out = {}
    cad_dir = foreman_dir / "cadences"
    if cad_dir.is_dir():
        for p in cad_dir.glob("*.yaml"):
            out[p.stem] = yaml.safe_load(p.read_text())
    return out


# ------------------------------------------------------------------- pair analysis

def stale_after_minutes(cad: dict | None) -> int | None:
    """Twice the schedule's longest normal gap, or None if the schedule is unknown."""
    if not cad or "schedule" not in cad:
        return None
    try:
        return 2 * Cron.parse(cad["schedule"]).max_interval_minutes()
    except ValueError:
        return None


def amber_run_start(history: list[dict]) -> dict | None:
    """The first receipt of the current unbroken amber run at the tip, else None."""
    if not history or history[-1].get("verdict") != "amber":
        return None
    start = history[-1]
    for r in reversed(history[:-1]):
        if r.get("verdict") == "amber":
            start = r
        else:
            break
    return start


def analyze_pair(history: list[dict], cad: dict | None, now: dt.datetime) -> dict:
    """Effective state of one (project, cadence): verdict, age, staleness, amber aging."""
    latest = history[-1] if history else None
    if latest is None:
        # No receipts. Only judge staleness for a cadence with a known schedule -- a
        # cadence merely declared in the registry but not yet defined (no cadence file)
        # is not-yet-active, rendered "-", not a dead scheduler.
        if cad is None or "schedule" not in cad:
            return {"effective": "none", "label": "-", "latest": None,
                    "needs_you": False, "severity": None, "reason": ""}
        return {"effective": "stale", "label": "stale", "latest": None, "needs_you": True,
                "severity": "R", "reason": "scheduled but no receipt ever recorded"}

    ended = _parse(latest["ended"])
    threshold = stale_after_minutes(cad)
    is_stale = threshold is not None and (now - ended).total_seconds() > threshold * 60
    age = _age(ended, now)

    if is_stale:
        return {"effective": "stale", "label": f"stale {age}", "latest": latest,
                "needs_you": True, "severity": "R",
                "reason": f"no receipt in {age}, past twice the schedule interval"}

    verdict = latest.get("verdict")
    status = latest.get("status")

    if verdict == "red" or status in ("failed", "timeout"):
        return {"effective": "red", "label": f"red {age}", "latest": latest,
                "needs_you": True, "severity": "R", "reason": _summary(latest)}

    if verdict == "amber":
        start = amber_run_start(history)
        amber_days = (now - _parse(start["ended"])).days if start else 0
        limit = (cad or {}).get("amber_ages_to_red_after_days")
        if limit is not None:
            remaining = limit - amber_days
            if remaining < 0:
                return {"effective": "red", "label": f"red {age} (amber {amber_days}d)",
                        "latest": latest, "needs_you": True, "severity": "R",
                        "reason": f"amber aged past {limit}d -> red"}
            if remaining == 0:
                return {"effective": "amber", "label": f"amber {amber_days}d",
                        "latest": latest, "needs_you": True, "severity": "A",
                        "reason": "ages to red today"}
        return {"effective": "amber", "label": f"amber {age}", "latest": latest,
                "needs_you": False, "severity": "A", "reason": _summary(latest)}

    return {"effective": "green", "label": f"green {age}", "latest": latest,
            "needs_you": False, "severity": None, "reason": ""}


def _summary(receipt: dict) -> str:
    esc = receipt.get("escalations") or []
    if esc:
        return esc[0].get("summary") or esc[0].get("evidence") or ""
    return receipt.get("notes") or ""


def _pr_ref(receipt: dict) -> str:
    for a in receipt.get("artifacts") or []:
        if a.get("kind") == "pr":
            return f"PR {a['ref']}"
    return ""


# ----------------------------------------------------------------------- rendering

def render(state_dir: Path, foreman_dir: Path, now: dt.datetime | None = None,
           index_path: str | Path | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    cadences = _cadence_meta(foreman_dir)
    groups = load_receipts(state_dir)

    projects = [p["slug"] for p in registry.get("projects", [])]
    project_cadences = {p["slug"]: list(p.get("cadences", [])) for p in registry.get("projects", [])}
    # Column order: cadences in the order they first appear across projects.
    columns: list[str] = []
    for slug in projects:
        for cad in project_cadences[slug]:
            if cad not in columns:
                columns.append(cad)

    analysis: dict[tuple[str, str], dict] = {}
    for slug in projects:
        for cad in project_cadences[slug]:
            history = groups.get((slug, cad), [])
            analysis[(slug, cad)] = analyze_pair(history, cadences.get(cad), now)

    out: list[str] = []
    out.append(_header(registry, groups, now, index_path))
    out.append("")
    # DRIFT leads: the effective-vs-declared gap is Foreman's headline signal, so a config
    # that silently diverged from intent is the first thing an operator sees, not buried.
    out.append(_drift(index_path, registry))
    out.append("")
    out.append(_needs_you(analysis, now))
    out.append("")
    out.append(_grid(projects, columns, project_cadences, analysis))
    out.append("")
    out.append(decisions.board(state_dir, now=now, known=set(projects)))
    out.append("")
    out.append(_quiet(groups, now, index_path))
    return "\n".join(out)


def _header(registry: dict, groups, now: dt.datetime, index_path=None) -> str:
    week_ago = now - dt.timedelta(days=7)
    cost = 0.0
    for lst in groups.values():
        for r in lst:
            if r.get("cost_usd") and _parse(r["ended"]) >= week_ago:
                cost += r["cost_usd"]
    n = len(registry.get("projects", []))
    stamp = now.strftime("%a %d %b, %H:%M")
    cost_s = f"${cost:.2f} wk" if cost else "$0.00 wk"
    quota_s, defer = _quota_bits(index_path)
    head = f"FOREMAN — {stamp}Z          {quota_s}{cost_s} · {n} projects"
    if defer:
        head += "\n  quota low: non-red cadences deferred to the next window"
    return head


def _quota_bits(index_path) -> tuple[str, bool]:
    if not index_path or not Path(index_path).exists():
        return "", False
    from collectors import db, quota
    conn = db.connect(index_path)
    try:
        h = quota.headroom(conn)
        return (f"quota {int(h)}% · " if h is not None else ""), quota.low(conn)
    finally:
        conn.close()


def _needs_you(analysis: dict, now: dt.datetime) -> str:
    items = [(k, a) for k, a in analysis.items() if a["needs_you"]]
    # Red before amber; within a severity, oldest first.
    def sort_key(item):
        _key, a = item
        ended = _parse(a["latest"]["ended"]) if a["latest"] else now
        return (0 if a["severity"] == "R" else 1, ended)
    items.sort(key=sort_key)
    if not items:
        return "NEEDS YOU (0)\n  (nothing needs a decision)"
    lines = [f"NEEDS YOU ({len(items)})"]
    for (proj, cad), a in items:
        sev = a["severity"]
        head = f"  {sev}  {proj} / {cad}"
        lines.append(f"{head:<34} {a['label']:<12} {a['reason']}".rstrip())
        latest = a["latest"]
        na = (latest or {}).get("next_action", "") if latest else ""
        pr = _pr_ref(latest) if latest else ""
        action = f"     -> {na}" if na else "     -> (no next_action on record)"
        lines.append(f"{action:<62}{pr}".rstrip())
    return "\n".join(lines)


def _grid(projects, columns, project_cadences, analysis) -> str:
    if not columns:
        return "CADENCES\n  (no cadences configured)"
    proj_w = max([len("project")] + [len(p) for p in projects]) + 2
    cells: dict = {}
    col_w = {}
    for cad in columns:
        w = len(cad)
        for slug in projects:
            if cad in project_cadences.get(slug, []):
                lbl = analysis[(slug, cad)]["label"]
            else:
                lbl = "-"
            cells[(slug, cad)] = lbl
            w = max(w, len(lbl))
        col_w[cad] = w + 2
    header = "  " + "project".ljust(proj_w) + "".join(c.ljust(col_w[c]) for c in columns)
    lines = ["CADENCES", header.rstrip()]
    for slug in projects:
        row = "  " + slug.ljust(proj_w) + "".join(cells[(slug, c)].ljust(col_w[c]) for c in columns)
        lines.append(row.rstrip())
    return "\n".join(lines)


def _drift(index_path, registry: dict) -> str:
    """DRIFT is the C6 output: pin drift, shadowing surprises, budget breach. Needs the index;
    when absent (M2 mode) the section is omitted rather than faked."""
    if not index_path or not Path(index_path).exists():
        return "DRIFT\n  (index unavailable; run collectors to populate)"
    from collectors import config_resolve, db
    conn = db.connect(index_path)
    try:
        lines = config_resolve.drift_lines(conn, registry)
    finally:
        conn.close()
    if not lines:
        return "DRIFT\n  (none)"
    body = "\n".join(f"  {scope:<12} {msg}" for scope, msg in lines)
    return f"DRIFT ({len(lines)})\n{body}"


def _quiet(groups, now: dt.datetime, index_path=None) -> str:
    week_ago = now - dt.timedelta(days=7)
    green = sum(
        1 for lst in groups.values() for r in lst
        if r.get("verdict") == "green" and _parse(r["ended"]) >= week_ago
    )
    lines = [f"{green} green runs in the last 7d"]
    lines.extend(_quiet_index_lines(index_path, now))
    return "QUIET\n" + "\n".join(f"  {ln}" for ln in lines)


def _quiet_index_lines(index_path, now: dt.datetime) -> list[str]:
    """Cost per project/cadence and the unused-skill list -- both need the index (C1/C2)."""
    if not index_path or not Path(index_path).exists():
        return []
    from collectors import db
    conn = db.connect(index_path)
    out: list[str] = []
    try:
        cutoff = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Cost per project/cadence from ingested runs over the last 7d.
        wk = (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cost = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) c FROM run WHERE ended >= ?", (wk,)).fetchone()
        if cost and cost["c"]:
            out.append(f"${cost['c']:.2f} spent across cadences in the last 7d")
        # Skills installed (invocable) with no fire in 30d -> prune candidates.
        try:
            snap = conn.execute("SELECT id FROM config_snapshot ORDER BY taken DESC LIMIT 1").fetchone()
            if snap:
                installed = {r["name"] for r in conn.execute(
                    "SELECT DISTINCT name FROM component WHERE snapshot_id = ? AND kind='skill' "
                    "AND invocable = 1", (snap["id"],))}
                fired = {r["skill"] for r in conn.execute(
                    "SELECT DISTINCT skill FROM skill_fire WHERE ts >= ?", (cutoff,))}
                unused = sorted(installed - fired)
                if unused:
                    shown = ", ".join(unused[:5]) + (" ..." if len(unused) > 5 else "")
                    out.append(f"{len(unused)} skills unused for 30d: {shown}")
        except Exception:
            pass
    finally:
        conn.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brief")
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    args = ap.parse_args(argv)
    if not args.state_dir:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        return 2
    print(render(Path(os.path.expanduser(args.state_dir)),
                 Path(os.path.expanduser(args.foreman_dir)),
                 index_path=os.path.expanduser(args.index) if args.index else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Escalation lifecycle (SPEC.md section 13).

Red opens a GitHub issue in the owning repo, one per (project, cadence). A repeat red
comments on that issue rather than opening another. A green run closes the issue with a link
to the receipt that cleared it. Escalations resolve only through ``/close`` or a green run --
nothing expires quietly.

This module owns the ``escalation`` table (ingest writes only ``run`` and ``metric``). It is
idempotent and index-drop-safe: GitHub issues are found by title before a new one is opened,
so re-running after a dropped index re-links to the existing issue instead of duplicating it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from collectors import db
from collectors.brief import analyze_pair, load_receipts, _cadence_meta
from collectors.build_receipt import _compact

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO)


def _esc_id(project: str, cadence: str, first_seen: str) -> int:
    return int(hashlib.sha1(f"{project}|{cadence}|{first_seen}".encode()).hexdigest()[:15], 16)


def streak_start(history: list[dict]) -> dict:
    """First receipt of the current unbroken non-green streak at the tip."""
    start = history[-1]
    for r in reversed(history[:-1]):
        if r.get("verdict") != "green" and r.get("status") not in ("locked", "skipped"):
            start = r
        else:
            break
    return start


def receipt_relpath(receipt: dict) -> str:
    return (f"receipts/{receipt['project']}/{receipt['cadence']}/"
            f"{_compact(receipt['ended'])}-{receipt['run_id']}.json")


def issue_title(project: str, cadence: str) -> str:
    return f"[foreman] {cadence} red on {project}"


# --------------------------------------------------------------------------- gh IO

class GitHub:
    """Thin ``gh`` wrapper. Tests inject a fake with the same four methods."""

    def __init__(self, run=None):
        self._run = run or self._subprocess

    @staticmethod
    def _subprocess(args: list[str]) -> str | None:
        try:
            p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout if p.returncode == 0 else None

    def find_issue(self, repo: str, title: str) -> int | None:
        out = self._run(["issue", "list", "--repo", repo, "--state", "open",
                         "--search", f'in:title "{title}"', "--json", "number,title"])
        try:
            for i in json.loads(out or "[]"):
                if i.get("title") == title:
                    return i["number"]
        except (TypeError, ValueError):
            return None
        return None

    def open_issue(self, repo: str, title: str, body: str, labels: list[str]) -> int | None:
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for lb in labels:
            args += ["--label", lb]
        out = self._run(args)  # gh prints the issue URL, ending in the number
        if not out:
            return None
        tail = out.strip().rsplit("/", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def comment(self, repo: str, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--repo", repo, "--body", body])

    def close_issue(self, repo: str, number: int, comment: str) -> None:
        self._run(["issue", "close", str(number), "--repo", repo, "--comment", comment])


# --------------------------------------------------------------------- reconcile

def _open_episodes(groups, cadences, now) -> dict:
    """Pairs currently red (red verdict, failed/timeout status, or amber aged to red)."""
    episodes = {}
    for (project, cadence), history in groups.items():
        real = [r for r in history if r.get("status") not in ("locked", "skipped")]
        if not real:
            continue
        a = analyze_pair(real, cadences.get(cadence), now)
        if a["effective"] == "red" and a["latest"] is not None:
            episodes[(project, cadence)] = {"latest": a["latest"],
                                            "first_seen": streak_start(real)["ended"],
                                            "reason": a["reason"]}
    return episodes


def _latest_green(history: list[dict]) -> dict | None:
    for r in reversed(history):
        if r.get("verdict") == "green" and r.get("status") not in ("locked", "skipped"):
            return r
    return None


def reconcile(conn, state_dir, foreman_dir, *, github: GitHub | None = None,
              now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    github = github or GitHub()
    registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
    pmeta = {p["slug"]: p for p in registry.get("projects", [])}
    cadences = _cadence_meta(Path(foreman_dir))
    groups = load_receipts(Path(state_dir))

    episodes = _open_episodes(groups, cadences, now)
    opened = closed = commented = 0

    # Close escalations whose pair is no longer red -- a green run cleared it.
    for row in conn.execute("SELECT * FROM escalation WHERE resolved IS NULL").fetchall():
        pair = (row["project"], row["cadence"])
        if pair in episodes:
            continue
        clearing = _latest_green(groups.get(pair, []))
        link = receipt_relpath(clearing) if clearing else "(no clearing receipt found)"
        conn.execute("UPDATE escalation SET resolved = ?, resolution = ? WHERE id = ?",
                     (_now_iso(), f"cleared by green run; {link}", row["id"]))
        repo = (pmeta.get(row["project"]) or {}).get("repo")
        if row["github_issue"] and repo:
            rid = clearing["run_id"] if clearing else "unknown"
            github.close_issue(repo, row["github_issue"],
                               f"Cleared by green run {rid}. Receipt: {link}")
        closed += 1

    # Open or update current red episodes.
    for (project, cadence), ep in episodes.items():
        latest = ep["latest"]
        first_seen = ep["first_seen"]
        eid = _esc_id(project, cadence, first_seen)
        esc = (latest.get("escalations") or [{}])[0]
        summary = esc.get("summary") or latest.get("next_action")
        evidence = esc.get("evidence") or latest.get("notes") or ep["reason"]

        row = conn.execute(
            "SELECT * FROM escalation WHERE project = ? AND cadence = ? AND resolved IS NULL",
            (project, cadence)).fetchone()
        new_red = row is None or row["run_id"] != latest["run_id"]
        db.upsert(conn, "escalation", {
            "id": eid, "run_id": latest["run_id"], "project": project, "cadence": cadence,
            "severity": "red", "summary": summary, "evidence": evidence,
            "opened": first_seen, "first_seen": first_seen,
            "github_issue": row["github_issue"] if row else None,
            "resolved": None, "resolution": None,
        }, keys=["id"])

        pm = pmeta.get(project) or {}
        repo = pm.get("repo")
        wants_issue = ((pm.get("escalation") or {}).get("github_issues")) and repo
        if not wants_issue:
            continue
        labels = (pm.get("escalation") or {}).get("labels") or []
        current_issue = row["github_issue"] if row else None
        if current_issue is None:
            title = issue_title(project, cadence)
            num = github.find_issue(repo, title)
            if num is None:
                body = (f"**{cadence}** is red on **{project}** since {first_seen}.\n\n"
                        f"Next action: {latest.get('next_action')}\n\n"
                        f"Receipt: {receipt_relpath(latest)}\n\nOpened by Foreman.")
                num = github.open_issue(repo, title, body, labels)
            if num is not None:
                conn.execute("UPDATE escalation SET github_issue = ? WHERE id = ?", (num, eid))
                opened += 1
        elif new_red:
            github.comment(repo, current_issue,
                           f"Still red as of {latest['run_id']}: {latest.get('next_action')}")
            commented += 1

    conn.commit()
    return {"open": len(episodes), "opened_issues": opened, "closed": closed,
            "commented": commented}


def close(conn, escalation_id: int, reason: str, *, foreman_dir, github: GitHub | None = None) -> bool:
    """/close: resolve an escalation with a reason and close its GitHub issue if any."""
    row = conn.execute("SELECT * FROM escalation WHERE id = ?", (escalation_id,)).fetchone()
    if row is None or row["resolved"] is not None:
        return False
    conn.execute("UPDATE escalation SET resolved = ?, resolution = ? WHERE id = ?",
                 (_now_iso(), f"/close: {reason}", escalation_id))
    conn.commit()
    if row["github_issue"]:
        registry = yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())
        repo = next((p.get("repo") for p in registry.get("projects", [])
                     if p["slug"] == row["project"]), None)
        if repo:
            (github or GitHub()).close_issue(repo, row["github_issue"],
                                             f"Closed via /close: {reason}")
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os
    from collectors import db

    ap = argparse.ArgumentParser(prog="escalations")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reconcile")
    pc = sub.add_parser("close")
    pc.add_argument("escalation_id", type=int)
    pc.add_argument("reason")
    args = ap.parse_args(argv)

    conn = db.open_index(args.index or str(db.default_path()))
    if args.cmd == "reconcile":
        print(json.dumps(reconcile(conn, args.state_dir, args.foreman_dir)))
        return 0
    if args.cmd == "close":
        ok = close(conn, args.escalation_id, args.reason, foreman_dir=args.foreman_dir)
        print(f"{'resolved' if ok else 'no-op (unknown or already resolved)'}: {args.escalation_id}")
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

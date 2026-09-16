"""C5 -- GitHub state per repo, via the ``gh`` CLI.

Produces one ``github_state`` row: open PRs, agent-authored PRs, oldest PR age, failing
checks grouped by failure class, security and dependabot alerts, Actions minutes, open
issues. IO (``gather``, which shells out to ``gh``) is separated from logic (``compute``,
pure) so the classification is testable without hitting the network. Failure modes: rate
limit or token expiry -- ``gather`` degrades each field to a default rather than aborting.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

AGENT_LOGINS = {"dependabot", "dependabot[bot]", "github-actions", "github-actions[bot]",
                "foreman", "foreman[bot]"}
FAIL_STATES = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}


def is_agent(login: str, is_bot: bool = False) -> bool:
    login = (login or "").lower()
    return is_bot or login.endswith("[bot]") or login in AGENT_LOGINS \
        or "claude" in login or "agent" in login


def classify_check(name: str) -> str:
    n = (name or "").lower()
    if "test" in n:
        return "test"
    if "lint" in n or "format" in n or "style" in n:
        return "lint"
    if "build" in n or "compile" in n:
        return "build"
    if "deploy" in n or "release" in n:
        return "deploy"
    if "security" in n or "scan" in n or "codeql" in n:
        return "security"
    return "other"


def _parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def compute(raw: dict, *, project: str, taken: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    prs = raw.get("prs") or []

    agent_prs = sum(1 for p in prs if is_agent(p.get("author_login", ""), p.get("is_bot", False)))
    oldest_days = 0
    for p in prs:
        if p.get("created_at"):
            oldest_days = max(oldest_days, (now - _parse_iso(p["created_at"])).days)

    failing = 0
    classes: dict[str, int] = {}
    for p in prs:
        for chk in p.get("checks") or []:
            if (chk.get("state") or "").upper() in FAIL_STATES:
                failing += 1
                cls = classify_check(chk.get("name", ""))
                classes[cls] = classes.get(cls, 0) + 1

    return {
        "project": project,
        "taken": taken,
        "open_prs": len(prs),
        "agent_prs": agent_prs,
        "oldest_pr_days": oldest_days,
        "failing_checks": failing,
        "failure_classes": json.dumps(classes, sort_keys=True),
        "security_alerts": raw.get("security_alerts", 0),
        "dependabot_open": raw.get("dependabot_open", 0),
        "actions_minutes_month": raw.get("actions_minutes_month"),
        "issues_open": raw.get("issues_open", 0),
    }


# --------------------------------------------------------------------------- gh IO

def _gh_json(args: list[str]):
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def gather(repo: str, gh=_gh_json) -> dict:
    """Collect raw GitHub state via gh. Every field degrades to a default on failure."""
    raw: dict = {"prs": [], "issues_open": 0, "security_alerts": 0,
                 "dependabot_open": 0, "actions_minutes_month": None}

    prs = gh(["pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
              "--json", "number,author,createdAt,statusCheckRollup"])
    if isinstance(prs, list):
        for p in prs:
            author = p.get("author") or {}
            checks = [{"name": c.get("name") or c.get("context"),
                       "state": c.get("state") or c.get("conclusion")}
                      for c in (p.get("statusCheckRollup") or [])]
            raw["prs"].append({
                "number": p.get("number"),
                "author_login": author.get("login", ""),
                "is_bot": (author.get("__typename") == "Bot"),
                "created_at": p.get("createdAt"),
                "checks": checks,
            })

    issues = gh(["issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
                 "--json", "number"])
    if isinstance(issues, list):
        raw["issues_open"] = len(issues)

    dependabot = gh(["api", f"repos/{repo}/dependabot/alerts?state=open&per_page=100"])
    if isinstance(dependabot, list):
        raw["dependabot_open"] = len(dependabot)

    security = gh(["api", f"repos/{repo}/code-scanning/alerts?state=open&per_page=100"])
    if isinstance(security, list):
        raw["security_alerts"] = len(security)

    return raw


def collect(repo: str, *, project: str, taken: str, now: dt.datetime | None = None,
            gh=_gh_json) -> dict:
    return compute(gather(repo, gh), project=project, taken=taken, now=now)

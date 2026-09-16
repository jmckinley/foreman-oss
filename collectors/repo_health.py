"""Turn raw git/GitHub state (C4/C5) into plain-English signals with a suggested action.

The index stores counts; this reads them and says what they mean and what to do, so the
brief and the dashboard show "100 uncommitted files — commit or stash" instead of a bare
number. Each signal is (level, text): red = act now, amber = worth attention, info = context.
"""

from __future__ import annotations

DIRTY_AMBER = 50
BLOB_RED_MB = 50
OLD_PR_AMBER_DAYS = 7
STALE_BRANCH_AMBER = 3      # 1-2 stale branches is routine; only nudge once it's a pile
AHEAD_AMBER = 10            # a big unpushed backlog is worth flagging (work not backed up)


def _n(row, col: str) -> int:
    try:
        v = row[col]
    except (KeyError, IndexError, TypeError):
        return 0
    return int(v) if v is not None else 0


def signals(git_row=None, github_row=None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    g = git_row
    if g is not None:
        d = _n(g, "dirty_files")
        if d > DIRTY_AMBER:
            out.append(("amber", f"{d} uncommitted files — commit or stash"))
        elif d > 0:
            out.append(("info", f"{d} uncommitted file(s)"))
        if _n(g, "behind") > 0:
            out.append(("amber", f"{_n(g, 'behind')} behind upstream — pull/rebase"))
        if _n(g, "ahead") > 0:
            out.append(("info", f"{_n(g, 'ahead')} ahead — push"))
        if _n(g, "stale_branches") > 0:
            out.append(("amber", f"{_n(g, 'stale_branches')} stale branch(es) — prune"))
        if _n(g, "unmerged_agent_branches") > 0:
            out.append(("amber", f"{_n(g, 'unmerged_agent_branches')} unmerged agent "
                                 "branch(es) — review or delete"))
        if _n(g, "orphan_worktrees") > 0:
            out.append(("amber", f"{_n(g, 'orphan_worktrees')} orphan worktree(s) — prune"))
        try:
            blob = float(g["largest_blob_mb"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            blob = 0.0
        if blob >= BLOB_RED_MB:
            out.append(("red", f"largest blob {blob:.0f} MB — move to git-lfs"))
        if _n(g, "force_pushes_7d") > 0:
            out.append(("red", f"{_n(g, 'force_pushes_7d')} force push(es) in 7d — check history"))
    h = github_row
    if h is not None:
        if _n(h, "failing_checks") > 0:
            out.append(("red", f"{_n(h, 'failing_checks')} failing check(s) — fix CI"))
        if _n(h, "security_alerts") > 0:
            out.append(("red", f"{_n(h, 'security_alerts')} security alert(s) — patch"))
        if _n(h, "oldest_pr_days") > OLD_PR_AMBER_DAYS:
            out.append(("amber", f"oldest PR {_n(h, 'oldest_pr_days')}d old — review"))
        if _n(h, "dependabot_open") > 0:
            out.append(("amber", f"{_n(h, 'dependabot_open')} dependabot PR(s) — merge"))
        if _n(h, "open_prs") > 0:
            out.append(("info", f"{_n(h, 'open_prs')} open PR(s)"))
        if _n(h, "issues_open") > 0:
            out.append(("info", f"{_n(h, 'issues_open')} open issue(s)"))
    rank = {"red": 0, "amber": 1, "info": 2}
    out.sort(key=lambda s: rank[s[0]])
    return out


def _blob_mb(g) -> float:
    try:
        return float(g["largest_blob_mb"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0


def suggestions(git_row=None, github_row=None) -> list[dict]:
    """Synthesize the raw counts into a short, prioritized list of *what to do next*.

    Where `signals()` emits one chip per non-zero stat (the firehose), this groups related
    stats into a handful of framed recommendations — "Patch dependencies" folds security
    alerts + dependabot PRs into one action — so the operator sees guidance, not a pile of
    numbers. Each item is {level, text}, ordered red -> amber -> info. Empty means healthy.
    """
    g, h = git_row, github_row
    out: list[dict] = []

    # --- red: things actively blocking or dangerous ------------------------------------
    if h is not None:
        checks = _n(h, "failing_checks")
        if checks > 0:
            out.append({"level": "red",
                        "text": f"Fix CI — {checks} failing check(s) are blocking merges. "
                                "Start here before reviewing anything else."})
        alerts = _n(h, "security_alerts")
        bot = _n(h, "dependabot_open")
        if alerts > 0:
            extra = f" ({bot} dependabot PR(s) already waiting)" if bot else ""
            out.append({"level": "red",
                        "text": f"Patch dependencies — {alerts} open security alert(s){extra}. "
                                "Review and merge the fixes."})
    if g is not None:
        blob = _blob_mb(g)
        fp = _n(g, "force_pushes_7d")
        if blob >= BLOB_RED_MB and fp > 0:
            out.append({"level": "red",
                        "text": f"Audit repo history — a {blob:.0f} MB blob is bloating the repo "
                                f"and there were {fp} force-push(es) in 7d. Move large files to "
                                "git-lfs and confirm the rewrites were intentional."})
        elif blob >= BLOB_RED_MB:
            out.append({"level": "red",
                        "text": f"Slim the repo — largest blob is {blob:.0f} MB. Move it to "
                                "git-lfs so clones stay fast."})
        elif fp > 0:
            out.append({"level": "red",
                        "text": f"Check history — {fp} force-push(es) in the last 7 days. "
                                "Confirm none clobbered shared work."})

    # --- amber: worth attention this week ----------------------------------------------
    if h is not None:
        prs = _n(h, "open_prs")
        old = _n(h, "oldest_pr_days")
        agent = _n(h, "agent_prs")
        bot = _n(h, "dependabot_open")
        alerts = _n(h, "security_alerts")
        if old > OLD_PR_AMBER_DAYS:
            bits = f"{prs} open" + (f", {agent} from agents" if agent else "")
            out.append({"level": "amber",
                        "text": f"Work down the PR backlog — oldest is {old}d old ({bits}). "
                                "Merge or close the stale ones."})
        # dependabot only gets its own line when it isn't already folded into a security
        # "Patch dependencies" red above (which only exists when there are alerts).
        if bot > 0 and alerts == 0:
            out.append({"level": "amber",
                        "text": f"Merge {bot} dependabot PR(s) to clear pending dependency updates."})

    if g is not None:
        behind = _n(g, "behind")
        ahead = _n(g, "ahead")
        if behind > 0:
            tail = f" (and push your {ahead} ahead)" if ahead else ""
            out.append({"level": "amber",
                        "text": f"Sync with upstream — {behind} commit(s) behind{tail}. "
                                "Pull/rebase before you branch further."})
        elif ahead >= AHEAD_AMBER:
            out.append({"level": "amber",
                        "text": f"Push your work — {ahead} commit(s) ahead of upstream and not "
                                "backed up. Push before the next big change."})
        # branch & working-tree hygiene fold into one cleanup suggestion. A single stale
        # branch is routine, so only *trigger* on a real pile (or agent/orphan cruft, or a
        # genuinely dirty tree); once triggered, list everything non-zero as detail.
        dirty = _n(g, "dirty_files")
        stale = _n(g, "stale_branches")
        agentbr = _n(g, "unmerged_agent_branches")
        orphan = _n(g, "orphan_worktrees")
        trigger = (dirty > DIRTY_AMBER or agentbr > 0 or orphan > 0
                   or stale >= STALE_BRANCH_AMBER)
        if trigger:
            clutter = []
            if dirty > DIRTY_AMBER:
                clutter.append(f"{dirty} uncommitted files")
            if agentbr:
                clutter.append(f"{agentbr} unmerged agent branch(es)")
            if stale:
                clutter.append(f"{stale} stale branch(es)")
            if orphan:
                clutter.append(f"{orphan} orphan worktree(s)")
            out.append({"level": "amber",
                        "text": "Tidy the workspace — " + ", ".join(clutter) +
                                ". Commit/stash, then prune what's merged or dead."})

    # --- info: nudge, not a chore ------------------------------------------------------
    if not out:
        if g is None and h is None:
            out.append({"level": "info",
                        "text": "No git/GitHub data collected yet — run a collect pass to populate."})
        else:
            ahead = _n(g, "ahead") if g is not None else 0
            if ahead > 0:
                out.append({"level": "info",
                            "text": f"Healthy — just {ahead} commit(s) waiting to push."})
            else:
                out.append({"level": "info", "text": "Healthy — nothing needs attention."})
    return out


def level_of(sigs: list[tuple[str, str]]) -> str:
    levels = {s[0] for s in sigs}
    if "red" in levels:
        return "red"
    if "amber" in levels:
        return "amber"
    return "green"


def level_of_suggestions(sug: list[dict]) -> str:
    """Roll-up level from the SAME suggestions the card body shows, so a repo's dot/headline can
    never contradict its advice (the info level reads as green — nothing to act on)."""
    levels = {s["level"] for s in sug}
    if "red" in levels:
        return "red"
    if "amber" in levels:
        return "amber"
    return "green"

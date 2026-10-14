"""Receipt-history intelligence -- mine the corpus, not just the tip.

The brief reads the *latest* receipt per (project, cadence). But every verdict Foreman has
ever collected is sitting in git, and that history answers questions a single verdict can't:
is this loop **getting worse**? Did it just **regress**? Is it **flapping** (a flaky check,
not a real problem)? Is it **chronically** stuck? At the current rate, when does it go red?

This module is the one feature a competitor can't copy without first building Foreman's
receipt contract -- the insight is a property of the corpus, and the corpus is the moat. It
reads receipts directly (no index needed) and is pure/deterministic given ``now``.

Signal taxonomy (severity R > A > info), per pair and rolled up across the fleet:

- ``regression``  (R) a worse verdict transition at the tip (green->red, green->amber, amber->red)
- ``chronic``     (R/A) the same red/amber verdict for many consecutive runs -- stuck, not new
- ``degrading``   (A) the verdict trend over the window is worsening, though not yet red
- ``projected``   (A) at the current worsening rate, extrapolates to red within the horizon
- ``flapping``    (A) many verdict changes in the window -- unstable, suspect a flaky probe
- ``recovering``  (info) improving after prior reds/ambers -- a positive signal worth seeing

CLI::

    foreman insights                      # the fleet roll-up + per-pair signals
    foreman insights --window-days 14
    foreman insights --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from collectors.brief import load_receipts, _parse

ROOT = Path(__file__).resolve().parent.parent

# Verdict as an ordinal so "worse" is arithmetic. Unknown/None sits at green (absence of a bad
# signal is not itself bad -- staleness is the brief's job, not a trend).
_ORD = {"green": 0, "amber": 1, "red": 2}
_NAME = {0: "green", 1: "amber", 2: "red"}
_SEV_RANK = {"R": 0, "A": 1, "info": 2}


def _ord(receipt: dict) -> int:
    v = receipt.get("verdict")
    if v in _ORD:
        return _ORD[v]
    # A failed/timeout run with no verdict is effectively red for trend purposes.
    if receipt.get("status") in ("failed", "timeout"):
        return 2
    return 0


# ------------------------------------------------------------------- primitives

def transitions(history: list[dict]) -> list[tuple[int, int, str]]:
    """Verdict changes as (from_ord, to_ord, ended_iso), oldest first."""
    out = []
    prev = None
    for r in history:
        o = _ord(r)
        if prev is not None and o != prev:
            out.append((prev, o, r.get("ended", "")))
        prev = o
    return out


def current_streak(history: list[dict]) -> tuple[int, int, str | None]:
    """(verdict_ord, length, since_iso) of the unbroken run of the tip verdict."""
    if not history:
        return (0, 0, None)
    tip = _ord(history[-1])
    n, since = 0, history[-1].get("ended")
    for r in reversed(history):
        if _ord(r) == tip:
            n += 1
            since = r.get("ended")
        else:
            break
    return (tip, n, since)


def slope_per_day(points: list[tuple[float, float]]) -> float:
    """Least-squares slope (value units per day) of (epoch_seconds, value) points.

    Zero for fewer than two distinct timestamps -- a flat read, never a divide-by-zero.
    """
    if len(points) < 2:
        return 0.0
    xs = [t / 86400.0 for t, _ in points]
    ys = [v for _, v in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _window(history: list[dict], now: dt.datetime, window_days: int) -> list[dict]:
    cutoff = now - dt.timedelta(days=window_days)
    out = []
    for r in history:
        try:
            if _parse(r["ended"]) >= cutoff:
                out.append(r)
        except (KeyError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- per-pair scan

def analyze_history(history: list[dict], now: dt.datetime, *, window_days: int = 30,
                    horizon_days: int = 14, chronic_runs: int = 4) -> list[dict]:
    """Signals for one (project, cadence). Returns a list of insight dicts (may be empty)."""
    win = _window(history, now, window_days)
    if len(win) < 2:
        return []                       # need history to say anything about a trend
    signals: list[dict] = []
    tip_ord, streak_len, since = current_streak(win)
    trs = transitions(win)

    # regression: the most recent transition made things worse, and it is recent (at the tip run)
    if trs:
        f, t, when = trs[-1]
        if t > f and _ord(win[-1]) == t:
            sev = "R" if t == 2 else "A"
            signals.append({"type": "regression", "severity": sev,
                            "headline": f"regressed {_NAME[f]}→{_NAME[t]}",
                            "detail": f"was {_NAME[f]}, now {_NAME[t]} as of {when[:10]}"})

    # chronic: stuck on the same bad verdict across many consecutive runs
    if tip_ord >= 1 and streak_len >= chronic_runs:
        sev = "R" if tip_ord == 2 else "A"
        signals.append({"type": "chronic", "severity": sev,
                        "headline": f"chronic {_NAME[tip_ord]} ({streak_len} runs)",
                        "detail": f"{_NAME[tip_ord]} for {streak_len} consecutive runs "
                                  f"since {(since or '')[:10]}"})

    # trend over the window on the verdict ordinal
    pts = [(_parse(r["ended"]).timestamp(), float(_ord(r))) for r in win if r.get("ended")]
    slope = slope_per_day(pts)
    if slope > 0.01 and tip_ord < 2 and not any(s["type"] == "regression" for s in signals):
        signals.append({"type": "degrading", "severity": "A",
                        "headline": "degrading trend",
                        "detail": f"verdict worsening at ~{slope:.2f}/day over {window_days}d"})
    if slope > 0.01 and tip_ord < 2:
        days_to_red = (2.0 - tip_ord) / slope
        if days_to_red <= horizon_days:
            signals.append({"type": "projected", "severity": "A",
                            "headline": f"projected red in ~{int(round(days_to_red))}d",
                            "detail": "linear extrapolation of the current trend (estimate)"})

    # flapping: many changes in the window -> likely a flaky probe, not steady decline
    if len(trs) >= 3:
        signals.append({"type": "flapping", "severity": "A",
                        "headline": f"flapping ({len(trs)} changes)",
                        "detail": f"{len(trs)} verdict changes in {window_days}d — "
                                  "suspect a flaky check"})

    # recovering: improving, and currently clean after prior trouble -- a positive signal
    if slope < -0.01 and tip_ord == 0 and any(_ord(r) >= 1 for r in win[:-1]):
        signals.append({"type": "recovering", "severity": "info",
                        "headline": "recovering",
                        "detail": f"back to green after earlier trouble in the last {window_days}d"})

    return signals


def metric_movers(history: list[dict], now: dt.datetime, *, window_days: int = 30) -> list[dict]:
    """Per-metric first->last movement in the window, for the supplementary detail view.

    Deliberately does not label a direction good or bad (metric polarity is cadence-specific);
    it reports the movement so the operator can read it against the verdict trend.
    """
    win = _window(history, now, window_days)
    series: dict[str, list[tuple[float, float]]] = {}
    for r in win:
        if not r.get("ended"):
            continue
        t = _parse(r["ended"]).timestamp()
        for name, val in (r.get("metrics") or {}).items():
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            series.setdefault(name, []).append((t, float(val)))
    out = []
    for name, pts in series.items():
        if len(pts) < 2:
            continue
        pts.sort()
        first, last = pts[0][1], pts[-1][1]
        if first == last:
            continue
        out.append({"metric": name, "first": first, "last": last,
                    "slope_per_day": round(slope_per_day(pts), 4)})
    return out


# --------------------------------------------------------------- fleet roll-up

def fleet_insights(state_dir: Path, foreman_dir: Path, now: dt.datetime | None = None,
                   *, window_days: int = 30) -> dict:
    """All per-pair signals plus a fleet summary. The dashboard/CLI headline comes from here."""
    now = now or dt.datetime.now(dt.timezone.utc)
    groups = load_receipts(Path(state_dir))
    pairs: list[dict] = []
    for (project, cadence), history in groups.items():
        sigs = analyze_history(history, now, window_days=window_days)
        if not sigs:
            continue
        sigs.sort(key=lambda s: _SEV_RANK[s["severity"]])
        pairs.append({"project": project, "cadence": cadence, "signals": sigs,
                      "severity": sigs[0]["severity"],
                      "movers": metric_movers(history, now, window_days=window_days)})
    pairs.sort(key=lambda p: (_SEV_RANK[p["severity"]], p["project"], p["cadence"]))

    def count(stype: str) -> int:
        return sum(1 for p in pairs for s in p["signals"] if s["type"] == stype)

    # Posture by cadence family: how many loops of each kind are worse vs the window start.
    posture: dict[str, dict] = {}
    for p in pairs:
        fam = p["cadence"]
        worse = any(s["type"] in ("regression", "degrading", "chronic", "projected")
                    for s in p["signals"])
        d = posture.setdefault(fam, {"worse": 0, "total": 0})
        d["total"] += 1
        d["worse"] += 1 if worse else 0

    summary = {
        "window_days": window_days,
        "regressions": count("regression"),
        "degrading": count("degrading"),
        "chronic": count("chronic"),
        "projected": count("projected"),
        "flapping": count("flapping"),
        "recovering": count("recovering"),
        "posture": posture,
    }
    return {"summary": summary, "pairs": pairs, "now": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


# -------------------------------------------------------------------- rendering

def headlines(result: dict) -> list[str]:
    """One-line fleet headlines, worst first -- what the brief/notification would lead with."""
    s = result["summary"]
    out = []
    if s["regressions"]:
        out.append(f"{s['regressions']} loop(s) regressed this window")
    if s["chronic"]:
        out.append(f"{s['chronic']} loop(s) chronically stuck")
    if s["projected"]:
        out.append(f"{s['projected']} loop(s) projected to go red within the horizon")
    if s["degrading"]:
        out.append(f"{s['degrading']} loop(s) on a worsening trend")
    if s["flapping"]:
        out.append(f"{s['flapping']} loop(s) flapping (suspect flaky checks)")
    for fam, d in sorted(s["posture"].items()):
        if d["worse"] >= 2:
            out.append(f"{fam}: {d['worse']}/{d['total']} loops worse than {s['window_days']}d ago")
    if s["recovering"]:
        out.append(f"{s['recovering']} loop(s) recovering")
    return out


def render_text(result: dict) -> str:
    lines = [f"INSIGHTS (last {result['summary']['window_days']}d)"]
    hl = headlines(result)
    if not hl and not result["pairs"]:
        return lines[0] + "\n  (not enough history yet — trends need ≥2 runs per loop)"
    for h in hl:
        lines.append(f"  • {h}")
    if result["pairs"]:
        lines.append("")
        for p in result["pairs"]:
            sev = p["severity"]
            heads = "; ".join(s["headline"] for s in p["signals"])
            lines.append(f"  {sev}  {p['project']} / {p['cadence']}: {heads}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="insights")
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.state_dir:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        return 2
    result = fleet_insights(Path(os.path.expanduser(args.state_dir)),
                            Path(os.path.expanduser(args.foreman_dir)),
                            window_days=args.window_days)
    print(json.dumps(result, indent=2) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

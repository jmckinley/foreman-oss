"""Outbound notifications -- push what needs you to where you already are.

A supervisor you have to remember to open is a report, not a supervisor. This module turns
the board's two most time-sensitive signals -- a run that went **red**, and an operator
action **queued** for approval -- into a message delivered to Slack, Discord, a generic
webhook, or a local file, with a deep link back into the dashboard.

Design, consistent with the invariants:

- **No secrets committed.** Channel config comes from the environment, never ``registry.yaml``
  (webhook URLs are secrets). ``FOREMAN_NOTIFY_WEBHOOK`` is the one required knob; the channel
  shape (slack / discord / generic JSON) is auto-detected from the URL or set explicitly with
  ``FOREMAN_NOTIFY_CHANNEL``. ``file:`` / ``stdout`` targets need no secret and are the default
  for a local-only operator.
- **The index is a cache.** The dedup watermark (what has already been pushed) lives in the
  spool at ``notify-state.json`` -- ephemeral, like a telemetry partial. Losing it re-sends at
  worst a few duplicates; it is never the source of truth. Receipts remain the record.
- **Foreman never does the work.** This only reads receipts + the queue and emits a message.
  The inline actions in a Slack message are *links into the dashboard*, where the existing
  approval gate still applies -- a notification can never push a branch on its own.

CLI::

    foreman notify test                 # send a sample message to the configured channel
    foreman notify scan                 # push any un-notified reds / queued approvals
    foreman notify scan --dry-run        # print what would be sent, change nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from collectors.brief import analyze_pair, load_receipts, _cadence_meta, _summary

ROOT = Path(__file__).resolve().parent.parent
_ISO = "%Y-%m-%dT%H:%M:%SZ"

# Decision kinds whose whole point is a human call -- the ones most worth a ping. Everything
# else (notes, answers) is delivered into a session and does not need to interrupt you.
_APPROVAL_KINDS = {"approve_push"}


# ----------------------------------------------------------------------- config

def channel_config(env=None) -> dict:
    """Resolve the notification target from the environment. Returns ``{kind, target}``.

    ``kind`` is one of slack / discord / generic / file / stdout / none. A missing webhook with
    no file target yields ``none`` -- the scan then no-ops rather than erroring, so wiring
    notifications is strictly opt-in.
    """
    env = env if env is not None else os.environ
    url = (env.get("FOREMAN_NOTIFY_WEBHOOK") or "").strip()
    explicit = (env.get("FOREMAN_NOTIFY_CHANNEL") or "").strip().lower()
    file_target = (env.get("FOREMAN_NOTIFY_FILE") or "").strip()

    if explicit == "stdout" or (not url and not file_target and explicit == "stdout"):
        return {"kind": "stdout", "target": None}
    if file_target or explicit == "file":
        return {"kind": "file", "target": file_target or str(ROOT / "spool" / "notify.log")}
    if not url:
        return {"kind": "none", "target": None}
    if explicit in ("slack", "discord", "generic"):
        return {"kind": explicit, "target": url}
    low = url.lower()
    if "hooks.slack.com" in low:
        return {"kind": "slack", "target": url}
    if "discord.com/api/webhooks" in low or "discordapp.com/api/webhooks" in low:
        return {"kind": "discord", "target": url}
    return {"kind": "generic", "target": url}


def dashboard_url(env=None) -> str:
    env = env if env is not None else os.environ
    return (env.get("FOREMAN_DASHBOARD_URL") or "http://127.0.0.1:8787").rstrip("/")


# -------------------------------------------------------------------- watermark

def _watermark_path(spool_dir: Path) -> Path:
    return spool_dir / "notify-state.json"


def load_watermark(spool_dir: Path) -> dict:
    p = _watermark_path(spool_dir)
    if not p.is_file():
        return {"sent": {}}
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict) and isinstance(data.get("sent"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"sent": {}}


def save_watermark(spool_dir: Path, wm: dict) -> None:
    spool_dir.mkdir(parents=True, exist_ok=True)
    _watermark_path(spool_dir).write_text(json.dumps(wm, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- build events

def build_events(state_dir: Path, foreman_dir: Path, now: dt.datetime | None = None) -> list[dict]:
    """The notifiable events on the board right now, newest first.

    Two kinds: ``red`` (a pair whose effective state is red -- a real red verdict, a failed/
    timeout run, or stale-by-absence) and ``approval`` (a decision awaiting the human gate).
    Each event carries a stable ``id`` so the caller can dedup against the watermark.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    groups = load_receipts(Path(state_dir))
    cadences = _cadence_meta(Path(foreman_dir))
    events: list[dict] = []

    for (project, cadence), history in groups.items():
        a = analyze_pair(history, cadences.get(cadence), now)
        if a["severity"] != "R":
            continue
        latest = a["latest"] or {}
        # A stale pair has no tip run_id; key the event on the pair + the day so a dead
        # scheduler nudges once a day rather than silently or every tick.
        ident = latest.get("run_id") or f"stale:{project}/{cadence}:{now:%Y-%m-%d}"
        events.append({
            "kind": "red",
            "id": ident,
            "project": project,
            "cadence": cadence,
            "label": a["label"],
            "reason": a["reason"] or _summary(latest),
            "next_action": latest.get("next_action", ""),
        })

    # Queued approvals: read the decision queue directly (no index needed).
    qroot = Path(state_dir) / "queue"
    if qroot.is_dir():
        for path in sorted(qroot.glob("*/*.json")):
            try:
                dec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if dec.get("status") != "pending" or dec.get("kind") not in _APPROVAL_KINDS:
                continue
            payload = dec.get("payload") or {}
            events.append({
                "kind": "approval",
                "id": dec["decision_id"],
                "project": dec.get("project", "?"),
                "cadence": payload.get("cadence", dec.get("kind")),
                "label": "awaiting approval",
                "reason": f"{dec['kind']}: push {payload.get('branch', '?')} "
                          f"-> {payload.get('base', '?')}",
                "next_action": "approve or dismiss in the dashboard",
            })

    events.sort(key=lambda e: e["id"], reverse=True)
    return events


# ------------------------------------------------------------- render a message

def _emoji(kind: str) -> str:
    return "🔴" if kind == "red" else "⏳"


def render_text(event: dict, base_url: str) -> str:
    """Plain-text form -- used by file/stdout/generic and as Slack/Discord fallback text."""
    head = f"{_emoji(event['kind'])} {event['project']} / {event['cadence']} — {event['label']}"
    lines = [head]
    if event.get("reason"):
        lines.append(event["reason"])
    if event.get("next_action"):
        lines.append(f"→ {event['next_action']}")
    anchor = "queued" if event["kind"] == "approval" else "needs-you"
    lines.append(f"{base_url}/#{anchor}")
    return "\n".join(lines)


def render_slack(event: dict, base_url: str) -> dict:
    anchor = "queued" if event["kind"] == "approval" else "needs-you"
    btn_text = "Review approval" if event["kind"] == "approval" else "Open board"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{_emoji(event['kind'])} *{event['project']} / {event['cadence']}* — "
                    f"{event['label']}"}},
    ]
    detail = []
    if event.get("reason"):
        detail.append(event["reason"])
    if event.get("next_action"):
        detail.append(f"→ {event['next_action']}")
    if detail:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(detail)}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": btn_text},
         "url": f"{base_url}/#{anchor}"}]})
    # text is the required fallback (notifications, screen readers).
    return {"text": render_text(event, base_url), "blocks": blocks}


def render_discord(event: dict, base_url: str) -> dict:
    return {"content": render_text(event, base_url)}


# ------------------------------------------------------------------- transport

def _post(url: str, payload: dict, timeout: float = 10.0) -> None:
    import httpx
    resp = httpx.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()


def deliver(event: dict, config: dict, base_url: str) -> str:
    """Send one event over the configured channel. Returns a short result string; raises on a
    real transport failure so the caller leaves it un-watermarked and retries next scan."""
    kind, target = config["kind"], config["target"]
    if kind == "stdout":
        print(render_text(event, base_url))
        return "stdout"
    if kind == "file":
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(render_text(event, base_url) + "\n\n")
        return f"file:{target}"
    if kind in ("slack", "discord", "generic"):
        payload = render_payload(event, kind, base_url)
        _post(target, payload)
        return kind
    raise ValueError(f"no channel configured (kind={kind})")


# ------------------------------------------------------------- scan + dispatch

def render_payload(event: dict, kind: str, base_url: str) -> dict:
    if kind == "slack":
        return render_slack(event, base_url)
    if kind == "discord":
        return render_discord(event, base_url)
    return {"event": event, "url": f"{base_url}/#"
            + ("queued" if event["kind"] == "approval" else "needs-you")}


def scan_and_notify(state_dir, foreman_dir, spool_dir, *, now=None, env=None,
                    dry_run: bool = False) -> dict:
    """Push every notifiable event not already sent. Idempotent via the spool watermark.

    No-ops (and never raises) when no channel is configured, so it is safe to call
    unconditionally at the end of a collect run.
    """
    env = env if env is not None else os.environ
    state_dir, foreman_dir, spool_dir = Path(state_dir), Path(foreman_dir), Path(spool_dir)
    config = channel_config(env)
    base = dashboard_url(env)
    events = build_events(state_dir, foreman_dir, now=now)
    if config["kind"] == "none":
        return {"channel": "none", "events": len(events), "sent": [], "skipped": len(events)}

    wm = load_watermark(spool_dir)
    sent, failed = [], []
    for ev in events:
        if ev["id"] in wm["sent"]:
            continue
        if dry_run:
            sent.append(ev["id"])
            continue
        try:
            deliver(ev, config, base)
            wm["sent"][ev["id"]] = (now or dt.datetime.now(dt.timezone.utc)).strftime(_ISO)
            sent.append(ev["id"])
        except Exception as exc:          # transport hiccup: leave un-watermarked, retry later
            failed.append({"id": ev["id"], "error": f"{type(exc).__name__}: {exc}"})
    if not dry_run and sent:
        # Prune watermark entries older than 30d so it cannot grow unbounded.
        _prune(wm, now)
        save_watermark(spool_dir, wm)
    return {"channel": config["kind"], "events": len(events), "sent": sent,
            "failed": failed, "skipped": len(events) - len(sent) - len(failed)}


def _prune(wm: dict, now: dt.datetime | None) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=30)
    for ident, stamp in list(wm["sent"].items()):
        try:
            when = dt.datetime.strptime(stamp, _ISO).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            del wm["sent"][ident]


# ------------------------------------------------------------------------- CLI

def _sample_event() -> dict:
    return {"kind": "red", "id": "sample", "project": "example", "cadence": "security-review",
            "label": "red 2d", "reason": "2 high-severity findings unresolved",
            "next_action": "triage the findings in issue #42"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="notify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "test"):
        sp = sub.add_parser(name)
        sp.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR"))
        sp.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
        sp.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL") or str(ROOT / "spool"))
        if name == "scan":
            sp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    config = channel_config()
    base = dashboard_url()
    if args.cmd == "test":
        if config["kind"] == "none":
            print("no channel configured — set FOREMAN_NOTIFY_WEBHOOK or FOREMAN_NOTIFY_FILE",
                  file=sys.stderr)
            return 1
        result = deliver(_sample_event(), config, base)
        print(f"sent sample via {result}")
        return 0

    if not args.state_dir:
        print("error: --state-dir or FOREMAN_STATE_DIR is required", file=sys.stderr)
        return 2
    out = scan_and_notify(args.state_dir, args.foreman_dir, args.spool, dry_run=args.dry_run)
    verb = "would send" if args.dry_run else "sent"
    print(f"channel={out['channel']} · {len(out['sent'])} {verb} · "
          f"{out['skipped']} already-sent · {len(out.get('failed', []))} failed")
    for f in out.get("failed", []):
        print(f"  ! {f['id']}: {f['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

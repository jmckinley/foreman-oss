"""C2 -- telemetry from Claude Code's OTLP export, rolled into ``telemetry_day``.

Claude Code exports metrics and events over OTLP. The receiver accepts OTLP/HTTP JSON and
appends each payload to an append-only spool; the drain recomputes ``telemetry_day`` from
the whole spool, so the rollup is idempotent and the index stays a rebuildable cache
(SPEC.md sections 1, 9). Claude Code reads telemetry config only at startup, so an env push
takes effect on the next session.

Metrics consumed: session count, cost, tokens, lines added/removed, commits, PRs, edit
accept/reject, active time. IO (the HTTP receiver) is thin; the parsing and rollup are pure
and testable. Set ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json`` so payloads arrive as JSON.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from collectors import db

# OTLP metric name -> the telemetry_day column it feeds. Lines and edit decisions split by
# an attribute (type / decision), handled in _fold.
METRIC_COLUMN = {
    "claude_code.session.count": "sessions",
    "claude_code.cost.usage": "cost_usd",
    "claude_code.token.usage": "tokens",
    "claude_code.commit.count": "commits",
    "claude_code.pull_request.count": "prs",
    "claude_code.active_time.total": "active_seconds",
}
SUM_COLUMNS = ["sessions", "cost_usd", "tokens", "lines_added", "lines_removed",
               "commits", "prs", "edit_accept", "edit_reject", "active_seconds"]


def _dp_value(dp: dict):
    if "asInt" in dp:
        return float(dp["asInt"])
    if "asDouble" in dp:
        return float(dp["asDouble"])
    return 0.0


def _attrs(items) -> dict:
    """OTLP attributes ([{key, value:{stringValue|intValue|...}}]) -> flat dict."""
    out = {}
    for a in items or []:
        v = a.get("value") or {}
        out[a.get("key")] = next((v[k] for k in
                                  ("stringValue", "intValue", "doubleValue", "boolValue") if k in v), None)
    return out


def parse_metrics(payload: dict) -> list[dict]:
    """Flatten OTLP/JSON metrics into records: name, value, ts_nano, and merged attributes."""
    records = []
    for rm in payload.get("resourceMetrics") or []:
        r_attrs = _attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics") or []:
            for metric in sm.get("metrics") or []:
                name = metric.get("name")
                points = ((metric.get("sum") or metric.get("gauge") or {}).get("dataPoints")) or []
                for dp in points:
                    records.append({
                        "name": name,
                        "value": _dp_value(dp),
                        "ts_nano": int(dp.get("timeUnixNano") or 0),
                        "attrs": {**r_attrs, **_attrs(dp.get("attributes"))},
                    })
    return records


def _day(ts_nano: int) -> str:
    import datetime as dt
    if not ts_nano:
        return "unknown"
    return dt.datetime.fromtimestamp(ts_nano / 1e9, dt.timezone.utc).strftime("%Y-%m-%d")


def _key(rec: dict) -> tuple:
    a = rec["attrs"]
    return (_day(rec["ts_nano"]), a.get("foreman.project") or a.get("project") or "unknown",
            a.get("foreman.host") or a.get("host") or "unknown", a.get("model") or "unknown")


def _blank() -> dict:
    return {c: 0.0 for c in SUM_COLUMNS}


def _fold(agg: dict, rec: dict) -> None:
    name, val, a = rec["name"], rec["value"], rec["attrs"]
    if name in METRIC_COLUMN:
        agg[METRIC_COLUMN[name]] += val
    elif name == "claude_code.lines_of_code.count":
        agg["lines_added" if a.get("type") == "added" else "lines_removed"] += val
    elif name == "claude_code.code_edit_tool.decision":
        agg["edit_accept" if a.get("decision") == "accept" else "edit_reject"] += val


def rollup(records: list[dict]) -> dict[tuple, dict]:
    """Aggregate metric records into telemetry_day rows keyed (day, project, host, model)."""
    days: dict[tuple, dict] = {}
    for rec in records:
        agg = days.setdefault(_key(rec), _blank())
        _fold(agg, rec)
    return days


# ------------------------------------------------------------------- spool + drain

def spool_path(spool_dir: Path) -> Path:
    return Path(spool_dir).expanduser() / "telemetry.jsonl"


def append_spool(spool_dir: Path, payload: dict) -> None:
    path = spool_path(spool_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


def drain(conn, spool_dir: Path) -> dict:
    """Recompute telemetry_day from the whole spool. Idempotent: re-draining replaces rows."""
    path = spool_path(spool_dir)
    records: list[dict] = []
    if path.is_file():
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.extend(parse_metrics(json.loads(line)))
                except json.JSONDecodeError:
                    continue
    days = rollup(records)
    for (day, project, host, model), agg in days.items():
        row = {"day": day, "project": project, "host": host, "model": model,
               "sessions": int(agg["sessions"]), "cost_usd": agg["cost_usd"],
               "tokens": int(agg["tokens"]), "lines_added": int(agg["lines_added"]),
               "lines_removed": int(agg["lines_removed"]), "commits": int(agg["commits"]),
               "prs": int(agg["prs"]), "edit_accept": int(agg["edit_accept"]),
               "edit_reject": int(agg["edit_reject"]), "active_seconds": int(agg["active_seconds"])}
        db.upsert(conn, "telemetry_day", row, keys=["day", "project", "host", "model"])
    conn.commit()
    return {"records": len(records), "days": len(days)}


def spend_summary(conn, *, now, days: int = 30, recent_days: int = 7) -> dict:
    """Per-project cost/usage from telemetry_day over a window, plus a recent-window cost, a
    per-model breakdown, and fleet totals. Read-only; empty when no telemetry has been ingested
    (the OTLP receiver must be running and Claude Code exporting foreman.project attributes)."""
    since = (now - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    recent_since = (now - dt.timedelta(days=recent_days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT project, SUM(cost_usd) cost, SUM(tokens) tokens, SUM(sessions) sessions, "
        "COUNT(DISTINCT day) active_days FROM telemetry_day WHERE day >= ? "
        "GROUP BY project ORDER BY cost DESC", (since,)).fetchall()
    recent = {r["project"]: (r["cost"] or 0.0) for r in conn.execute(
        "SELECT project, SUM(cost_usd) cost FROM telemetry_day WHERE day >= ? GROUP BY project",
        (recent_since,)).fetchall()}
    projects = [{"project": r["project"], "cost": r["cost"] or 0.0, "tokens": r["tokens"] or 0,
                 "sessions": r["sessions"] or 0, "active_days": r["active_days"] or 0,
                 "cost_recent": recent.get(r["project"], 0.0)} for r in rows]
    by_model = [{"model": m["model"], "cost": m["cost"] or 0.0, "tokens": m["tokens"] or 0}
                for m in conn.execute(
                    "SELECT model, SUM(cost_usd) cost, SUM(tokens) tokens FROM telemetry_day "
                    "WHERE day >= ? GROUP BY model ORDER BY cost DESC", (since,)).fetchall()]
    total = {"cost": sum(p["cost"] for p in projects),
             "tokens": sum(p["tokens"] for p in projects),
             "sessions": sum(p["sessions"] for p in projects)}
    return {"since": since, "days": days, "recent_days": recent_days,
            "projects": projects, "by_model": by_model, "total": total}


# ---------------------------------------------------------------------- receiver

def serve(spool_dir: Path, host: str = "0.0.0.0", port: int = 4318) -> None:
    """A minimal OTLP/HTTP receiver: buffer payloads to the spool, drained separately."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - match base signature
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path.rstrip("/").endswith("/v1/metrics"):
                try:
                    append_spool(spool_dir, json.loads(body or b"{}"))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    ThreadingHTTPServer((host, port), Handler).serve_forever()


# ------------------------------------------------------ receiver as a service
#
# The receiver must outlive any one session -- it is the endpoint every host's Claude Code
# exports to. On mini (macOS) that is a launchd agent; on a Linux host (vps) a systemd user
# unit. Both keep it alive across restarts. The rollup is NOT a service: `collect` drains the
# spool idempotently, so the receiver only has to keep buffering. Unit generation is a pure
# string (testable without touching the host), mirroring scheduler.launchd_plist.

LABEL = "com.foreman.telemetry"


def receiver_launchd_plist(*, spool, port=4318, python=None) -> str:
    py = python or sys.executable
    repo = str(Path(__file__).resolve().parent.parent)
    log = f"{os.path.expanduser(str(spool))}/telemetry.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>-m</string><string>collectors.telemetry</string>
    <string>serve</string><string>--spool</string><string>{spool}</string>
    <string>--port</string><string>{port}</string></array>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>{repo}</string></dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def receiver_systemd_unit(*, spool, port=4318, python=None) -> str:
    py = python or sys.executable
    repo = str(Path(__file__).resolve().parent.parent)
    return f"""[Unit]
Description=Foreman telemetry (OTLP/HTTP) receiver
After=network.target

[Service]
Environment=PYTHONPATH={repo}
ExecStart={py} -m collectors.telemetry serve --spool {spool} --port {port}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_receiver(*, spool, port=4318) -> Path:
    """Install and start the receiver as a persistent service for this host's OS."""
    if sys.platform == "darwin":
        path = Path(os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(receiver_launchd_plist(spool=spool, port=port))
        import subprocess
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(path)], capture_output=True)
        return path
    path = Path(os.path.expanduser(f"~/.config/systemd/user/{LABEL}.service"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(receiver_systemd_unit(spool=spool, port=port))
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{LABEL}.service"],
                   capture_output=True)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("serve")
    ps.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL", "~/foreman/spool"))
    ps.add_argument("--port", type=int, default=4318)
    pd = sub.add_parser("drain")
    pd.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL", "~/foreman/spool"))
    pd.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    pi = sub.add_parser("install", help="install the receiver as a persistent service")
    pi.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL", "~/foreman/spool"))
    pi.add_argument("--port", type=int, default=4318)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        print(f"telemetry receiver on :{args.port}, spooling to {args.spool}", file=sys.stderr)
        serve(Path(args.spool), port=args.port)
        return 0
    if args.cmd == "drain":
        conn = db.open_index(args.index or str(db.default_path()))
        print(json.dumps(drain(conn, Path(args.spool))))
        return 0
    if args.cmd == "install":
        p = install_receiver(spool=args.spool, port=args.port)
        print(f"installed {LABEL} (receiver on :{args.port}): {p}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

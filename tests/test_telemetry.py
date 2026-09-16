"""OTLP metrics parsing, rollup, spool→index drain, and the receiver-unit generators."""
import json

from collectors import telemetry as T, db

# 2026-09-06 12:00:00Z in unix-nanoseconds
TS = 1757160000_000000000


def _metric(name, value, attrs=None, kind="sum"):
    dp = {"asDouble": value, "timeUnixNano": str(TS)}
    if attrs:
        dp["attributes"] = [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]
    return {name: {"name": name, kind: {"dataPoints": [dp]}}}


def _payload(*metrics, resource=None):
    res = {"attributes": [{"key": k, "value": {"stringValue": v}}
                          for k, v in (resource or {}).items()]}
    flat = [m for metric in metrics for m in metric.values()]   # keep duplicate-named metrics
    return {"resourceMetrics": [{"resource": res,
            "scopeMetrics": [{"metrics": flat}]}]}


def test_parse_metrics_flattens_attrs_and_values():
    p = _payload(_metric("claude_code.token.usage", 1000, {"model": "opus"}),
                 resource={"foreman.project": "alpha", "foreman.host": "mbp"})
    recs = T.parse_metrics(p)
    assert len(recs) == 1
    r = recs[0]
    assert r["name"] == "claude_code.token.usage" and r["value"] == 1000.0
    assert r["attrs"]["foreman.project"] == "alpha" and r["attrs"]["model"] == "opus"


def test_dp_value_handles_int_double_missing():
    assert T._dp_value({"asInt": "5"}) == 5.0
    assert T._dp_value({"asDouble": 2.5}) == 2.5
    assert T._dp_value({}) == 0.0


def test_rollup_folds_special_metrics():
    recs = T.parse_metrics(_payload(
        _metric("claude_code.session.count", 1),
        _metric("claude_code.cost.usage", 0.5),
        _metric("claude_code.lines_of_code.count", 30, {"type": "added"}),
        _metric("claude_code.lines_of_code.count", 4, {"type": "removed"}),
        _metric("claude_code.code_edit_tool.decision", 2, {"decision": "accept"}),
        _metric("claude_code.code_edit_tool.decision", 1, {"decision": "reject"}),
        resource={"foreman.project": "alpha", "foreman.host": "mbp", "model": "opus"}))
    (day, project, host, model), agg = next(iter(T.rollup(recs).items()))
    assert project == "alpha" and host == "mbp" and model == "opus" and day == T._day(TS)
    assert agg["sessions"] == 1 and agg["cost_usd"] == 0.5
    assert agg["lines_added"] == 30 and agg["lines_removed"] == 4
    assert agg["edit_accept"] == 2 and agg["edit_reject"] == 1


def test_key_falls_back_to_unknown():
    rec = {"name": "x", "value": 1.0, "ts_nano": 0, "attrs": {}}
    assert T._key(rec) == ("unknown", "unknown", "unknown", "unknown")


def test_drain_is_idempotent(tmp_path):
    spool = tmp_path / "spool"
    T.append_spool(spool, _payload(
        _metric("claude_code.token.usage", 100),
        resource={"foreman.project": "alpha", "foreman.host": "mbp", "model": "opus"}))
    T.append_spool(spool, {"not": "otlp"})            # junk line tolerated
    conn = db.open_index(":memory:")
    r1 = T.drain(conn, spool)
    assert r1["records"] == 1 and r1["days"] == 1
    row = conn.execute("SELECT tokens FROM telemetry_day WHERE project='alpha'").fetchone()
    assert row["tokens"] == 100
    # re-draining replaces, does not double
    T.drain(conn, spool)
    n = conn.execute("SELECT COUNT(*) c FROM telemetry_day WHERE project='alpha'").fetchone()["c"]
    assert n == 1


def test_drain_missing_spool_is_empty(tmp_path):
    conn = db.open_index(":memory:")
    assert T.drain(conn, tmp_path / "nope")["records"] == 0


def test_spend_summary():
    import datetime as dt
    conn = db.open_index(":memory:")

    def seed(day, project, cost, tokens, sessions, model="opus"):
        db.upsert(conn, "telemetry_day", {
            "day": day, "project": project, "host": "mbp", "model": model,
            "sessions": sessions, "cost_usd": cost, "tokens": tokens, "lines_added": 0,
            "lines_removed": 0, "commits": 0, "prs": 0, "edit_accept": 0, "edit_reject": 0,
            "active_seconds": 0}, keys=["day", "project", "host", "model"])

    now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)
    seed("2026-09-09", "alpha", 2.0, 1_000_000, 3)          # within 7d
    seed("2026-09-08", "alpha", 1.0, 500_000, 2, model="haiku")
    seed("2026-08-20", "alpha", 5.0, 9_000_000, 9)          # >7d, still within 30d
    seed("2026-09-07", "beta", 0.5, 100_000, 1)
    s = T.spend_summary(conn, now=now, days=30, recent_days=7)
    by = {p["project"]: p for p in s["projects"]}
    assert by["alpha"]["cost"] == 8.0 and by["alpha"]["cost_recent"] == 3.0   # 7d excludes Aug 20
    assert by["alpha"]["active_days"] == 3 and by["alpha"]["sessions"] == 14
    assert s["projects"][0]["project"] == "alpha"          # sorted by cost desc
    assert s["total"]["cost"] == 8.5
    assert {m["model"] for m in s["by_model"]} == {"opus", "haiku"}
    # nothing ingested -> empty, no crash
    assert T.spend_summary(db.open_index(":memory:"), now=now)["projects"] == []


def test_receiver_unit_generators():
    plist = T.receiver_launchd_plist(spool="~/foreman/spool", port=4318, python="/usr/bin/python3")
    assert T.LABEL in plist and "collectors.telemetry" in plist and "4318" in plist
    unit = T.receiver_systemd_unit(spool="/srv/spool", port=4319, python="/usr/bin/python3")
    assert "ExecStart=/usr/bin/python3 -m collectors.telemetry serve" in unit and "4319" in unit


def test_install_receiver_writes_unit(tmp_path, monkeypatch):
    # force the systemd branch and neutralise the subprocess calls so no service is touched
    monkeypatch.setattr(T.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
    path = T.install_receiver(spool=str(tmp_path / "spool"), port=4318)
    assert path.is_file() and path.name == f"{T.LABEL}.service"


def test_cli_drain(tmp_path, capsys):
    spool = tmp_path / "spool"
    T.append_spool(spool, _payload(_metric("claude_code.commit.count", 3),
                                   resource={"foreman.project": "alpha"}))
    idx = str(tmp_path / "index.db")
    assert T.main(["drain", "--spool", str(spool), "--index", idx]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 1

"""Receipt-history intelligence: the per-pair signal taxonomy (regression, chronic, degrading,
projected, flapping, recovering), the trend math, and the fleet roll-up headlines."""
import datetime as dt

from collectors import insights


NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


def _hist(seq):
    """seq is [(days_ago, verdict), ...] -> receipts oldest-first with metrics for one metric."""
    out = []
    for days_ago, verdict in seq:
        ended = (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"ended": ended, "verdict": verdict, "status": "ok",
                    "metrics": {"findings": {"green": 0, "amber": 2, "red": 5}[verdict]}})
    out.sort(key=lambda r: r["ended"])
    return out


def test_slope_per_day_zero_and_positive():
    assert insights.slope_per_day([(0.0, 1.0)]) == 0.0
    s = insights.slope_per_day([(0.0, 0.0), (86400.0, 1.0), (172800.0, 2.0)])
    assert round(s, 3) == 1.0


def test_regression_at_tip():
    h = _hist([(10, "green"), (5, "green"), (1, "red")])
    sigs = insights.analyze_history(h, NOW)
    types = {s["type"] for s in sigs}
    assert "regression" in types
    reg = [s for s in sigs if s["type"] == "regression"][0]
    assert reg["severity"] == "R" and "green→red" in reg["headline"]


def test_chronic_red_streak():
    h = _hist([(8, "red"), (6, "red"), (4, "red"), (2, "red"), (1, "red")])
    sigs = insights.analyze_history(h, NOW)
    chronic = [s for s in sigs if s["type"] == "chronic"]
    assert chronic and chronic[0]["severity"] == "R"


def test_degrading_and_projected():
    # steadily worsening but still amber at the tip -> degrading + a red projection
    h = _hist([(12, "green"), (9, "green"), (6, "amber"), (3, "amber"), (1, "amber")])
    sigs = insights.analyze_history(h, NOW, horizon_days=30)
    types = {s["type"] for s in sigs}
    assert "degrading" in types or "regression" in types
    assert "projected" in types


def test_flapping():
    h = _hist([(10, "green"), (8, "red"), (6, "green"), (4, "red"), (2, "green")])
    sigs = insights.analyze_history(h, NOW)
    assert any(s["type"] == "flapping" for s in sigs)


def test_recovering_is_info():
    h = _hist([(10, "red"), (7, "red"), (4, "amber"), (1, "green")])
    sigs = insights.analyze_history(h, NOW)
    rec = [s for s in sigs if s["type"] == "recovering"]
    assert rec and rec[0]["severity"] == "info"


def test_too_little_history_is_silent():
    assert insights.analyze_history(_hist([(1, "red")]), NOW) == []


def test_metric_movers():
    h = _hist([(10, "green"), (5, "amber"), (1, "red")])     # findings 0 -> 2 -> 5
    movers = insights.metric_movers(h, NOW)
    m = {x["metric"]: x for x in movers}["findings"]
    assert m["first"] == 0 and m["last"] == 5 and m["slope_per_day"] > 0


def test_fleet_rollup_and_headlines(state_dir, foreman_dir, make_receipt):
    # one regressing security loop, one chronic docs loop
    for days_ago, v in [(10, "green"), (5, "green"), (1, "red")]:
        ended = (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        make_receipt("acmeapi", "security-review", ended, v)
    for days_ago in (8, 6, 4, 2, 1):
        ended = (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        make_receipt("paysvc", "docs-sync", ended, "red")

    result = insights.fleet_insights(state_dir, foreman_dir, now=NOW)
    assert result["summary"]["regressions"] >= 1
    assert result["summary"]["chronic"] >= 1
    hl = insights.headlines(result)
    assert any("regressed" in h for h in hl)
    txt = insights.render_text(result)
    assert "INSIGHTS" in txt and "acmeapi / security-review" in txt

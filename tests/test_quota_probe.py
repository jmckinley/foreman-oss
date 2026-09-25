"""The FOREMAN_QUOTA_CMD probe (collectors.quota_probe) — sums billed tokens in the current 5-hour
window from local transcripts and reports headroom, or nothing when no plan budget is set."""
import datetime as dt
import json
import os

from collectors import quota_probe

NOW = dt.datetime(2026, 9, 25, 18, 0, tzinfo=dt.timezone.utc)


def _write_transcript(root, name, records):
    d = root / f"-proj-{name}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    # window_usage pre-filters by mtime against the (test-fixed) `now`; pin mtime into the window
    os.utime(p, (NOW.timestamp(), NOW.timestamp()))
    return p


def _usage_rec(ts, *, inp=0, out=0, cw=0, cr=0):
    return {"timestamp": ts, "message": {"usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}


def _iso(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_probe_none_without_limit(tmp_path):
    assert quota_probe.probe(root=tmp_path, limit=None, now=NOW) is None
    assert quota_probe.probe(root=tmp_path, limit=0, now=NOW) is None


def test_window_usage_sums_in_window_and_ignores_old(tmp_path):
    _write_transcript(tmp_path, "a", [
        _usage_rec(_iso(1), inp=1000, out=500, cw=200, cr=300),   # in window: 2000
        _usage_rec(_iso(6), inp=9999, out=9999),                  # 6h ago: outside 5h window
    ])
    _write_transcript(tmp_path, "b", [_usage_rec(_iso(2), inp=1000)])  # in window: 1000
    used, earliest = quota_probe.window_usage(tmp_path, now=NOW)
    assert used == 3000
    assert earliest == NOW - dt.timedelta(hours=2)                # oldest still inside the window


def test_probe_computes_pct_and_reset(tmp_path):
    _write_transcript(tmp_path, "a", [_usage_rec(_iso(1), inp=2500)])
    d = quota_probe.probe(root=tmp_path, limit=10000, now=NOW)
    assert d["pct_used"] == 25.0
    assert d["plan"]                                              # a plan label is always present
    # window clears 5h after the earliest in-window use (1h ago) -> 4h from now
    assert d["window_resets"] == (NOW + dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_probe_caps_at_100(tmp_path):
    _write_transcript(tmp_path, "a", [_usage_rec(_iso(1), inp=50000)])
    assert quota_probe.probe(root=tmp_path, limit=10000, now=NOW)["pct_used"] == 100.0


def test_probe_empty_is_zero_pct(tmp_path):
    d = quota_probe.probe(root=tmp_path, limit=10000, now=NOW)
    assert d["pct_used"] == 0.0 and "window_resets" not in d      # no activity -> no reset time


def test_output_feeds_the_quota_guard(tmp_path):
    # the JSON shape must be exactly what runstate.default_quota_fetch accepts
    _write_transcript(tmp_path, "a", [_usage_rec(_iso(1), inp=3000)])
    d = quota_probe.probe(root=tmp_path, limit=10000, now=NOW)
    assert set(("pct_used",)).issubset(d) and isinstance(d["pct_used"], float)

"""keyscan (shared-value grouping), dispatch (off-cycle enqueue + quota guard), and runstate
(quota read + receipt-less run reconciliation)."""
import datetime as dt
import json

import pytest

from collectors import keyscan, dispatch, runstate, decisions as D, quota, db


# --------------------------------------------------------------------- keyscan

def test_keyscan_groups_shared_values_without_secrets(tmp_path):
    reg = {"projects": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]}
    digests = {"a": {"API_KEY": "h1"}, "b": {"API_KEY": "h1"}, "c": {"API_KEY": "h2"}}
    data = keyscan.build_keyshare(reg, "mbp", digests_by_project=digests, taken="T")
    assert data["groups"] == [{"key": "API_KEY", "projects": ["a", "b"]}]   # c differs -> excluded
    blob = json.dumps(data)
    assert "h1" not in blob and "h2" not in blob                            # no hashes/values emitted
    path = keyscan.write_keyshare(tmp_path, data)
    assert path.exists()
    lookup = keyscan.load_lookup(tmp_path)
    assert lookup[("a", "API_KEY")] == ["b"] and lookup[("b", "API_KEY")] == ["a"]


def test_keyscan_load_lookup_absent(tmp_path):
    assert keyscan.load_lookup(tmp_path) == {}                              # no artifact -> empty


def test_keyscan_scan_reads_declared_only(tmp_path):
    wt = tmp_path / "proj"
    wt.mkdir()
    (wt / ".env").write_text("API_KEY=shared-value\nUNDECLARED=x\n")
    reg = {"projects": [{"slug": "a", "worktree": {"mbp": str(wt)}, "env_keys": ["API_KEY"]}]}
    scanned = keyscan._scan(reg, "mbp")
    assert set(scanned["a"]) == {"API_KEY"}                                 # undeclared key ignored


def test_keyscan_cli(tmp_path, capsys):
    wt = tmp_path / "proj"
    wt.mkdir()
    (wt / ".env").write_text("API_KEY=v\n")
    (tmp_path / "registry.yaml").write_text(
        "version: 1\nprojects:\n  - slug: a\n    worktree: {mbp: %s}\n    env_keys: [API_KEY]\n" % wt)
    assert keyscan.main(["--foreman-dir", str(tmp_path), "--host", "mbp"]) == 0
    assert "shared-value group" in capsys.readouterr().out
    assert (tmp_path / "data" / "keyshare.json").exists()


# --------------------------------------------------------------------- dispatch

_REG = {"projects": [{"slug": "alpha", "cadences": ["docs-sync"]}]}


def test_dispatch_enqueues(tmp_path):
    conn = db.open_index(":memory:")
    out = dispatch.dispatch(conn, _REG, tmp_path, "alpha", "docs-sync")
    assert out["deferred"] is False and out["dispatched"]
    assert any(dec["kind"] == "dispatch_cadence" for _, dec in D.load_pending(tmp_path, "alpha"))


def test_dispatch_unknown_project_or_cadence(tmp_path):
    conn = db.open_index(":memory:")
    with pytest.raises(ValueError):
        dispatch.dispatch(conn, _REG, tmp_path, "ghost", "docs-sync")
    with pytest.raises(ValueError):
        dispatch.dispatch(conn, _REG, tmp_path, "alpha", "not-enabled")


def test_dispatch_deferred_under_low_quota(tmp_path):
    conn = db.open_index(":memory:")
    quota.record(conn, pct_used=97, taken="2026-09-06T12:00:00Z")   # <15% headroom
    out = dispatch.dispatch(conn, _REG, tmp_path, "alpha", "docs-sync")
    assert out["deferred"] is True and "deferring" in out["reason"]
    assert not D.load_pending(tmp_path, "alpha")                    # nothing enqueued


def test_dispatch_cli(tmp_path, capsys):
    (tmp_path / "registry.yaml").write_text(
        "version: 1\nprojects:\n  - slug: alpha\n    cadences: [docs-sync]\n")
    idx = str(tmp_path / "index.db")
    db.open_index(idx).close()
    assert dispatch.main(["alpha", "docs-sync", "--state-dir", str(tmp_path),
                          "--foreman-dir", str(tmp_path), "--index", idx]) == 0
    assert "dispatched" in capsys.readouterr().out


def test_dispatch_red_bypasses_quota_guard(tmp_path):
    conn = db.open_index(":memory:")
    quota.record(conn, pct_used=97, taken="2026-09-06T12:00:00Z")
    conn.execute("INSERT INTO escalation(id, run_id, project, cadence, severity, opened, first_seen) "
                 "VALUES(1,'r','alpha','docs-sync','red','2026-09-06T00:00:00Z','2026-09-06T00:00:00Z')")
    conn.commit()
    out = dispatch.dispatch(conn, _REG, tmp_path, "alpha", "docs-sync")   # red -> not deferred
    assert out["deferred"] is False and out["dispatched"]


# --------------------------------------------------------------------- runstate

def test_collect_quota_records_and_skips(tmp_path):
    conn = db.open_index(":memory:")
    now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)
    assert runstate.collect_quota(conn, fetch=lambda: None, now=now) is None
    got = runstate.collect_quota(conn, fetch=lambda: {"pct_used": 42, "plan": "max"}, now=now)
    assert got["pct_used"] == 42
    assert quota.headroom(conn) == 58


def test_default_quota_fetch_via_cmd(monkeypatch):
    monkeypatch.delenv("FOREMAN_QUOTA_CMD", raising=False)
    assert runstate.default_quota_fetch() is None                  # unwired -> None
    monkeypatch.setenv("FOREMAN_QUOTA_CMD", 'printf %s \'{"pct_used": 30, "plan": "pro"}\'')
    assert runstate.default_quota_fetch()["pct_used"] == 30
    monkeypatch.setenv("FOREMAN_QUOTA_CMD", "printf notjson")
    assert runstate.default_quota_fetch() is None                  # unparseable -> None


def test_reconcile_runs_idempotent(tmp_path):
    conn = db.open_index(":memory:")
    hist = [{"project": "alpha", "cadence": "docs-sync", "started": "2026-09-06T08:00:00Z",
             "status": "failed"}]
    assert runstate.reconcile_runs(conn, hist) == 1
    row = conn.execute("SELECT run_id, status FROM run WHERE project='alpha'").fetchone()
    assert row["run_id"].startswith("rt-") and row["status"] == "failed"
    assert runstate.reconcile_runs(conn, hist) == 0                # same day already recorded
    out = runstate.collect(conn, quota_fetch=lambda: None, routine_history=hist)
    assert out["runs_recorded"] == 0 and out["quota"] is None


def test_runstate_cli(tmp_path, capsys):
    idx = str(tmp_path / "index.db")
    hist = tmp_path / "h.json"
    hist.write_text(json.dumps([{"project": "alpha", "cadence": "docs-sync",
                                 "started": "2026-09-06T08:00:00Z", "status": "failed"}]))
    assert runstate.main(["--index", idx, "--history", str(hist)]) == 0
    assert "runs_recorded" in capsys.readouterr().out

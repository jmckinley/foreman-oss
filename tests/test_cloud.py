"""The cloud routine (collectors.cloud) — the CI half of the scheduler that fires tier:cloud
cadences. Exercised in-process with the clone + `claude -p` steps stubbed, so the due-from-
receipts logic and the receipt shape (host/tier/lock) are covered without network or the CLI."""
import datetime as dt
import json
from pathlib import Path

from collectors import cloud

# 2026-09-08 is a Tuesday, so quality-review's "23 5 * * 2" has a recent previous_fire.
NOW = dt.datetime(2026, 9, 8, 12, 0)

GREEN_METRICS = {"review_debt_prs": 0, "coverage_pct": 85, "complexity_hotspots": 2}


def _fake_claude_writing(metrics, next_action="Ship it."):
    def _run(prompt, *, cwd, env, timeout_s, log):
        spool, rid = Path(env["FOREMAN_SPOOL"]), env["FOREMAN_RUN_ID"]
        (spool / f"{rid}.partial.json").write_text(
            json.dumps({"metrics": metrics, "next_action": next_action, "deltas": []}))
        return 0
    return _run


def test_due_cloud_includes_never_run(foreman_dir, state_dir):
    due = cloud.due_cloud(foreman_dir, state_dir, now=NOW)
    pairs = {(d["project"], d["cadence"]) for d in due}
    # acmeapi enables quality-review at tier:cloud, and it has never produced a receipt
    assert ("acmeapi", "quality-review") in pairs
    # every returned cadence carries the project's repo for cloning
    assert all(d.get("repo") for d in due)


def test_due_cloud_skips_local_tier(foreman_dir, state_dir):
    due = cloud.due_cloud(foreman_dir, state_dir, now=NOW)
    # self-check / ops-readiness are tier:local on foreman — the cloud routine must never fire them
    assert not any(d["cadence"] in {"self-check", "ops-readiness", "intuitive-ux"} for d in due)


def test_due_cloud_idempotent_after_receipt(foreman_dir, state_dir, make_receipt):
    occ = cloud.previous_fire("23 5 * * 2", NOW)      # the occurrence this pass would fire
    ended = occ.strftime("%Y-%m-%dT%H:%M:%SZ")
    make_receipt("acmeapi", "quality-review", ended, "green", metrics=GREEN_METRICS)
    due = cloud.due_cloud(foreman_dir, state_dir, now=NOW)
    assert ("acmeapi", "quality-review") not in {(d["project"], d["cadence"]) for d in due}


def test_run_one_writes_cloud_receipt(foreman_dir, state_dir, spool_dir, monkeypatch):
    monkeypatch.setattr(cloud, "_clone_repo", lambda *a, **k: True)
    monkeypatch.setattr(cloud, "_run_claude", _fake_claude_writing(GREEN_METRICS))
    res = cloud.run_one(foreman_dir, state_dir, spool_dir, project="acmeapi",
                        cadence="quality-review", repo="acme-demo/acmeapi", now=NOW)
    assert res["cloned"] and res["receipt"]
    r = json.loads(Path(res["receipt"]).read_text())
    assert r["host"] == "cloud" and r["tier"] == "cloud"
    assert r["lock"] == "unverified"               # index unreachable from CI (SPEC §14)
    assert r["status"] == "ok" and r["verdict"] == "green"
    assert r["metrics"] == GREEN_METRICS


def test_run_one_clone_failure_is_red(foreman_dir, state_dir, spool_dir, monkeypatch):
    # a repo that won't clone must still leave a receipt — red by absence, never silence
    monkeypatch.setattr(cloud, "_clone_repo", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(cloud, "_run_claude", lambda *a, **k: called.append(1))
    res = cloud.run_one(foreman_dir, state_dir, spool_dir, project="acmeapi",
                        cadence="quality-review", repo="acme-demo/acmeapi", now=NOW)
    assert not res["cloned"] and not called          # claude never runs without a clone
    r = json.loads(Path(res["receipt"]).read_text())
    assert r["status"] == "failed" and r["verdict"] == "red"


def test_tick_fires_then_is_idempotent(foreman_dir, state_dir, spool_dir, monkeypatch):
    monkeypatch.setattr(cloud, "_clone_repo", lambda *a, **k: True)
    monkeypatch.setattr(cloud, "_run_claude", _fake_claude_writing(GREEN_METRICS))
    first = cloud.tick(foreman_dir, state_dir, spool_dir, now=NOW)
    assert first["fired"] and all(f.get("receipt") for f in first["fired"])
    # the receipts written this pass capture the current occurrence, so a second tick at the
    # same instant finds nothing due
    second = cloud.tick(foreman_dir, state_dir, spool_dir, now=NOW)
    assert second["fired"] == []


def test_tick_only_scopes_to_matching_cadence(foreman_dir, state_dir, spool_dir, monkeypatch):
    monkeypatch.setattr(cloud, "_clone_repo", lambda *a, **k: True)
    monkeypatch.setattr(cloud, "_run_claude", _fake_claude_writing(GREEN_METRICS))
    out = cloud.tick(foreman_dir, state_dir, spool_dir, now=NOW, only="quality-review")
    # only quality-review cadences fired; nothing else (e.g. arch-review) slipped through
    assert out["fired"] and all(f["cadence"] == "quality-review" for f in out["fired"])


def test_receipts_validate_against_schema(foreman_dir, state_dir, spool_dir, monkeypatch):
    import jsonschema
    schema = json.loads((foreman_dir / "schema" / "receipt.schema.json").read_text())
    monkeypatch.setattr(cloud, "_clone_repo", lambda *a, **k: True)
    monkeypatch.setattr(cloud, "_run_claude", _fake_claude_writing(GREEN_METRICS))
    cloud.tick(foreman_dir, state_dir, spool_dir, now=NOW)
    written = list((state_dir / "receipts").rglob("*.json"))
    assert written
    for p in written:
        jsonschema.validate(json.loads(p.read_text()), schema)

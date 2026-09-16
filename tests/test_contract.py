"""The contract: registry/cadence validation and receipt emission."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from collectors import validate as V
from collectors import build_receipt

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- validate

def test_real_repo_validates():
    assert V.validate(ROOT).ok()


def test_foreman_dir_copy_validates(foreman_dir):
    assert V.validate(foreman_dir).ok()


def _mutate(foreman_dir, mutate, prompt="do work\n"):
    cad = yaml.safe_load((foreman_dir / "cadences" / "docs-sync.yaml").read_text())
    mutate(cad)
    (foreman_dir / "cadences" / "docs-sync.yaml").write_text(yaml.safe_dump(cad, sort_keys=False))
    (foreman_dir / "prompts" / "docs-sync.md").write_text(prompt)
    return V.validate(foreman_dir)


def test_cloud_rejects_allowed_hosts(foreman_dir):
    rep = _mutate(foreman_dir, lambda c: c.__setitem__("allowed_hosts", ["mbp"]))
    assert any("allowed_hosts" in e for e in rep.errors)


def test_session_rejects_escalate_when(foreman_dir):
    rep = _mutate(foreman_dir, lambda c: (c.__setitem__("tier", "session"),
                                          c.__setitem__("escalate_when", "red")))
    assert any("escalate_when" in e for e in rep.errors)


def test_cloud_rejects_absolute_path_in_prompt(foreman_dir):
    rep = _mutate(foreman_dir, lambda c: None, prompt="write to /Users/john/out.json\n")
    assert any("absolute local path" in e for e in rep.errors)


def test_cloud_rejects_subhourly(foreman_dir):
    rep = _mutate(foreman_dir, lambda c: c.__setitem__("schedule", "*/10 * * * *"))
    assert any("between fires" in e for e in rep.errors)


@pytest.mark.parametrize("minute", ["0", "30"])
def test_minute_00_30_rejected(foreman_dir, minute):
    rep = _mutate(foreman_dir, lambda c: c.__setitem__("schedule", f"{minute} 3 * * 1"))
    assert any("avoid :00 and :30" in e for e in rep.errors)


def test_unknown_project_and_metric(foreman_dir):
    rep = _mutate(foreman_dir, lambda c: c.__setitem__("applies_to", ["ghost"]))
    assert any("unknown project" in e for e in rep.errors)
    rep2 = _mutate(foreman_dir, lambda c: c["verdict"].__setitem__("green", "phantom == 0"))
    assert any("undeclared metric" in e for e in rep2.errors)


def test_cron_intervals():
    c = V.Cron.parse("17 3 * * 1,4")
    assert c.min_interval_minutes() == 4320 and c.max_interval_minutes() == 5760


# ------------------------------------------------------------------ receipts

def _emit(env, spool, state, partial=None):
    if partial is not None:
        (spool / f"{env['FOREMAN_RUN_ID']}.partial.json").write_text(json.dumps(partial))
    full_env = {**os.environ, **env, "FOREMAN_SPOOL": str(spool), "FOREMAN_STATE_DIR": str(state),
                "PYTHONPATH": str(ROOT), "FOREMAN_DIR": str(ROOT)}
    out = subprocess.run(["bash", str(ROOT / "hooks" / "session_end.sh")],
                         env=full_env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    path = out.stdout.strip()
    return json.loads(open(path).read()) if path else None


def _env(run_id="01HZY0R4M8K3V9WTE2N6QGDCBF"):
    return {"FOREMAN_RUN_ID": run_id, "FOREMAN_CADENCE": "docs-sync",
            "FOREMAN_PROJECT": "acmeapi", "FOREMAN_HOST": "cloud", "FOREMAN_TIER": "cloud",
            "FOREMAN_STARTED": "2026-09-06T08:00:00Z", "FOREMAN_CC_VERSION": "2.0"}


def _schema_ok(receipt):
    schema = json.loads((ROOT / "schema" / "receipt.schema.json").read_text())
    return not list(Draft202012Validator(schema).iter_errors(receipt))


def test_receipt_green(spool_dir, state_dir):
    r = _emit(_env(), spool_dir, state_dir, partial={
        "metrics": {"undocumented_public_symbols": 0, "guide_sections_stale_days_max": 3,
                    "changelog_commit_delta": 2},
        "next_action": "Doc the new symbols"})
    assert r["status"] == "ok" and r["verdict"] == "green" and _schema_ok(r)


def test_receipt_amber(spool_dir, state_dir):
    r = _emit(_env(), spool_dir, state_dir, partial={
        "metrics": {"undocumented_public_symbols": 2, "guide_sections_stale_days_max": 20,
                    "changelog_commit_delta": 1}, "next_action": "Refresh deploy docs"})
    assert r["verdict"] == "amber" and _schema_ok(r)


def test_receipt_failed_no_partial(spool_dir, state_dir):
    r = _emit(_env(), spool_dir, state_dir, partial=None)
    assert r["status"] == "failed" and r["verdict"] == "red"
    assert r["escalations"][0]["severity"] == "red" and _schema_ok(r)


def test_receipt_failed_missing_metric(spool_dir, state_dir):
    r = _emit(_env(), spool_dir, state_dir,
              partial={"metrics": {"undocumented_public_symbols": 0}, "next_action": "x"})
    assert r["status"] == "failed" and _schema_ok(r)


def test_clean_deltas_tolerates_malformed():
    # a dict (what a model wrote) -> dropped, not fatal; valid items kept & trimmed
    assert build_receipt._clean_deltas({"tests_failed": 0}) == []
    assert build_receipt._clean_deltas(None) == []
    assert build_receipt._clean_deltas([{"kind": "metric", "name": "x", "direction": "worse",
                                         "from": 1, "to": 2, "junk": 9}]) == \
        [{"kind": "metric", "name": "x", "direction": "worse", "from": 1, "to": 2}]
    assert build_receipt._clean_deltas([{"name": "x"}, "nope"]) == []   # missing keys dropped


def test_receipt_survives_dict_shaped_deltas(spool_dir, state_dir):
    # the exact failure the headless self-check hit: deltas as a dict must not sink the receipt
    r = _emit(_env(), spool_dir, state_dir, partial={
        "metrics": {"undocumented_public_symbols": 0, "guide_sections_stale_days_max": 3,
                    "changelog_commit_delta": 2},
        "deltas": {"undocumented_public_symbols": 0}, "next_action": "x"})
    assert r["status"] == "ok" and "deltas" not in r and _schema_ok(r)


# --------------------------------------------------------- independent verifier (M8.2)

def test_apply_verifier_downgrades_unreproduced_red():
    cad = {"verify": {"enabled": True}}
    r = {"status": "ok", "verdict": "red", "metrics": {"m": 5}, "next_action": "x"}
    build_receipt.apply_verifier(r, cad, verifier=lambda receipt, cadence: False)
    assert r["verdict"] == "amber" and r["verification"] == {"reproduced": False, "verifier": "probe"}
    assert "not reproduced" in r["notes"]


def test_apply_verifier_keeps_reproduced_red():
    cad = {"verify": {"enabled": True, "model": "haiku"}}
    r = {"status": "ok", "verdict": "red", "metrics": {"m": 5}, "next_action": "x"}
    build_receipt.apply_verifier(r, cad, verifier=lambda receipt, cadence: True)
    assert r["verdict"] == "red" and r["verification"] == {"reproduced": True, "verifier": "haiku"}
    assert "notes" not in r


def test_apply_verifier_noop_when_disabled_or_not_red():
    called = []
    spy = lambda receipt, cadence: called.append(1) or True
    # not enabled
    r1 = {"status": "ok", "verdict": "red", "metrics": {}, "next_action": "x"}
    build_receipt.apply_verifier(r1, {}, verifier=spy)
    # enabled but green
    r2 = {"status": "ok", "verdict": "green", "metrics": {}, "next_action": "x"}
    build_receipt.apply_verifier(r2, {"verify": {"enabled": True}}, verifier=spy)
    # enabled but a hard failure red (status failed) -- not a metric red, do not second-guess
    r3 = {"status": "failed", "verdict": "red", "metrics": {}, "next_action": "x"}
    build_receipt.apply_verifier(r3, {"verify": {"enabled": True}}, verifier=spy)
    assert called == [] and "verification" not in r1 and "verification" not in r3


def test_default_verifier_reads_verify_cmd(monkeypatch):
    r = {"cadence": "c", "project": "p", "metrics": {"m": 5}, "next_action": "x"}
    monkeypatch.setenv("FOREMAN_VERIFY_CMD", "printf '{\"reproduced\": false}'")
    assert build_receipt.default_verifier(r, {}) is False
    monkeypatch.setenv("FOREMAN_VERIFY_CMD", "printf '{\"reproduced\": true}'")
    assert build_receipt.default_verifier(r, {}) is True
    monkeypatch.setenv("FOREMAN_VERIFY_CMD", "exit 3")           # verifier failure keeps the red
    assert build_receipt.default_verifier(r, {}) is True


def _verify_foreman(tmp_path):
    """A temp foreman dir with one verify-enabled cadence that produces a red for m>0."""
    fm = tmp_path / "vfm"
    (fm / "cadences").mkdir(parents=True)
    (fm / "prompts").mkdir()
    (fm / "prompts" / "vr.md").write_text("do work\n")
    (fm / "cadences" / "verify-red.yaml").write_text(
        "slug: verify-red\ntier: cloud\nschedule: \"7 3 * * 1\"\napplies_to: [acmeapi]\n"
        "prompt_ref: prompts/vr.md\nmetrics: [m]\nverify: {enabled: true}\n"
        "verdict: {green: 'm == 0', amber: 'm == 0', red: otherwise}\n")
    return fm


def _emit_verify(env, spool, state, foreman_dir, partial, verify_cmd):
    full = {**os.environ, **env, "FOREMAN_SPOOL": str(spool), "FOREMAN_STATE_DIR": str(state),
            "PYTHONPATH": str(ROOT), "FOREMAN_DIR": str(foreman_dir),
            "FOREMAN_VERIFY_CMD": verify_cmd}
    (spool / f"{env['FOREMAN_RUN_ID']}.partial.json").write_text(json.dumps(partial))
    out = subprocess.run(["bash", str(ROOT / "hooks" / "session_end.sh")],
                         env=full, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(open(out.stdout.strip()).read())


def test_receipt_red_downgraded_by_verifier(spool_dir, state_dir, tmp_path):
    fm = _verify_foreman(tmp_path)
    env = {**_env(), "FOREMAN_CADENCE": "verify-red"}
    r = _emit_verify(env, spool_dir, state_dir, fm, {"metrics": {"m": 5}, "next_action": "x"},
                     verify_cmd="printf '{\"reproduced\": false}'")
    assert r["status"] == "ok" and r["verdict"] == "amber"          # red -> amber
    assert r["verification"]["reproduced"] is False and _schema_ok(r)


def test_receipt_red_confirmed_by_verifier(spool_dir, state_dir, tmp_path):
    fm = _verify_foreman(tmp_path)
    env = {**_env(run_id="01HZY0R4M8K3V9WTE2N6QGDCBG"), "FOREMAN_CADENCE": "verify-red"}
    r = _emit_verify(env, spool_dir, state_dir, fm, {"metrics": {"m": 5}, "next_action": "x"},
                     verify_cmd="printf '{\"reproduced\": true}'")
    assert r["verdict"] == "red" and r["verification"]["reproduced"] is True and _schema_ok(r)


def test_emission_idempotent(spool_dir, state_dir):
    r1 = _emit(_env(), spool_dir, state_dir, partial={
        "metrics": {"undocumented_public_symbols": 0, "guide_sections_stale_days_max": 3,
                    "changelog_commit_delta": 2}, "next_action": "x"})
    existing = build_receipt.existing_receipt(state_dir, "acmeapi", "docs-sync", r1["run_id"])
    assert existing is not None


# --------------------------------------------------------------------------- loop craft

def _all_loop_specs():
    """Every runnable loop's (metrics, green, amber) — committed cadences + CATALOG templates."""
    from collectors import loops
    specs = {}
    for f in (ROOT / "cadences").glob("*.yaml"):
        d = yaml.safe_load(f.read_text())
        if "verdict" in d and "metrics" in d:
            specs[d["slug"]] = (d["metrics"], d["verdict"].get("green", ""),
                                d["verdict"].get("amber", ""))
    for name, s in loops.CATALOG.items():
        if "metrics" in s and name not in specs:
            specs[name] = (s["metrics"], s.get("green", ""), s.get("amber", ""))
    return specs


# metrics deliberately recorded for context/trend but NOT gating a verdict (not defects)
_INFORMATIONAL = {
    ("beta-readiness", "days_to_target"),
    ("handover-refresh", "days_since_handover_update"),
    ("harness-refresh", "skills_unused"),
    ("self-check", "uncommitted_files"),
    ("pen-test", "attack_surface_uncovered"),
}


def test_every_declared_metric_gates_or_is_informational():
    """Craft guardrail: a measured metric must influence the verdict, or be explicitly marked
    informational — otherwise it's a false-green waiting to happen (a real problem the verdict
    ignores)."""
    for slug, (metrics, green, amber) in _all_loop_specs().items():
        expr = f"{green} {amber}"
        for m in metrics:
            assert m in expr or (slug, m) in _INFORMATIONAL, (
                f"{slug}: metric {m!r} neither gates the verdict nor is listed informational")


def test_regated_verdicts_are_not_falsely_green():
    """The specific false-greens fixed in the loop-craft review stay fixed."""
    def v(name, metrics):
        cad = yaml.safe_load((ROOT / "cadences" / f"{name}.yaml").read_text())
        return build_receipt.compute_verdict(cad, metrics)
    assert v("security-review", {"high_severity_findings": 0, "secrets_exposed": 0,
                                 "deps_with_known_cves": 3}) == "amber"
    assert v("perf-review", {"hot_paths_unbounded": 0, "p95_regressions": 1,
                             "load_headroom_pct": 90}) == "amber"
    assert v("arch-review", {"boundary_violations": 0, "cyclic_deps": 0,
                             "files_over_500loc": 9}) == "amber"
    assert v("quality-review", {"review_debt_prs": 0, "coverage_pct": 95,
                                "complexity_hotspots": 7}) == "amber"
    assert v("docs-sync", {"undocumented_public_symbols": 0, "guide_sections_stale_days_max": 2,
                           "changelog_commit_delta": 30}) == "amber"

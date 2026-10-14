"""Bounded auto-remediation: the opt-in resolver, the red-triggers-a-fix-dispatch plan, the
attempt cap with an operator note, idempotency against an already-queued fix, and the off-by-
default safety (no opt-in => nothing happens)."""
import json

import pytest
import yaml

from collectors import remediate, decisions as D


_REG = """version: 1
hosts:
  mbp: {os: darwin, role: interactive}
defaults:
  marketplace: github:x/y
  marketplace_pin: "0.1.0"
  context_budget_tokens: 100
  receipt_branch: state
  spool_dir: /tmp
projects:
  - slug: alpha
    repo: o/alpha
    default_branch: main
    worktree: {mbp: /tmp/alpha}
    tier_default: local
    cadences: [security-review]
    auto_remediate: true
    escalation: {github_issues: false}
"""

_CADENCE = {
    "slug": "security-review", "title": "sec", "tier": "cloud",
    "schedule": "0 9 * * 1", "applies_to": ["alpha"], "prompt_ref": "security-review.md",
    "metrics": ["findings"], "verdict": {"green": "a", "amber": "b", "red": "c"},
    "remediation": {"enabled": True, "max_attempts": 2},
}


@pytest.fixture
def fd(tmp_path):
    d = tmp_path / "fm"
    (d / "cadences").mkdir(parents=True)
    (d / "registry.yaml").write_text(_REG)
    (d / "cadences" / "security-review.yaml").write_text(yaml.safe_dump(_CADENCE))
    return d


@pytest.fixture
def sd(tmp_path):
    d = tmp_path / "state"
    (d / "receipts").mkdir(parents=True)
    (d / "queue").mkdir()
    return d


def _receipt(sd, project, cadence, ended, verdict, status="ok"):
    r = {"schema_version": 1, "run_id": D.ulid(), "cadence": cadence, "project": project,
         "host": "cloud", "tier": "cloud", "started": ended, "ended": ended,
         "status": status, "verdict": verdict, "cc_version": "2.0", "metrics": {},
         "next_action": "x"}
    p = sd / "receipts" / project / cadence
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{ended.replace(':', '').replace('-', '')}-{r['run_id']}.json").write_text(json.dumps(r))
    return r


def test_resolve_auto_remediate_forms():
    assert remediate.resolve_auto_remediate({"auto_remediate": True}, {}) is True
    assert remediate.resolve_auto_remediate({"auto_remediate": ["a", "b"]}, {}) == {"a", "b"}
    assert remediate.resolve_auto_remediate({}, {"auto_remediate": True}) is True
    assert remediate.resolve_auto_remediate({}, {}) is False
    # project overrides defaults
    assert remediate.resolve_auto_remediate({"auto_remediate": False}, {"auto_remediate": True}) is False


def test_red_dispatches_a_fix(fd, sd):
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "red")
    actions = remediate.plan(sd, fd)
    assert len(actions) == 1 and actions[0]["action"] == "dispatch"
    assert actions[0]["fix_cadence"] == "security-review-fix"

    out = remediate.scan(sd, fd)
    assert len(out["dispatched"]) == 1
    # a real dispatch_cadence decision was queued, tagged as remediation
    pending = D.load_pending(sd, "alpha")
    assert len(pending) == 1
    _, dec = pending[0]
    assert dec["kind"] == "dispatch_cadence"
    assert dec["payload"]["cadence"] == "security-review-fix"
    assert dec["payload"]["remediation"] is True


def test_green_never_remediates(fd, sd):
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "green")
    assert remediate.plan(sd, fd) == []
    assert remediate.scan(sd, fd)["dispatched"] == []


def test_not_opted_in_is_noop(fd, sd):
    reg = yaml.safe_load((fd / "registry.yaml").read_text())
    reg["projects"][0].pop("auto_remediate")
    (fd / "registry.yaml").write_text(yaml.safe_dump(reg))
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "red")
    assert remediate.plan(sd, fd) == []


def test_idempotent_skips_already_queued(fd, sd):
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "red")
    remediate.scan(sd, fd)                      # first dispatch
    actions = remediate.plan(sd, fd)
    assert actions[0]["action"] == "skip-pending"
    out = remediate.scan(sd, fd)                # second scan adds nothing
    assert out["dispatched"] == [] and len(D.load_pending(sd, "alpha")) == 1


def test_attempt_cap_files_a_note(fd, sd):
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "red")
    # simulate two prior fix attempts already applied (cap = 2), still red
    for i in range(2):
        _path, dec = D.enqueue(sd, "alpha", "dispatch_cadence",
                               {"cadence": "security-review-fix", "remediation": True,
                                "parent": "security-review", "attempt": i + 1}, "foreman-remediate")
        dec["status"] = "applied"
        (sd / "queue" / "alpha" / f"{dec['decision_id']}.json").write_text(json.dumps(dec))

    actions = remediate.plan(sd, fd)
    assert actions[0]["action"] == "capped"
    out = remediate.scan(sd, fd)
    assert out["capped"] and not out["dispatched"]
    # an operator note was filed, and it is idempotent (second scan files no duplicate)
    notes = [d for _, d in D.load_pending(sd, "alpha") if d["kind"] == "note"]
    assert len(notes) == 1
    remediate.scan(sd, fd)
    notes = [d for _, d in D.load_pending(sd, "alpha") if d["kind"] == "note"]
    assert len(notes) == 1


def test_attempts_reset_after_a_green(fd, sd):
    # red, then a later green, then red again: the green resets the streak, so a fresh attempt
    _receipt(sd, "alpha", "security-review", "2026-09-05T09:00:00Z", "red")
    # an old applied fix attempt before the green
    _path, dec = D.enqueue(sd, "alpha", "dispatch_cadence",
                           {"cadence": "security-review-fix", "remediation": True,
                            "parent": "security-review", "attempt": 1}, "foreman-remediate")
    dec["status"] = "applied"
    dec["created"] = "2026-09-05T09:30:00Z"
    (sd / "queue" / "alpha" / f"{dec['decision_id']}.json").write_text(json.dumps(dec))
    _receipt(sd, "alpha", "security-review", "2026-09-06T09:00:00Z", "green")
    _receipt(sd, "alpha", "security-review", "2026-09-07T09:00:00Z", "red")

    actions = remediate.plan(sd, fd)
    assert actions[0]["action"] == "dispatch" and actions[0]["attempt"] == 1

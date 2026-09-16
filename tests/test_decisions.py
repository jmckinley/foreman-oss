"""The operator-decision queue: enqueue, the app-side and supervisor-side apply paths, the
headless apply_decision the dashboard uses, dismiss, gc, and the CLI. Previously only exercised
incidentally; these drive the mutation paths directly."""
import datetime as dt
import json

import pytest
import yaml

from collectors import decisions as D

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
    worktree:
      mbp: WT
    tier_default: local
    cadences: [docs-sync]
    marketplace_pin: "0.1.0"
    escalation: {github_issues: false}
"""


@pytest.fixture
def fm(tmp_path):
    wt = tmp_path / "alpha"
    wt.mkdir()
    fd = tmp_path / "fm"
    fd.mkdir()
    (fd / "registry.yaml").write_text(_REG.replace("WT", str(wt)))
    state = tmp_path / "state"
    return fd, wt, state


def test_apply_setting_set_and_append(tmp_path):
    pd = tmp_path / "proj"
    pd.mkdir()
    D._apply_setting(pd, {"path": "permissions.allow", "value": ["Bash"], "op": "set"})
    data = json.loads((pd / ".claude" / "settings.json").read_text())
    assert data["permissions"]["allow"] == ["Bash"]
    D._apply_setting(pd, {"path": "permissions.allow", "value": "Read", "op": "append"})
    data = json.loads((pd / ".claude" / "settings.json").read_text())
    assert data["permissions"]["allow"] == ["Bash", "Read"]
    # appending onto a non-list is refused
    D._apply_setting(pd, {"path": "model", "value": "opus"})
    with pytest.raises(ValueError):
        D._apply_setting(pd, {"path": "model", "value": "x", "op": "append"})


def test_apply_app_side_kinds(tmp_path):
    pd = tmp_path / "p"
    pd.mkdir()
    ctx, res = D.apply_app_side({"kind": "note", "payload": {"text": "hi"}}, pd)
    assert "hi" in ctx and res is None
    ctx, _ = D.apply_app_side(
        {"kind": "answer_question", "payload": {"question_ref": "Q1", "answer": "yes"}}, pd)
    assert "Q1" in ctx and "yes" in ctx
    ctx, _ = D.apply_app_side({"kind": "dispatch_cadence", "payload": {"cadence": "docs-sync"}}, pd)
    assert "docs-sync" in ctx
    with pytest.raises(ValueError):
        D.apply_app_side({"kind": "set_pin", "payload": {}}, pd)   # supervisor-side, not app-side


def test_drain_session(fm):
    fd, wt, state = fm
    D.enqueue(state, "alpha", "apply_setting",
              {"path": "model", "value": "opus"}, "op")
    D.enqueue(state, "alpha", "note", {"text": "welcome"}, "op")
    out = D.drain_session(state, fd, wt, host="mbp", session_uuid="sess-1")
    assert out["project"] == "alpha" and len(out["applied"]) == 2
    assert "welcome" in out["context"]
    assert json.loads((wt / ".claude" / "settings.json").read_text())["model"] == "opus"
    assert not D.load_pending(state, "alpha")            # both marked applied
    # a cwd outside any worktree resolves to nothing
    assert D.drain_session(state, fd, fd, host="mbp", session_uuid=None)["project"] is None


def test_edit_registry_pin(fm):
    fd, _, _ = fm
    text = (fd / "registry.yaml").read_text()
    # defaults scope updates the existing pin
    out = D._edit_registry_pin(text, "defaults", "9.9.9")
    assert 'marketplace_pin: "9.9.9"' in out.split("projects:")[0]
    # project scope with an existing pin
    out = D._edit_registry_pin(text, "alpha", "2.0.0")
    assert 'marketplace_pin: "2.0.0"' in out
    # unknown project raises
    with pytest.raises(ValueError):
        D._edit_registry_pin(text, "ghost", "1.0.0")


def test_edit_registry_pin_inserts_when_absent(fm):
    fd, _, _ = fm
    text = (fd / "registry.yaml").read_text().replace('    marketplace_pin: "0.1.0"\n', "", 1)
    # defaults still has its pin; the project's was removed -> insert path
    out = D._edit_registry_pin(text, "alpha", "3.3.3")
    reg = yaml.safe_load(out)
    assert next(p for p in reg["projects"] if p["slug"] == "alpha")["marketplace_pin"] == "3.3.3"


def test_apply_supervisor_side_set_pin_and_strings(fm, monkeypatch):
    fd, _, _ = fm
    r = D.apply_supervisor_side(
        {"kind": "set_pin", "project": "alpha",
         "payload": {"scope": "defaults", "marketplace_pin": "5.5.5"}}, fd)
    assert "5.5.5" in r
    assert yaml.safe_load((fd / "registry.yaml").read_text())["defaults"]["marketplace_pin"] == "5.5.5"
    assert "close" in D.apply_supervisor_side(
        {"kind": "close_escalation", "project": "alpha",
         "payload": {"escalation": "E1", "reason": "fixed"}}, fd)
    # approve_push shells out; stub the remote side
    monkeypatch.setattr(D, "push_branch", lambda repo, payload: f"pushed {payload['branch']}")
    r = D.apply_supervisor_side(
        {"kind": "approve_push", "project": "alpha",
         "payload": {"branch": "foreman/x", "base": "main"}}, fd, host="mbp")
    assert r == "pushed foreman/x"


def test_drain_supervisor_skips_manual(fm):
    fd, _, state = fm
    D.enqueue(state, "alpha", "set_pin", {"scope": "defaults", "marketplace_pin": "6.6.6"}, "op")
    D.enqueue(state, "alpha", "approve_push", {"branch": "b", "base": "main"}, "op")
    applied = D.drain_supervisor(state, fd, host="mbp")
    assert [a for a in applied if "result" in a]                 # set_pin applied
    kinds_pending = {dec["kind"] for _, dec in D.load_pending(state, "alpha")}
    assert kinds_pending == {"approve_push"}                     # manual-only left pending


def test_apply_decision_headless_refused_and_notfound(fm):
    fd, _, state = fm
    _, pin = D.enqueue(state, "alpha", "set_pin",
                       {"scope": "defaults", "marketplace_pin": "7.7.7"}, "op")
    assert D.apply_decision(state, fd, pin["decision_id"], host="mbp")["ok"] is True
    # a session-delivery kind cannot be force-applied
    _, note = D.enqueue(state, "alpha", "note", {"text": "later"}, "op")
    refused = D.apply_decision(state, fd, note["decision_id"], host="mbp")
    assert refused["ok"] is False and "session" in refused["reason"]
    assert D.apply_decision(state, fd, "missing", host="mbp")["ok"] is False


def test_apply_decision_requires_session_setting(fm):
    fd, wt, state = fm
    _, dec = D.enqueue(state, "alpha", "apply_setting",
                       {"path": "env.FOO", "value": "bar"}, "op")
    out = D.apply_decision(state, fd, dec["decision_id"], host="mbp")
    assert out["ok"] is True
    assert json.loads((wt / ".claude" / "settings.json").read_text())["env"]["FOO"] == "bar"


def test_dismiss(fm):
    _, _, state = fm
    _, dec = D.enqueue(state, "alpha", "note", {"text": "x"}, "op")
    assert D.dismiss(state, dec["decision_id"]) is True
    assert not D.load_pending(state, "alpha")
    assert D.dismiss(state, "nope") is False


def test_gc_expires_and_prunes(fm):
    fd, _, state = fm
    past = "2020-01-01T00:00:00Z"
    D.enqueue(state, "alpha", "note", {"text": "stale"}, "op", expires=past)
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    res = D.gc(state, now=now)
    assert res["expired"]                                        # the past-expiry pending -> expired
    # an applied decision older than retention is pruned
    D.enqueue(state, "alpha", "set_pin", {"scope": "defaults", "marketplace_pin": "8.8.8"}, "op")
    D.drain_supervisor(state, fd, host="mbp")                    # marks it applied (now)
    pruned = D.gc(state, retention_days=0,
                  now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
    assert pruned["pruned"]


def test_cli_enqueue_list_board_gc(fm, monkeypatch, capsys):
    fd, _, state = fm
    monkeypatch.setenv("FOREMAN_STATE_DIR", str(state))
    monkeypatch.setenv("FOREMAN_DIR", str(fd))
    assert D.main(["enqueue", "--project", "alpha", "--kind", "note",
                   "--payload", '{"text": "hello"}', "--actor", "op"]) == 0
    assert D.main(["list"]) == 0
    assert "note" in capsys.readouterr().out
    assert D.main(["board"]) == 0
    assert "QUEUED" in capsys.readouterr().out
    assert D.main(["gc"]) == 0

"""build_receipt.build() in-process. Its end-to-end tests run through hooks/session_end.sh as
a subprocess (uncounted by coverage); these drive build() directly, plus the verdict evaluator
and verifier-envelope helpers."""
import json
from pathlib import Path

import pytest

from collectors import build_receipt as BR
from collectors.decisions import ulid

_CAD = ('slug: q\ntier: cloud\nschedule: "19 9 * * *"\n'
        'metrics: [errors]\nverdict: {green: "errors == 0", amber: "errors < 5", red: otherwise}\n'
        'applies_to: [alpha]\nprompt_ref: prompts/q.md\n')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    fd = tmp_path / "fm"
    (fd / "cadences").mkdir(parents=True)
    (fd / "cadences" / "q.yaml").write_text(_CAD)
    spool = tmp_path / "spool"
    spool.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for k, v in {"FOREMAN_RUN_ID": ulid(), "FOREMAN_CADENCE": "q", "FOREMAN_PROJECT": "alpha",
                 "FOREMAN_HOST": "cloud", "FOREMAN_TIER": "cloud",
                 "FOREMAN_STARTED": "2026-09-06T08:00:00Z", "FOREMAN_SPOOL": str(spool),
                 "FOREMAN_STATE_DIR": str(state), "FOREMAN_DIR": str(fd),
                 "FOREMAN_ENDED": "2026-09-06T08:05:00Z"}.items():
        monkeypatch.setenv(k, v)

    def run(partial=None, run_id=None, **env):
        rid = run_id or ulid()
        if partial is not None:
            (spool / f"{rid}.partial.json").write_text(json.dumps(partial))
        monkeypatch.setenv("FOREMAN_RUN_ID", rid)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        path = BR.build()
        return json.loads(Path(path).read_text()), Path(path)

    return run, state, spool, fd, monkeypatch


def test_build_green_amber_red(rig):
    run, state, *_ = rig
    r, path = run({"metrics": {"errors": 0}, "next_action": "all clear"})
    assert r["status"] == "ok" and r["verdict"] == "green" and path.is_file()
    r, _ = run({"metrics": {"errors": 3}, "next_action": "reduce errors"})
    assert r["verdict"] == "amber"
    r, _ = run({"metrics": {"errors": 9}, "next_action": "fix"})
    assert r["verdict"] == "red" and r["status"] == "ok"


def test_build_truncates_overlong_next_action(rig):
    # a completed run must not vanish ("red by absence") just because its one-sentence next step
    # exceeded the schema's 200-char cap — it's truncated and the receipt still validates + writes
    run, *_ = rig
    long = "Fix the add-a-loop trap: " + "warn the operator before they save a scheduled loop " * 6
    assert len(long) > 200
    r, path = run({"metrics": {"errors": 0}, "next_action": long})
    assert r["status"] == "ok" and path.is_file()          # written, not discarded
    assert len(r["next_action"]) <= 200 and r["next_action"].endswith("…")


def test_build_failed_without_partial(rig):
    run, *_ = rig
    r, _ = run(partial=None)
    assert r["status"] == "failed" and r["verdict"] == "red"
    assert r["escalations"][0]["severity"] == "red"
    assert "no partial" in r["escalations"][0]["evidence"] or "without a partial" in r["notes"]
    assert r["next_action"]                                # synthesised, never empty


def test_build_failed_on_metric_mismatch_and_missing_action(rig):
    run, *_ = rig
    r, _ = run({"metrics": {"wrong": 1}, "next_action": "x"})
    assert r["status"] == "failed" and "do not match" in r["notes"]
    r, _ = run({"metrics": {"errors": 0}})     # no next_action
    assert r["status"] == "failed" and "next_action" in r["notes"]


def test_build_is_idempotent(rig):
    run, state, *_ = rig
    rid = ulid()
    _, p1 = run({"metrics": {"errors": 0}, "next_action": "ok"}, run_id=rid)
    p1.write_text(p1.read_text().replace('"green"', '"TAMPERED"'))   # prove it isn't rewritten
    _, p2 = run({"metrics": {"errors": 0}, "next_action": "ok"}, run_id=rid)   # same run_id
    assert p1 == p2 and "TAMPERED" in p2.read_text()


def test_build_includes_deltas_model_lock(rig):
    run, *_ = rig
    r, _ = run({"metrics": {"errors": 0}, "next_action": "ok",
                "deltas": [{"kind": "metric", "name": "errors", "direction": "better",
                            "from": 5, "to": 0}]},
               FOREMAN_MODEL="opus", FOREMAN_LOCK="held")
    assert r["model"] == "opus" and r["lock"] == "held"
    assert r["deltas"][0]["direction"] == "better"


def test_build_verifier_downgrades_red(rig):
    run, state, spool, fd, monkeypatch = rig
    (fd / "cadences" / "qv.yaml").write_text(_CAD.replace("slug: q\n", "slug: qv\n")
                                             + "verify:\n  enabled: true\n")
    # a verify command that says "not reproduced" turns a metric red into amber
    r, _ = run({"metrics": {"errors": 9}, "next_action": "fix"},
               FOREMAN_CADENCE="qv", FOREMAN_VERIFY_CMD='printf %s \'{"reproduced": false}\'')
    assert r["verdict"] == "amber"
    assert r["verification"]["reproduced"] is False
    assert "downgraded to amber" in r["notes"]


def test_build_missing_env_exits(rig, monkeypatch):
    run, *_ = rig
    monkeypatch.delenv("FOREMAN_PROJECT")
    with pytest.raises(SystemExit):
        BR.build()


# ---- verdict expression evaluator ----

def test_check_expr_and_compute_verdict():
    assert BR.check_expr("a == 0 and b < 5", {"a": 0, "b": 3}) is True
    assert BR.check_expr("a == 0 or b < 5", {"a": 1, "b": 9}) is False
    assert BR.check_expr("not (a == 0)", {"a": 1}) is True
    assert BR.check_expr("0 < a < 10", {"a": 5}) is True       # chained compare
    cad = {"verdict": {"green": "x == 0", "amber": "x < 5", "red": "otherwise"}}
    assert BR.compute_verdict(cad, {"x": 0}) == "green"
    assert BR.compute_verdict(cad, {"x": 3}) == "amber"
    assert BR.compute_verdict(cad, {"x": 99}) == "red"


def test_check_expr_rejects_unsafe():
    with pytest.raises(ValueError):
        BR.check_expr("unknownvar == 1", {})                  # unknown name
    with pytest.raises(ValueError):
        BR.check_expr("__import__('os')", {})                 # disallowed node (call)


def test_parse_reproduced_envelope():
    assert BR._parse_reproduced('{"reproduced": false}') is False
    assert BR._parse_reproduced('{"result": "{\\"reproduced\\": false}"}') is False  # claude -p envelope
    assert BR._parse_reproduced("not json") is True           # tolerant default = keep the red
    assert BR._parse_reproduced('{"other": 1}') is True


def test_write_receipt_validates(tmp_path):
    with pytest.raises(SystemExit):     # _validate exits non-zero on a contract violation
        BR.write_receipt(tmp_path / "state", {"schema_version": 1, "run_id": "x",
                                              "project": "p", "cadence": "c"})   # missing fields

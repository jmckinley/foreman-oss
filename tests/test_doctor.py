"""foreman doctor: the preflight check statuses and the fix-hint rendering."""
from collectors import doctor


def _by_name(checks):
    return {c["name"]: c for c in checks}


def test_missing_instance_fails(tmp_path):
    checks = _by_name(doctor.run_checks(tmp_path / "nope", env={}))
    assert checks["python"]["status"] == doctor.OK
    assert checks["instance"]["status"] == doctor.FAIL
    assert "foreman init" in checks["instance"]["hint"]


def test_valid_instance_passes_validate(foreman_dir):
    # foreman_dir is a schema-valid instance (registry + cadences + schema).
    checks = _by_name(doctor.run_checks(foreman_dir, env={}))
    assert checks["instance"]["status"] == doctor.OK
    assert checks["validate"]["status"] == doctor.OK


def test_notify_check_reflects_env(tmp_path):
    off = _by_name(doctor.run_checks(tmp_path, env={}))["notify"]
    assert off["status"] == doctor.WARN and "FOREMAN_NOTIFY" in off["hint"]
    on = _by_name(doctor.run_checks(
        tmp_path, env={"FOREMAN_NOTIFY_WEBHOOK": "https://hooks.slack.com/x"}))["notify"]
    assert on["status"] == doctor.OK and "slack" in on["detail"]


def test_state_detected_from_env(foreman_dir):
    state = foreman_dir / "state"
    (state / "receipts").mkdir(parents=True)
    checks = _by_name(doctor.run_checks(foreman_dir, env={"FOREMAN_STATE_DIR": str(state)}))
    assert checks["state"]["status"] == doctor.OK


def test_render_marks_and_hints(tmp_path):
    out = doctor.render(doctor.run_checks(tmp_path / "nope", env={}))
    assert "foreman doctor" in out
    assert "✗" in out and "→" in out      # a failure and its fix hint are shown

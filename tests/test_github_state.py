"""github_state.compute — turning raw gh JSON into a github_state row — plus its classifiers."""
import datetime as dt
import json

from collectors import github_state as G

NOW = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)


def test_is_agent():
    assert G.is_agent("dependabot[bot]") is True
    assert G.is_agent("someone", is_bot=True) is True
    assert G.is_agent("claude-bot") is True          # "claude" substring
    assert G.is_agent("my-agent") is True            # "agent" substring
    assert G.is_agent("github-actions[bot]") is True # bot suffix
    assert G.is_agent("realdev") is False
    assert G.is_agent("") is False


def test_classify_check():
    assert G.classify_check("unit tests") == "test"
    assert G.classify_check("eslint") == "lint"
    assert G.classify_check("docker build") == "build"
    assert G.classify_check("deploy prod") == "deploy"
    assert G.classify_check("CodeQL scan") == "security"
    assert G.classify_check("something else") == "other"


def test_parse_iso():
    assert G._parse_iso("2026-09-01T00:00:00Z") == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)


def test_compute_full():
    raw = {
        "prs": [
            {"author_login": "realdev", "created_at": "2026-09-01T00:00:00Z",
             "checks": [{"name": "unit tests", "state": "FAILURE"},
                        {"name": "CodeQL", "state": "SUCCESS"}]},
            {"author_login": "dependabot[bot]", "created_at": "2026-09-09T00:00:00Z",
             "checks": [{"name": "security scan", "state": "ERROR"}]},
        ],
        "security_alerts": 2, "dependabot_open": 3, "issues_open": 5,
        "actions_minutes_month": 420,
    }
    r = G.compute(raw, project="alpha", taken="2026-09-10T12:00:00Z", now=NOW)
    assert r["open_prs"] == 2 and r["agent_prs"] == 1
    assert r["oldest_pr_days"] == 9                       # the 2026-09-01 PR
    assert r["failing_checks"] == 2                       # FAILURE + ERROR
    assert json.loads(r["failure_classes"]) == {"test": 1, "security": 1}
    assert r["security_alerts"] == 2 and r["dependabot_open"] == 3
    assert r["issues_open"] == 5 and r["actions_minutes_month"] == 420


def test_compute_empty():
    r = G.compute({}, project="alpha", taken="t", now=NOW)
    assert r["open_prs"] == 0 and r["agent_prs"] == 0 and r["failing_checks"] == 0
    assert r["oldest_pr_days"] == 0 and json.loads(r["failure_classes"]) == {}
    assert r["security_alerts"] == 0 and r["actions_minutes_month"] is None

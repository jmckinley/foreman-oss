"""Shared test harness: fixtures for a Foreman environment, test git repos, sessions,
receipts, a fake GitHub, telemetry payloads, and the index.

Everything is built in tmp dirs so the suite never touches the real repo state, and the
real schemas under the repo are reused so the contract is exercised, not a copy.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from collectors import db, decisions as D, escalations  # noqa: E402

ISO = "%Y-%m-%dT%H:%M:%SZ"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def _no_real_transcripts(tmp_path, monkeypatch):
    """Point the 'recently worked on' reader at an empty dir by default, so the suite never reads
    the developer's real ~/.claude transcripts (privacy + determinism). Tests that exercise the
    feature set FOREMAN_TRANSCRIPTS_ROOT to their own fixture dir."""
    empty = tmp_path / "empty-transcripts"
    empty.mkdir()
    monkeypatch.setenv("FOREMAN_TRANSCRIPTS_ROOT", str(empty))
    # and a per-test prompt store, so the collector/dashboard never touch ~/.foreman/prompts.db
    monkeypatch.setenv("FOREMAN_PROMPT_STORE", str(tmp_path / "prompts.db"))


@pytest.fixture
def now():
    return dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def foreman_dir(tmp_path):
    """A controlled copy of the control repo: registry + cadences + prompts."""
    d = tmp_path / "foreman"
    d.mkdir()
    (d / "registry.yaml").write_text((REPO / "registry.yaml").read_text())
    for sub in ("cadences", "prompts", "schema", "sql"):
        shutil.copytree(REPO / sub, d / sub)
    return d


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    (d / "receipts").mkdir(parents=True)
    (d / "queue").mkdir()
    return d


@pytest.fixture
def spool_dir(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    return d


@pytest.fixture
def conn():
    return db.open_index(":memory:")


@pytest.fixture
def index_path(tmp_path):
    return str(tmp_path / "index.db")


def _ts_ms(ended: str) -> int:
    return int(dt.datetime.strptime(ended, ISO).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


@pytest.fixture
def make_receipt(state_dir):
    """Write a schema-shaped receipt into the state tree and return it."""
    def _mk(project, cadence, ended, verdict, status="ok", *, host="cloud", tier="cloud",
            metrics=None, **extra):
        rid = extra.pop("run_id", None) or D.ulid(_ts_ms(ended))
        r = {"schema_version": 1, "run_id": rid, "cadence": cadence, "project": project,
             "host": host, "tier": tier, "started": ended, "ended": ended, "status": status,
             "verdict": verdict, "cc_version": "2.0", "metrics": metrics or {},
             "next_action": extra.pop("next_action", "do the thing")}
        r.update(extra)
        d = state_dir / "receipts" / project / cadence
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ended.replace(':', '').replace('-', '')}-{rid}.json").write_text(json.dumps(r))
        return r
    return _mk


@pytest.fixture
def git_repo(tmp_path):
    """Factory for a real git repo with optional dirty/agent/stale-branch state."""
    counter = {"n": 0}

    def _run(cwd, *args, date=None):
        env = dict(GIT_ENV)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                       capture_output=True, text=True)

    def _mk(*, settings=None, agent_branch=False, stale_branch=False, dirty=False):
        counter["n"] += 1
        d = tmp_path / f"repo{counter['n']}"
        d.mkdir()
        _run(d, "init", "-q", "-b", "main")
        (d / "a.txt").write_text("hello\n")
        _run(d, "add", "a.txt")
        _run(d, "commit", "-q", "-m", "init")
        if settings is not None:
            (d / ".claude").mkdir()
            (d / ".claude" / "settings.json").write_text(json.dumps(settings))
        if agent_branch:
            _run(d, "checkout", "-q", "-b", "foreman/work")
            (d / "b.txt").write_text("x")
            _run(d, "add", "b.txt")
            _run(d, "commit", "-q", "-m", "agent")
            _run(d, "checkout", "-q", "main")
        if stale_branch:
            old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
            _run(d, "checkout", "-q", "-b", "old")
            (d / "c.txt").write_text("old")
            _run(d, "add", "c.txt")
            _run(d, "commit", "-q", "-m", "old", date=old)
            _run(d, "checkout", "-q", "main")
        if dirty:
            (d / "dirty.txt").write_text("uncommitted")
        return d
    return _mk


@pytest.fixture
def make_transcript(tmp_path):
    """Write a Claude Code CLI transcript JSONL under an encoded-cwd dir; return (root, path)."""
    def _mk(cwd, uuid, records, root_name="projects"):
        from collectors.transcripts import encode_cwd
        root = tmp_path / root_name
        enc = root / encode_cwd(cwd)
        enc.mkdir(parents=True, exist_ok=True)
        path = enc / f"{uuid}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return root, path
    return _mk


class FakeGitHub(escalations.GitHub):
    """In-memory GitHub for the escalation lifecycle."""

    def __init__(self):
        self.issues = {}
        self.n = 0

    def find_issue(self, repo, title):
        for num, i in self.issues.items():
            if i["repo"] == repo and i["title"] == title and i["state"] == "open":
                return num
        return None

    def open_issue(self, repo, title, body, labels):
        self.n += 1
        self.issues[self.n] = {"repo": repo, "title": title, "body": body,
                               "labels": labels, "state": "open", "comments": []}
        return self.n

    def comment(self, repo, number, body):
        self.issues[int(number)]["comments"].append(body)

    def close_issue(self, repo, number, comment):
        self.issues[int(number)]["state"] = "closed"
        self.issues[int(number)]["comments"].append("CLOSE: " + comment)


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def otlp_metric():
    """Build one OTLP/JSON metric datapoint."""
    def _mk(name, value, attrs, ts_nano=1_757_000_000_000_000_000):
        return {"name": name, "sum": {"dataPoints": [{
            "asDouble": value, "timeUnixNano": str(ts_nano),
            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in attrs.items()],
        }]}}
    return _mk

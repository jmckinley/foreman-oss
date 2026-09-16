"""The hourly prompt collector: forward byte-offset tailing into a local store, resumable and
idempotent, with secrets masked before they land."""
import json

from collectors import prompts, recent


def _dir(root, cwd):
    d = root / recent.encode_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append(path, records):
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _human(text, cwd, ts=None):
    r = {"type": "user", "cwd": cwd, "message": {"role": "user", "content": text}}
    if ts:
        r["timestamp"] = ts
    return r


def test_collect_stores_per_project_and_timeline(tmp_path):
    store = tmp_path / "p.db"
    f = _dir(tmp_path, "/w/foreman") / "s.jsonl"
    _append(f, [_human("first ask", "/w/foreman", "2026-09-10T08:00:00Z"),
                _human("second ask", "/w/foreman", "2026-09-10T09:00:00Z")])
    _append(_dir(tmp_path, "/w/paysvc") / "s.jsonl",
            [_human("fix paysvc", "/w/paysvc", "2026-09-10T07:00:00Z")])
    cwds = {"foreman": "/w/foreman", "paysvc": "/w/paysvc"}
    r = prompts.collect(cwds, root=tmp_path, store=store)
    assert r["prompts_added"] == 3
    latest = {x["slug"]: x["prompt"] for x in prompts.latest_by_project(store=store)}
    assert latest == {"foreman": "second ask", "paysvc": "fix paysvc"}   # newest per project
    tl = prompts.timeline("foreman", store=store)
    assert [t["text"] for t in tl] == ["second ask", "first ask"]          # newest first


def test_collect_is_resumable_and_idempotent(tmp_path):
    store = tmp_path / "p.db"
    f = _dir(tmp_path, "/w/p") / "s.jsonl"
    _append(f, [_human("one", "/w/p", "2026-09-10T08:00:00Z")])
    cwds = {"p": "/w/p"}
    assert prompts.collect(cwds, root=tmp_path, store=store)["prompts_added"] == 1
    # re-running without new content adds nothing (offset remembered)
    assert prompts.collect(cwds, root=tmp_path, store=store)["prompts_added"] == 0
    # appending only reads the new tail
    _append(f, [_human("two", "/w/p", "2026-09-10T09:00:00Z")])
    assert prompts.collect(cwds, root=tmp_path, store=store)["prompts_added"] == 1
    assert len(prompts.timeline("p", store=store)) == 2


def test_secrets_are_masked_before_storage(tmp_path):
    store = tmp_path / "p.db"
    _append(_dir(tmp_path, "/w/p") / "s.jsonl",
            [_human("deploy with sk-ant-api03-" + "a" * 93 + "AA please", "/w/p",
                    "2026-09-10T08:00:00Z")])
    prompts.collect({"p": "/w/p"}, root=tmp_path, store=store)
    stored = prompts.timeline("p", store=store)[0]["text"]
    assert "sk-ant" not in stored and "[API_KEY]" in stored


def test_missing_cwd_is_skipped(tmp_path):
    store = tmp_path / "p.db"
    r = prompts.collect({"ghost": "/w/nope", "none": None}, root=tmp_path, store=store)
    assert r == {"prompts_added": 0, "sessions_scanned": 0}


def test_search_and_week_counts(tmp_path):
    store = tmp_path / "p.db"
    _append(_dir(tmp_path, "/w/foreman") / "s.jsonl", [
        _human("fix the scheduler bug", "/w/foreman", "2026-09-13T08:00:00Z"),
        _human("commit and push", "/w/foreman", "2026-09-13T09:00:00Z"),
        _human("old note", "/w/foreman", "2026-08-01T09:00:00Z")])
    _append(_dir(tmp_path, "/w/paysvc") / "s.jsonl",
            [_human("scheduler review", "/w/paysvc", "2026-09-12T09:00:00Z")])
    prompts.collect({"foreman": "/w/foreman", "paysvc": "/w/paysvc"}, root=tmp_path, store=store)

    hits = prompts.search("scheduler", store=store)
    assert {h["slug"] for h in hits} == {"foreman", "paysvc"}
    assert hits[0]["ts"] >= hits[-1]["ts"]                       # newest first
    assert prompts.search("", store=store) == []                # empty query -> no results
    # a LIKE wildcard in the query is treated literally, not as SQL glob
    assert prompts.search("%", store=store) == []

    wk = {w["slug"]: w["count"] for w in prompts.counts_since("2026-09-07T00:00:00Z", store=store)}
    assert wk == {"foreman": 2, "paysvc": 1}                    # the August note is excluded

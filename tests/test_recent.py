"""The local-only 'recently worked on' reader: one row per project (its last interactive human
prompt), via a bounded tail. Never touches the real ~/.claude (a temp root is injected)."""
import json
import os

from collectors import recent


def _dir(root, cwd):
    d = root / recent.encode_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _human(text, cwd):
    return {"type": "user", "cwd": cwd, "message": {"role": "user", "content": text}}


def test_roster_is_per_project_last_human_prompt(tmp_path):
    _write(_dir(tmp_path, "/w/foreman") / "s.jsonl", [
        _human("first foreman ask", "/w/foreman"),
        {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},   # skip
        _human("latest foreman ask", "/w/foreman"),                                 # most recent wins
    ])
    _write(_dir(tmp_path, "/w/paysvc") / "s.jsonl", [_human("fix paysvc CI", "/w/paysvc")])
    cwds = {"foreman": "/w/foreman", "paysvc": "/w/paysvc", "nope": "/w/nope"}
    roster = recent.recent_by_project(cwds, root=tmp_path)
    by = {r["slug"]: r["prompt"] for r in roster}
    assert by == {"foreman": "latest foreman ask", "paysvc": "fix paysvc CI"}     # nope omitted


def test_skips_foreman_probes_and_cadence_prompts(tmp_path):
    # a project whose only recent turns are foreman's own headless prompts yields nothing
    _write(_dir(tmp_path, "/w/probed") / "s.jsonl", [
        _human("Report as JSON only: active model, permission mode, plugins", "/w/probed"),
        _human("# ui-ux-review", "/w/probed"),
        _human("You are running the docs-sync cadence for one project", "/w/probed"),
    ])
    _write(_dir(tmp_path, "/w/real") / "s.jsonl", [_human("add a dark mode toggle", "/w/real")])
    roster = recent.recent_by_project({"probed": "/w/probed", "real": "/w/real"}, root=tmp_path)
    assert [r["slug"] for r in roster] == ["real"]                                  # probed dropped


def test_sorted_by_recency_and_limited(tmp_path):
    for i, slug in enumerate(["a", "b", "c"]):
        f = _dir(tmp_path, f"/w/{slug}") / "s.jsonl"
        _write(f, [_human(f"work on {slug}", f"/w/{slug}")])
        os.utime(f, (1000 + i, 1000 + i))                     # c newest, a oldest
    roster = recent.recent_by_project({"a": "/w/a", "b": "/w/b", "c": "/w/c"}, limit=2, root=tmp_path)
    assert [r["slug"] for r in roster] == ["c", "b"]          # newest first, capped at 2


def test_falls_back_past_a_probe_only_newest_session(tmp_path):
    d = _dir(tmp_path, "/w/proj")
    old = d / "old.jsonl"
    new = d / "new.jsonl"
    _write(old, [_human("the real work I did", "/w/proj")])
    _write(new, [_human("Report as JSON only: config probe", "/w/proj")])   # newest = probe only
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    roster = recent.recent_by_project({"proj": "/w/proj"}, root=tmp_path)
    assert roster and roster[0]["prompt"] == "the real work I did"          # looked back one file


def test_empty_or_missing_root(tmp_path):
    assert recent.recent_by_project({"a": "/w/a"}, root=tmp_path / "nope") == []
    assert recent.recent_by_project({}, root=tmp_path) == []

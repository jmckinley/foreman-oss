"""Project discovery + registration: remote parsing, register_project guardrails,
scan_dir_for_repos, register_from_dir (dry-run vs apply), and the CLI."""
import subprocess

import pytest
import yaml

from collectors import discover

_SEED = """version: 1
hosts:
  mbp: {os: darwin, role: interactive}
defaults:
  marketplace: github:x/y
  marketplace_pin: "0"
  context_budget_tokens: 100
  receipt_branch: state
  spool_dir: /tmp
projects:
  - slug: seed
    repo: o/seed
    default_branch: main
    worktree:
      mbp: /tmp/nonexistent-seed-xyz
    tier_default: local
    cadences: [docs-sync]
    escalation:
      github_issues: false
"""


@pytest.fixture
def fdir(tmp_path):
    d = tmp_path / "fm"
    d.mkdir()
    (d / "registry.yaml").write_text(_SEED)
    return d


def _repo(path, origin=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def test_parse_remote_forms():
    assert discover._parse_remote("https://github.com/acme/Widget.git") == ("acme", "Widget")
    assert discover._parse_remote("git@github.com:acme/widget.git") == ("acme", "widget")
    assert discover._parse_remote("not a url") is None


def test_slugify():
    assert discover._slugify("My_Cool.Repo") == "my-cool-repo"


def test_git_remote(tmp_path):
    r = _repo(tmp_path / "w", "https://github.com/acme/widget.git")
    assert discover.git_remote(r) == ("acme", "widget")
    assert discover.git_remote(_repo(tmp_path / "nr")) is None       # no origin


def test_register_project_happy_and_dupes(fdir, tmp_path):
    r = _repo(tmp_path / "widget", "https://github.com/acme/widget.git")
    res = discover.register_project(fdir, r)
    assert res["registered"] == "widget" and res["repo"] == "acme/widget"
    reg = yaml.safe_load((fdir / "registry.yaml").read_text())
    assert any(p["slug"] == "widget" for p in reg["projects"])
    # registering the same dir again is a no-op (resolve_project matches)
    assert discover.register_project(fdir, r)["skipped"] == "already registered"


def test_register_project_guardrails(fdir, tmp_path):
    # no origin and no explicit repo -> error
    with pytest.raises(ValueError):
        discover.register_project(fdir, _repo(tmp_path / "bare"))
    # owner filter that doesn't match -> skipped
    r = _repo(tmp_path / "o", "https://github.com/other/thing.git")
    assert "skipped" in discover.register_project(fdir, r, owner="acme")
    # malformed explicit repo
    with pytest.raises(ValueError):
        discover.register_project(fdir, _repo(tmp_path / "x"), repo="noslash")


def test_register_project_slug_collision(fdir, tmp_path):
    r1 = _repo(tmp_path / "a" / "widget", "https://github.com/acme/widget.git")
    r2 = _repo(tmp_path / "b" / "widget", "https://github.com/other/widget.git")
    assert discover.register_project(fdir, r1)["registered"] == "widget"
    assert discover.register_project(fdir, r2)["skipped"].startswith("slug")


def test_scan_dir_for_repos(tmp_path):
    base = tmp_path / "code"
    _repo(base / "one", "https://github.com/acme/one.git")
    _repo(base / "two", "https://github.com/other/two.git")
    _repo(base / "three")                       # no origin -> ignored
    (base / "plain").mkdir()                     # not a repo -> ignored
    found = discover.scan_dir_for_repos(base)
    assert {e["repo"] for e in found} == {"acme/one", "other/two"}
    assert [e["repo"] for e in discover.scan_dir_for_repos(base, owner="acme")] == ["acme/one"]
    assert discover.scan_dir_for_repos(tmp_path / "nope") == []


def test_register_from_dir_dryrun_then_apply(fdir, tmp_path):
    base = tmp_path / "code"
    _repo(base / "one", "https://github.com/acme/one.git")
    dry = discover.register_from_dir(fdir, base, apply=False)
    assert dry and dry[0]["would_register"] == "one"
    reg = yaml.safe_load((fdir / "registry.yaml").read_text())
    assert not any(p["slug"] == "one" for p in reg["projects"])     # dry-run wrote nothing
    applied = discover.register_from_dir(fdir, base, apply=True)
    assert applied[0]["registered"] == "one"
    # a second apply skips it
    assert discover.register_from_dir(fdir, base, apply=True)[0]["skipped"] == "already registered"


def test_cli_register_and_scan(fdir, tmp_path, capsys):
    r = _repo(tmp_path / "widget", "https://github.com/acme/widget.git")
    assert discover.main(["--foreman-dir", str(fdir), "register", str(r)]) == 0
    assert "widget" in capsys.readouterr().out
    base = tmp_path / "code"
    _repo(base / "two", "https://github.com/acme/two.git")
    assert discover.main(["--foreman-dir", str(fdir), "scan", str(base)]) == 0
    assert "dry-run" in capsys.readouterr().out

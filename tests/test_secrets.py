"""Secret resolution and .env materialisation: the DictBackend, scope precedence, push /
push_all / rotate, index inventory, expiry, and the .env scanners/fingerprints. No sops (that
path shells the binary); the logic is exercised through the in-memory backend."""
import datetime as dt

import pytest

from collectors import secrets as S, db


def _registry(tmp_path, env_keys=("API_KEY",)):
    wt = tmp_path / "proj"
    wt.mkdir()
    return {"projects": [{"slug": "alpha", "repo": "o/alpha",
                          "worktree": {"mbp": str(wt)}, "env_keys": list(env_keys)}]}, wt


def test_scoped_name_and_resolve_precedence():
    assert S.scoped_name("K", project="alpha") == "project:alpha/K"
    assert S.scoped_name("K", host="mbp") == "host:mbp/K"
    assert S.scoped_name("K") == "K"
    b = S.DictBackend({"K": "default", "host:mbp/K": "hostval", "project:alpha/K": "projval"})
    assert S.resolve_key(b, "K", "alpha", "mbp") == ("projval", "project")
    assert S.resolve_key(S.DictBackend({"K": "d", "host:mbp/K": "h"}), "K", "alpha", "mbp") == ("h", "host")
    assert S.resolve_key(S.DictBackend({"K": "d"}), "K", "alpha", "mbp") == ("d", "default")
    assert S.resolve_key(S.DictBackend({}), "K", "alpha", "mbp") == (None, "missing")


def test_write_env_and_parse_roundtrip(tmp_path):
    env = tmp_path / ".env"
    S.write_env(env, {"A": "plain", "B": "has space"})
    assert oct(env.stat().st_mode)[-3:] == "600"
    assert S._parse_env(env.read_text()) == {"A": "plain", "B": "has space"}
    # a single quote is escaped direnv-style (…'\''…) so the .env stays loadable by direnv
    assert S._dotenv_line("C", "a'b") == "C='a'\\''b'\n"


def test_parse_env_comment_and_quotes():
    txt = 'X=bare # trailing comment\nY="quoted # kept"\nexport Z=zz\n# not a key\n'
    assert S._parse_env(txt) == {"X": "bare", "Y": "quoted # kept", "Z": "zz"}


def test_push_writes_declared_keys_and_reports_missing(tmp_path):
    reg, wt = _registry(tmp_path, env_keys=("API_KEY", "MISSING_KEY"))
    backend = S.DictBackend({"API_KEY": "v1", "project:alpha/API_KEY": "override1"})
    res = S.push(backend, reg, "alpha", "mbp")
    assert res["written"] == ["API_KEY"] and res["missing"] == ["MISSING_KEY"]
    assert res["overrides"] == {"API_KEY": "project"}
    assert S._parse_env((wt / ".env").read_text())["API_KEY"] == "override1"
    with pytest.raises(ValueError):
        S.push(backend, reg, "ghost", "mbp")


def test_push_all_only_existing_worktrees(tmp_path):
    reg, _ = _registry(tmp_path)
    reg["projects"].append({"slug": "beta", "repo": "o/beta",
                            "worktree": {"mbp": str(tmp_path / "gone")}, "env_keys": ["API_KEY"]})
    out = S.push_all(S.DictBackend({"API_KEY": "v"}), reg, "mbp")
    assert [r["project"] for r in out] == ["alpha"]     # beta's worktree doesn't exist


def test_rotate_sets_and_pushes(tmp_path):
    reg, wt = _registry(tmp_path)
    backend = S.DictBackend({"API_KEY": "old"})
    res = S.rotate(backend, reg, "API_KEY", "new", "mbp")
    assert res["pushed_to"] == ["alpha"]
    assert backend.get("API_KEY") == "new"
    assert S._parse_env((wt / ".env").read_text())["API_KEY"] == "new"
    # project-scoped rotate writes an override key
    S.rotate(backend, reg, "API_KEY", "scoped", "mbp", scope_project="alpha")
    assert backend.get("project:alpha/API_KEY") == "scoped"


def test_inventory_and_expiring(tmp_path):
    reg, _ = _registry(tmp_path)
    backend = S.DictBackend({"API_KEY": "v", "project:alpha/API_KEY": "o"})
    conn = db.open_index(":memory:")
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    n = S.inventory(conn, backend, reg, now=now,
                    expiry={"API_KEY": "2026-01-10T00:00:00Z"})
    assert n >= 1
    row = conn.execute("SELECT scope FROM credential WHERE name='API_KEY'").fetchone()
    assert row["scope"] == "shared"                    # present in the store
    assert conn.execute("SELECT 1 FROM credential WHERE name='project:alpha/API_KEY'").fetchone()
    exp = S.expiring_credentials(conn, now=now, days=14)
    assert any(e["name"] == "API_KEY" and e["days_left"] == 9 for e in exp)
    assert S.expiring_credentials(conn, now=now, days=2) == []   # nothing within 2 days


def test_scanners_and_fingerprints(tmp_path):
    wt = tmp_path / "proj"
    wt.mkdir()
    (wt / ".env").write_text("API_KEY=abcde12345\nEXTRA=zzz\n")
    (wt / ".env.example").write_text("TEMPLATE_ONLY=x\n")   # template ignored
    assert set(S.scan_env_files(wt)) == {"API_KEY", "EXTRA"}
    assert S.unmanaged_env_keys(wt, ["API_KEY"]) == ["EXTRA"]
    dig = S.env_key_digests(wt)
    assert dig["API_KEY"]["fp"] == "…12345"
    assert len(dig["API_KEY"]["hash"]) == 64
    assert S.env_fingerprints(wt)["EXTRA"] == "…zzz"


class _CP:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_sops_backend(tmp_path, monkeypatch):
    store = tmp_path / "store.sops.yaml"
    store.write_text("A: enc\n")
    monkeypatch.setattr(S.shutil, "which", lambda _n: "/usr/bin/sops")

    def run(args, **k):
        if "--extract" in args:
            return _CP(0, "secretval\n")
        if "--set" in args:
            return _CP(0)
        if args[:2] == ["sops", "-d"]:
            return _CP(0, "A: 1\nB: 2\n")
        return _CP(1)

    monkeypatch.setattr(S.subprocess, "run", run)
    b = S.SopsBackend(store)
    assert b.available() is True
    assert b.get("A") == "secretval"
    b.set("A", "v")                                   # check=True path, stubbed rc 0
    assert b.list_names() == ["A", "B"]


def test_sops_requires_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda _n: None)
    b = S.SopsBackend(tmp_path / "s.sops.yaml")
    assert b.available() is False                     # no binary
    with pytest.raises(RuntimeError):
        b.get("K")


def test_cli_set_get_list_push(tmp_path, monkeypatch, capsys):
    reg, wt = _registry(tmp_path)
    fd = tmp_path / "fm"
    (fd / "secrets").mkdir(parents=True)
    import yaml as _yaml
    (fd / "registry.yaml").write_text(_yaml.safe_dump(reg))
    # back the CLI with the in-memory DictBackend instead of real sops
    store = S.DictBackend({"API_KEY": "v1"})
    monkeypatch.setattr(S, "SopsBackend", lambda _path: store)
    monkeypatch.setenv("FOREMAN_DIR", str(fd))
    assert S.main(["--foreman-dir", str(fd), "list"]) == 0
    assert "API_KEY" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("newval\n"))
    assert S.main(["--foreman-dir", str(fd), "set", "API_KEY", "--project", "alpha"]) == 0
    assert store.get("project:alpha/API_KEY") == "newval"
    assert S.main(["--foreman-dir", str(fd), "--host", "mbp", "push", "alpha"]) == 0
    assert (wt / ".env").is_file()


def test_env_files_and_is_live_whitelist(tmp_path):
    wt = tmp_path / "proj"
    wt.mkdir()
    (wt / ".env").write_text("A=1\n")
    (wt / ".env.bak").write_text("A=2\n")               # backup excluded
    files = S.env_files(wt)
    assert [f.name for f in files] == [".env"]
    assert S.is_live_env_file(str(wt / ".env"), [str(wt)]) is True
    assert S.is_live_env_file("/etc/passwd", [str(wt)]) is False
    assert S.is_live_env_file(str(wt / ".env.bak"), [str(wt)]) is False

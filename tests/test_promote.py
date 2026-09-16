"""promote — move a skill into the plugin, bump versions, and verify it installed via the probe."""
import json

import pytest

from collectors import promote as P


def _scaffold(tmp_path):
    fd = tmp_path / "fm"
    plug = fd / ".claude-plugin" / "foreman-ops" / ".claude-plugin"
    plug.mkdir(parents=True)
    (plug / "plugin.json").write_text(json.dumps({"name": "foreman-ops", "version": "0.1.0"}))
    (fd / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"metadata": {"version": "0.1.0"}}))
    (fd / "registry.yaml").write_text('version: 1\ndefaults:\n  marketplace_pin: "0.1.0"\n'
                                      'projects:\n  - slug: alpha\n')
    src = tmp_path / "src"
    (src / "myskill").mkdir(parents=True)
    (src / "myskill" / "SKILL.md").write_text("---\nname: myskill\n---\ndo the thing\n")
    return fd, src


def test_bump_patch():
    assert P.bump_patch("0.4.2") == "0.4.3"
    assert P.bump_patch("1.9.99") == "1.9.100"
    for bad in ("1.2", "x.y.z", "1.2.3.4"):
        with pytest.raises(ValueError):
            P.bump_patch(bad)


def test_move_skill(tmp_path):
    fd, src = _scaffold(tmp_path)
    dest = P.move_skill(fd, "myskill", src)
    assert (dest / "SKILL.md").is_file()
    assert not (src / "myskill").exists()                 # moved, not copied
    with pytest.raises(ValueError):
        P.move_skill(fd, "ghost", src)                    # no SKILL.md


def test_bump_versions(tmp_path):
    fd, _ = _scaffold(tmp_path)
    new = P.bump_versions(fd)
    assert new == "0.1.1"
    pj = json.loads((fd / ".claude-plugin" / "foreman-ops" / ".claude-plugin" / "plugin.json").read_text())
    assert pj["version"] == "0.1.1"
    mkt = json.loads((fd / ".claude-plugin" / "marketplace.json").read_text())
    assert mkt["metadata"]["version"] == "0.1.1"
    assert 'marketplace_pin: "0.1.1"' in (fd / "registry.yaml").read_text()


def test_verify_installed():
    reg = {"projects": [{"slug": "a"}, {"slug": "b"}]}
    probes = {"a": {"skills": [{"name": "s", "invocable": True}]},
              "b": {"skills": [{"name": "s", "invocable": False}]}}
    out = P.verify_installed(reg, "s", probe_fn=lambda slug: probes.get(slug))
    assert out["a"] == {"installed": True, "invocable": True}
    assert out["b"] == {"installed": True, "invocable": False}


def test_promote_end_to_end(tmp_path):
    fd, src = _scaffold(tmp_path)
    reg = {"projects": [{"slug": "alpha"}]}
    out = P.promote(fd, reg, "myskill", source_dir=src,
                    probe_fn=lambda slug: {"skills": [{"name": "myskill", "invocable": True}]})
    assert out["skill"] == "myskill" and out["version"] == "0.1.1"
    assert out["propagated"] is True
    assert (fd / ".claude-plugin" / "foreman-ops" / "skills" / "myskill" / "SKILL.md").is_file()

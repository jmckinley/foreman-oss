"""init — scaffolding a fresh Foreman instance (dirs, contract, registry skeleton, index)."""
from collectors import init as I


def test_init_instance_scaffolds_and_is_idempotent(tmp_path):
    d = tmp_path / "inst"
    out = I.init_instance(d)
    assert out["registry_created"] is True and out["index_created"] is True
    assert out["needs_project"] is True                 # skeleton has no projects yet
    assert (d / "registry.yaml").is_file()
    assert (d / "index.db").is_file()
    assert (d / "schema").is_dir() and (d / "sql").is_dir()   # contract copied
    assert (d / "cadences").is_dir() and (d / "state").is_dir()

    # re-running is idempotent — existing registry/index are left untouched
    out2 = I.init_instance(d)
    assert out2["registry_created"] is False and out2["index_created"] is False


def test_has_project(tmp_path):
    d = tmp_path / "inst"
    I.init_instance(d)
    assert I._has_project(d / "registry.yaml") is False
    (d / "registry.yaml").write_text(
        "version: 1\ndefaults: {}\nprojects:\n  - slug: alpha\n    repo: o/alpha\n")
    assert I._has_project(d / "registry.yaml") is True


def test_init_with_first_project(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    d = tmp_path / "inst"
    out = I.init_instance(d, first_project=(str(proj), "owner/myproj"))
    assert out["registered"] and out["registered"].get("registered")
    assert out["needs_project"] is False
    assert "owner/myproj" in (d / "registry.yaml").read_text()

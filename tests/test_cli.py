"""One-command install: `foreman init` scaffolding and the `foreman` dispatcher (M8.3)."""

import json

from collectors import init as I, cli, validate as V


def test_init_scaffolds_valid_instance(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    inst = tmp_path / "inst"
    out = I.init_instance(inst, host="mbp", first_project=(str(proj), "me/myproj"))

    # layout
    for sub in ("state/receipts", "state/queue", "spool", "cadences", "prompts"):
        assert (inst / sub).is_dir()
    assert (inst / "registry.yaml").is_file() and (inst / "index.db").is_file()
    assert out["registry_created"] and out["registered"]["registered"] == "myproj"
    assert out["needs_project"] is False

    # a fresh instance with one project passes the gate
    assert V.validate(inst).ok()


def test_init_without_project_flags_incomplete(tmp_path):
    inst = tmp_path / "inst"
    out = I.init_instance(inst)
    assert out["needs_project"] is True                 # honest: no project yet
    assert (inst / "index.db").is_file()


def test_init_is_idempotent(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    inst = tmp_path / "inst"
    I.init_instance(inst, first_project=(str(proj), "me/p"))
    before = (inst / "registry.yaml").read_text()
    out2 = I.init_instance(inst)                          # re-run
    assert out2["registry_created"] is False and out2["index_created"] is False
    assert (inst / "registry.yaml").read_text() == before  # not clobbered
    assert V.validate(inst).ok()


def test_cli_dispatch_init_and_validate(tmp_path, capsys):
    proj = tmp_path / "p"
    proj.mkdir()
    inst = tmp_path / "inst"
    assert cli.main(["init", str(inst), "--register", str(proj), "--repo", "me/p", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["registered"]["registered"] == "p"

    assert cli.main(["validate", str(inst)]) == 0        # validate routes to the instance
    assert "validate: OK" in capsys.readouterr().out


def test_cli_unknown_command(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err

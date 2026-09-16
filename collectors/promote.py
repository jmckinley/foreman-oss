"""/promote -- move a skill into the marketplace plugin (SPEC.md sections 11).

Moves a user skill into ``foreman-ops``, bumps the plugin + marketplace version and the
registry pin together, then VERIFIES installation per project via the C6 probe rather than
assuming propagation -- there is an open report that ``extraKnownMarketplaces`` +
``enabledPlugins`` does not always trigger the install prompt, so a populated cache is not
proof the skill loaded.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from collectors.decisions import _edit_registry_pin


def bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a semver patch version: {version!r}")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def _plugin_dir(foreman_dir: Path) -> Path:
    return foreman_dir / ".claude-plugin" / "foreman-ops"


def move_skill(foreman_dir: Path, skill_name: str, source_dir: Path) -> Path:
    src = source_dir / skill_name
    if not (src / "SKILL.md").is_file():
        raise ValueError(f"no SKILL.md under {src}")
    dest = _plugin_dir(foreman_dir) / "skills" / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest


def bump_versions(foreman_dir: Path) -> str:
    """Bump plugin.json, marketplace metadata, and the registry default pin to one new patch."""
    plugin_json = _plugin_dir(foreman_dir) / ".claude-plugin" / "plugin.json"
    pj = json.loads(plugin_json.read_text())
    new_ver = bump_patch(pj["version"])
    pj["version"] = new_ver
    plugin_json.write_text(json.dumps(pj, indent=2) + "\n")

    mkt_path = foreman_dir / ".claude-plugin" / "marketplace.json"
    mkt = json.loads(mkt_path.read_text())
    mkt.setdefault("metadata", {})["version"] = new_ver
    mkt_path.write_text(json.dumps(mkt, indent=2) + "\n")

    reg_path = foreman_dir / "registry.yaml"
    reg_path.write_text(_edit_registry_pin(reg_path.read_text(), "defaults", new_ver))
    return new_ver


def verify_installed(registry: dict, skill_name: str, *, probe_fn) -> dict:
    """Per project, ask the live probe whether the skill is present and invocable."""
    results = {}
    for p in registry.get("projects", []):
        probe = probe_fn(p["slug"]) or {}
        skills = probe.get("skills") or []
        match = next((s for s in skills if s.get("name") == skill_name), None)
        results[p["slug"]] = {
            "installed": match is not None,
            "invocable": bool(match and match.get("invocable")),
        }
    return results


def promote(foreman_dir, registry: dict, skill_name: str, *, source_dir, probe_fn) -> dict:
    foreman_dir = Path(foreman_dir)
    move_skill(foreman_dir, skill_name, Path(source_dir))
    new_ver = bump_versions(foreman_dir)
    verified = verify_installed(registry, skill_name, probe_fn=probe_fn)
    return {"skill": skill_name, "version": new_ver, "verified": verified,
            "propagated": all(v["invocable"] for v in verified.values()) if verified else False}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import yaml
    from collectors import config_resolve

    ap = argparse.ArgumentParser(prog="promote")
    ap.add_argument("skill")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--source-dir", default=os.path.expanduser("~/.claude/skills"))
    args = ap.parse_args(argv)
    foreman_dir = Path(args.foreman_dir)
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())

    def probe(slug):
        # C6 probe from each project's worktree, best-effort.
        proj = next((p for p in registry["projects"] if p["slug"] == slug), None)
        wt = next(iter((proj or {}).get("worktree", {}).values()), None)
        return config_resolve._claude_probe(Path(wt)) if wt else None

    print(json.dumps(promote(foreman_dir, registry, args.skill,
                             source_dir=args.source_dir, probe_fn=probe), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

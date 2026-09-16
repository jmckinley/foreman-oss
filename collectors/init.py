"""``foreman init`` -- scaffold a Foreman instance in one command (BUILD.md M8.3).

An instance is a directory Foreman reads and writes: ``registry.yaml`` (the roster), ``state/``
(receipts + the decision queue), ``spool/`` (telemetry + partials), and ``index.db`` (the
rebuildable cache). This creates that layout, writes a minimal valid registry for the current
host, optionally registers a first project, and optionally installs the scheduler + telemetry
services. Idempotent: re-running never clobbers an existing registry or index.

Cadence *files* are not copied -- a project may list cadences whose files do not exist yet
(validate treats that as not-yet-active, not an error), and loops are materialized on demand
with ``loops enable``. So a fresh instance with one project passes ``collectors.validate``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_SUBDIRS = ("state/receipts", "state/queue", "spool", "cadences", "prompts")
# Contract files the instance needs to be self-validating, copied from the package. Cadence
# and prompt files are intentionally NOT copied: an instance starts with none and materializes
# them per project via `loops enable`.
_CONTRACT_DIRS = ("schema", "sql")
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _registry_skeleton(host: str) -> str:
    os_name = "darwin" if sys.platform == "darwin" else "linux"
    return (
        "version: 1\n\n"
        "hosts:\n"
        f"  {host}:\n"
        f"    os: {os_name}\n"
        "    role: interactive\n\n"
        "defaults:\n"
        "  marketplace: github:you/foreman        # your marketplace, once you have one\n"
        '  marketplace_pin: "0.1.0"\n'
        "  context_budget_tokens: 2400            # always-on plugin cost ceiling per session\n"
        "  receipt_branch: state\n"
        "  spool_dir: ~/foreman/spool\n"
    )


def init_instance(instance_dir, *, host: str = "mbp", first_project: tuple | None = None,
                  with_services: bool = False) -> dict:
    """Scaffold an instance at ``instance_dir``. ``first_project`` is ``(path, repo_or_None)``.

    Returns a summary. A registry or index that already exists is left untouched (idempotent),
    so re-running to add services or a project is safe.
    """
    from collectors import db

    d = Path(os.path.expanduser(str(instance_dir)))
    for sub in _SUBDIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)

    # Copy the contract (schemas + DDL) so the instance validates on its own. Idempotent:
    # dirs_exist_ok overwrites the shipped files, never touches registry/cadences/state.
    for name in _CONTRACT_DIRS:
        src = _PACKAGE_ROOT / name
        if src.is_dir():
            shutil.copytree(src, d / name, dirs_exist_ok=True)

    reg = d / "registry.yaml"
    registry_created = not reg.exists()
    if registry_created:
        reg.write_text(_registry_skeleton(host))

    index_path = d / "index.db"
    index_created = not index_path.exists()
    db.open_index(str(index_path)).close()          # migrate/create the cache

    registered = None
    if first_project is not None:
        from collectors import discover
        path, repo = first_project
        registered = discover.register_project(d, path, host=host, repo=repo)

    services: list[str] = []
    if with_services:
        from collectors import scheduler, telemetry
        spool = d / "spool"
        try:
            scheduler.install_launchd(
                foreman_dir=str(d), state_dir=str(d / "state"),
                index_path=str(index_path), spool=str(spool), host=host,
                mode="auto", interval=300)
            services.append(scheduler.LABEL)
        except Exception as exc:                    # host may lack launchctl/systemctl
            services.append(f"scheduler: not installed ({type(exc).__name__})")
        try:
            telemetry.install_receiver(spool=str(spool))
            services.append(telemetry.LABEL)
        except Exception as exc:
            services.append(f"telemetry: not installed ({type(exc).__name__})")

    has_project = bool(registered and registered.get("registered")) or _has_project(reg)
    return {"instance": str(d), "registry_created": registry_created,
            "index_created": index_created, "registered": registered,
            "services": services, "needs_project": not has_project}


def _has_project(reg_path: Path) -> bool:
    import yaml
    try:
        data = yaml.safe_load(reg_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(data.get("projects"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="foreman init", description="scaffold a Foreman instance")
    ap.add_argument("dir", nargs="?", default=os.environ.get("FOREMAN_DIR", "~/foreman"),
                    help="instance directory (default: $FOREMAN_DIR or ~/foreman)")
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    ap.add_argument("--register", metavar="PATH", help="register a first project at PATH")
    ap.add_argument("--repo", help="OWNER/REPO for the first project (else its git origin)")
    ap.add_argument("--with-services", action="store_true",
                    help="install the scheduler + telemetry services for this host")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    first = (args.register, args.repo) if args.register else None
    out = init_instance(args.dir, host=args.host, first_project=first,
                        with_services=args.with_services)
    if args.json:
        print(json.dumps(out, default=str))
    else:
        print(f"initialized instance at {out['instance']}")
        if out["registered"] and out["registered"].get("registered"):
            print(f"  registered first project: {out['registered']['registered']}")
        for s in out["services"]:
            print(f"  service: {s}")
        if out["needs_project"]:
            print("  next: register a project — foreman discover register <path> "
                  "(or re-run init with --register)")
        print(f"  next: foreman validate && foreman collect --host {args.host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

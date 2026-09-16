"""The ``foreman`` console entry point -- one command that dispatches to the collectors.

Installed as ``foreman`` (see pyproject ``[project.scripts]``), so ``foreman init``,
``foreman collect``, ``foreman brief`` etc. work without the ``python -m collectors.<x>``
prefix. Each subcommand delegates to the module's own ``main(argv)``; this file only routes.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# subcommand -> module under collectors. Every target exposes main(argv) except validate,
# which is handled specially below (its main takes no argv).
_ROUTES = {
    "init": "collectors.init",
    "collect": "collectors.collect",
    "brief": "collectors.brief",
    "web": "collectors.web",
    "scheduler": "collectors.scheduler",
    "telemetry": "collectors.telemetry",
    "dispatch": "collectors.dispatch",
    "discover": "collectors.discover",
    "loops": "collectors.loops",
    "decisions": "collectors.decisions",
    "escalations": "collectors.escalations",
    "promote": "collectors.promote",
    "retention": "collectors.retention",
    "secrets": "collectors.secrets",
    "config": "collectors.config_resolve",
    "keyscan": "collectors.keyscan",
    "validate": "collectors.validate",
}


def _usage() -> str:
    cmds = "  ".join(sorted(_ROUTES))
    return (f"foreman <command> [args]\n\ncommands:\n  {cmds}\n\n"
            "run 'foreman <command> -h' for a command's options")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0 if argv else 1
    cmd, rest = argv[0], argv[1:]
    module = _ROUTES.get(cmd)
    if module is None:
        print(f"foreman: unknown command {cmd!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    mod = importlib.import_module(module)
    if cmd == "validate":
        # validate.main() takes no argv; honour an optional path/--foreman-dir here.
        root = rest[0] if rest and not rest[0].startswith("-") else \
            os.environ.get("FOREMAN_DIR", ".")
        rep = mod.validate(Path(root))
        if rep.ok():
            print("validate: OK (registry + all cadences pass schema and tier constraints)")
            return 0
        print("validate: FAILED", file=sys.stderr)
        for line in rep.errors:
            print(f"  - {line}", file=sys.stderr)
        return 1
    return mod.main(rest)


if __name__ == "__main__":
    raise SystemExit(main())

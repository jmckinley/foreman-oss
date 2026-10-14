"""``foreman doctor`` -- a preflight that tells you exactly what's missing and how to fix it.

The hardest part of adopting Foreman is wiring: a state worktree, ``gh`` auth, the ``claude``
binary for the drift probe, a scheduler, a notification channel. Doctor runs every check,
prints a pass/warn/fail line with a copy-pasteable fix for anything that isn't ready, and
exits non-zero only on a hard failure -- so it is safe in a setup script and honest about what
still needs doing. It reads only; it changes nothing.

CLI::

    foreman doctor                 # check the default instance (~/foreman)
    foreman doctor ~/work/foreman  # check a specific instance
    foreman doctor --json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def _check(name, status, detail, hint="") -> dict:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _cmd_ok(args, timeout=10) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def run_checks(instance_dir: Path, env=None) -> list[dict]:
    env = env if env is not None else os.environ
    checks: list[dict] = []

    # Python
    v = sys.version_info
    checks.append(_check("python", OK if v >= (3, 11) else FAIL,
                         f"{v.major}.{v.minor}.{v.micro}",
                         "" if v >= (3, 11) else "Foreman needs Python ≥ 3.11"))

    # git (hard requirement -- receipts + worktrees)
    if shutil.which("git"):
        checks.append(_check("git", OK, "found"))
    else:
        checks.append(_check("git", FAIL, "not found", "install git — Foreman is git-native"))

    # gh (needed for GitHub signals + remediation PRs; degrades, so warn not fail)
    if shutil.which("gh"):
        authed, _ = _cmd_ok(["gh", "auth", "status"])
        checks.append(_check("gh", OK if authed else WARN,
                             "authed" if authed else "installed, not authed",
                             "" if authed else "run `gh auth login` for PRs + private-repo signals"))
    else:
        checks.append(_check("gh", WARN, "not found",
                             "install the GitHub CLI for PR + CI + security-alert signals"))

    # claude (the effective-config drift probe is the moat; warn if absent)
    checks.append(_check("claude", OK if shutil.which("claude") else WARN,
                         "found" if shutil.which("claude") else "not found",
                         "" if shutil.which("claude") else
                         "install Claude Code — the `claude -p` drift probe needs it"))

    # instance dir + registry
    reg = instance_dir / "registry.yaml"
    if reg.is_file():
        checks.append(_check("instance", OK, str(instance_dir)))
        checks.append(_registry_check(instance_dir))
    else:
        checks.append(_check("instance", FAIL, f"no registry.yaml at {instance_dir}",
                             f"run `foreman init {instance_dir}`"))

    # state worktree
    state = Path(os.path.expanduser(env.get("FOREMAN_STATE_DIR") or str(instance_dir / "state")))
    if (state / "receipts").is_dir():
        checks.append(_check("state", OK, str(state)))
    else:
        checks.append(_check("state", WARN, f"no receipts dir at {state}",
                             "create the state branch worktree: "
                             f"`git worktree add {state} state` (or `foreman init`)"))

    # index (a cache; its absence is only info)
    idx = instance_dir / "index.db"
    checks.append(_check("index", OK if idx.is_file() else WARN,
                         "present" if idx.is_file() else "not built yet",
                         "" if idx.is_file() else "run `foreman collect` to build the cache"))

    # notifications
    checks.append(_notify_check(env))

    # scheduler (best-effort; a warn, since session/cloud tiers don't need it)
    checks.append(_scheduler_check())

    return checks


def _registry_check(instance_dir: Path) -> dict:
    try:
        from collectors import validate
        rep = validate.validate(instance_dir)
        if rep.ok():
            return _check("validate", OK, "registry + cadences pass")
        return _check("validate", FAIL, f"{len(rep.errors)} error(s)",
                      "run `foreman validate` to see them")
    except Exception as exc:
        return _check("validate", WARN, f"could not validate ({type(exc).__name__})")


def _notify_check(env) -> dict:
    try:
        from collectors import notify
        cfg = notify.channel_config(env)
    except Exception:
        return _check("notify", WARN, "unavailable")
    if cfg["kind"] == "none":
        return _check("notify", WARN, "no channel configured",
                      "set FOREMAN_NOTIFY_WEBHOOK (Slack/Discord) or FOREMAN_NOTIFY_FILE "
                      "to get pinged on reds")
    return _check("notify", OK, f"channel: {cfg['kind']}")


def _scheduler_check() -> dict:
    if sys.platform == "darwin":
        ok, out = _cmd_ok(["launchctl", "list"])
        if ok and "foreman" in out.lower():
            return _check("scheduler", OK, "launchd agent loaded")
        return _check("scheduler", WARN, "no launchd agent",
                      "install it with `foreman init --with-services` (local-tier loops need it)")
    ok, out = _cmd_ok(["systemctl", "--user", "list-units", "--type=timer"])
    if ok and "foreman" in out.lower():
        return _check("scheduler", OK, "systemd timer loaded")
    return _check("scheduler", WARN, "no systemd timer",
                  "install it with `foreman init --with-services` (local-tier loops need it)")


def render(checks: list[dict]) -> str:
    lines = ["foreman doctor"]
    for c in checks:
        head = f"  {_MARK[c['status']]} {c['name']:<11} {c['detail']}"
        lines.append(head.rstrip())
        if c["status"] != OK and c["hint"]:
            lines.append(f"      → {c['hint']}")
    fails = sum(1 for c in checks if c["status"] == FAIL)
    warns = sum(1 for c in checks if c["status"] == WARN)
    tail = "all clear" if not fails and not warns else f"{fails} to fix, {warns} optional"
    lines.append(f"  — {tail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="foreman doctor", description="preflight an instance")
    ap.add_argument("dir", nargs="?",
                    default=os.environ.get("FOREMAN_DIR", "~/foreman"),
                    help="instance directory (default: $FOREMAN_DIR or ~/foreman)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    checks = run_checks(Path(os.path.expanduser(args.dir)))
    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        print(render(checks))
    return 1 if any(c["status"] == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())

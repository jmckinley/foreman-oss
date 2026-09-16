"""Assisted project discovery (the answer to "ongoing autodiscovery").

Foreman is declare-don't-discover by design (SPEC §2: single operator, known projects). But
it need not be blind: every Claude Code session records its cwd, and any cwd that does not
resolve to a registered project is a project Foreman does not know about. This surfaces those
so the operator can register them -- assisted, not automatic, because silently supervising
whatever directory a session happened to open would be a footgun.

Two data sources, same logic:
- ``discover_from_roots``: scan the session transcript roots directly (works before C1 runs).
- ``discover_from_index``: read sessions with a null project (the "ongoing" path, once C1 has
  populated the index); surfaced as a DISCOVERED line in the brief.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from collectors import decisions, transcripts

DEFAULT_ROOTS = [("~/.claude/projects", "cli")]
_TEMP_MARKERS = ("/var/folders/", "/tmp/", "/T/tmp", "pytest-of-")
_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def _resolve(registry: dict, cwd: str) -> bool:
    return decisions.resolve_project(registry, Path(cwd)) is not None


def _skip(cwd: str) -> bool:
    """Ignore ephemeral/scratch dirs and paths that no longer exist -- not real projects."""
    if any(m in cwd for m in _TEMP_MARKERS):
        return True
    return not Path(cwd).is_dir()


def discover_from_roots(registry: dict, roots=DEFAULT_ROOTS) -> list[dict]:
    """Unregistered project dirs seen in session transcripts, by session count."""
    seen: dict[str, dict] = {}
    for root, _surface in roots:
        rp = Path(os.path.expanduser(root))
        if not rp.is_dir():
            continue
        for jsonl in rp.glob("*/*.jsonl"):
            cwd = transcripts._first_cwd(jsonl)
            if not cwd or _resolve(registry, cwd) or _skip(cwd):
                continue
            e = seen.setdefault(cwd, {"cwd": cwd, "sessions": 0, "last_mtime": 0.0,
                                      "is_git": (Path(cwd) / ".git").exists()})
            e["sessions"] += 1
            try:
                e["last_mtime"] = max(e["last_mtime"], jsonl.stat().st_mtime)
            except OSError:
                pass
    # Real git repos first, then by session count.
    return sorted(seen.values(), key=lambda e: (not e["is_git"], -e["sessions"], e["cwd"]))


def discover_from_index(conn, registry: dict) -> list[dict]:
    """Unregistered project dirs from the index's session table (project IS NULL or unknown)."""
    rows = conn.execute(
        "SELECT cwd, COUNT(*) n, MAX(started) last FROM session "
        "WHERE cwd IS NOT NULL GROUP BY cwd").fetchall()
    out = []
    for r in rows:
        if r["cwd"] and not _resolve(registry, r["cwd"]):
            out.append({"cwd": r["cwd"], "sessions": r["n"], "last": r["last"]})
    return sorted(out, key=lambda e: (-e["sessions"], e["cwd"]))


def suggest_stanza(cwd: str, host: str = "mbp") -> str:
    """A registry project stanza the operator can paste in, derived from a discovered dir."""
    rr = git_remote(cwd)
    repo = f"{rr[0]}/{rr[1]}" if rr else f"OWNER/{Path(cwd).name.lower().replace('_', '-')}"
    return project_stanza(slug=_slugify(rr[1]) if rr else Path(cwd).name.lower().replace("_", "-"),
                          repo=repo, host=host, path=cwd)


# --------------------------------------------------------------- assisted registration

def _slugify(name: str) -> str:
    """A registry-legal slug (``^[a-z0-9][a-z0-9-]*$``): lowercase, and any run of other
    characters (``_``, ``.``, etc.) collapsed to a single dash."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _parse_remote(url: str):
    m = _REMOTE_RE.search(url.strip())
    return (m.group(1), m.group(2)) if m else None


def git_remote(path) -> tuple[str, str] | None:
    """``(owner, repo)`` from a directory's ``origin`` remote, or None. This is the reliable
    signal for matching a local dir to its GitHub project -- the folder name can differ."""
    try:
        out = subprocess.run(["git", "-C", str(os.path.expanduser(str(path))),
                              "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_remote(out.stdout) if out.returncode == 0 else None


def project_stanza(*, slug: str, repo: str, host: str, path: str,
                   tier: str = "local", cadences=("docs-sync",)) -> str:
    return (f"  - slug: {slug}\n"
            f"    repo: {repo}\n"
            f"    default_branch: main\n"
            f"    worktree:\n"
            f"      {host}: {path}\n"
            f"    tier_default: {tier}\n"
            f"    cadences: [{', '.join(cadences)}]\n"
            f"    escalation:\n"
            f"      github_issues: false\n")


def _append_project(text: str, stanza: str) -> str:
    """Insert a stanza as the last item under top-level ``projects:``, preserving the rest of
    the file (comments included)."""
    if not stanza.endswith("\n"):
        stanza += "\n"
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith("projects:")), None)
    if start is None:                       # no projects key yet -> create one at EOF
        sep = "" if text.endswith("\n") else "\n"
        return f"{text}{sep}\nprojects:\n{stanza}"
    end = len(lines)                        # end of the projects block: next top-level key
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace():
            end = j
            break
    head = "".join(lines[:end])
    if not head.endswith("\n"):
        head += "\n"
    return f"{head}\n{stanza}{''.join(lines[end:])}"


def register_project(foreman_dir, path, *, host: str = "mbp", repo: str | None = None,
                     slug: str | None = None, tier: str = "local",
                     cadences=("docs-sync",), owner: str | None = None) -> dict:
    """Register one local project directory in ``registry.yaml`` (comment-preserving). ``repo``
    defaults to the dir's git ``origin``; ``owner`` (if given) filters to that GitHub owner."""
    foreman_dir = Path(foreman_dir)
    path = Path(os.path.expanduser(str(path))).resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    reg_path = foreman_dir / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text())
    if decisions.resolve_project(reg, path) is not None:
        return {"skipped": "already registered", "path": str(path)}
    if repo is None:
        rr = git_remote(path)
        if rr is None:
            raise ValueError(f"{path} has no git 'origin' remote; pass repo=OWNER/REPO")
        repo = f"{rr[0]}/{rr[1]}"
    if "/" not in repo:
        raise ValueError(f"repo {repo!r} must be OWNER/REPO")
    repo_owner, repo_name = repo.split("/", 1)
    if owner and repo_owner != owner:
        return {"skipped": f"origin owner {repo_owner!r} != {owner!r}", "path": str(path)}
    slug = slug or _slugify(repo_name)
    if any(p["slug"] == slug for p in reg.get("projects", [])):
        return {"skipped": f"slug {slug!r} already registered", "path": str(path)}
    reg_path.write_text(_append_project(
        reg_path.read_text(),
        project_stanza(slug=slug, repo=repo, host=host, path=str(path),
                       tier=tier, cadences=cadences)))
    return {"registered": slug, "repo": repo, "path": str(path), "host": host}


def scan_dir_for_repos(base_dir, *, owner: str | None = None) -> list[dict]:
    """Immediate subdirectories of ``base_dir`` that are git repos with a GitHub ``origin``,
    optionally filtered to ``owner``. This is how a local projects folder is matched to GitHub."""
    base = Path(os.path.expanduser(str(base_dir)))
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue
        rr = git_remote(d)
        if rr is None or (owner and rr[0] != owner):
            continue
        out.append({"cwd": str(d), "owner": rr[0], "repo": f"{rr[0]}/{rr[1]}"})
    return out


def register_from_dir(foreman_dir, base_dir, *, host: str = "mbp",
                      owner: str | None = None, apply: bool = False) -> list[dict]:
    """Scan ``base_dir`` for git repos (optionally owner-filtered) and register the unregistered
    ones. Dry-run unless ``apply`` -- returns what would be / was registered, plus skips."""
    foreman_dir = Path(foreman_dir)
    results = []
    for e in scan_dir_for_repos(base_dir, owner=owner):
        reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
        if decisions.resolve_project(reg, Path(e["cwd"])) is not None \
                or any(p["slug"] == _slugify(e["repo"].split("/")[1])
                       for p in reg.get("projects", [])):
            results.append({**e, "skipped": "already registered"})
        elif not apply:
            results.append({**e, "would_register": _slugify(e["repo"].split("/", 1)[1])})
        else:
            results.append(register_project(foreman_dir, e["cwd"], host=host, repo=e["repo"]))
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(prog="discover")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--root", action="append", help="session root(s) to scan")
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    sub = ap.add_subparsers(dest="cmd")
    pr = sub.add_parser("register", help="register one project directory")
    pr.add_argument("path")
    pr.add_argument("--repo", help="OWNER/REPO (default: from the dir's git origin)")
    pr.add_argument("--tier", default="local")
    ps = sub.add_parser("scan", help="scan a base dir and register matching git repos")
    ps.add_argument("base", help="the folder your projects live in, e.g. ~")
    ps.add_argument("--owner", help="only register repos whose GitHub owner matches")
    ps.add_argument("--apply", action="store_true", help="write registry.yaml (else dry-run)")
    args = ap.parse_args(argv)
    fdir = Path(args.foreman_dir)

    if args.cmd == "register":
        try:
            res = register_project(fdir, args.path, host=args.host, repo=args.repo,
                                   tier=args.tier)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(res))
        return 0 if res.get("registered") else 1
    if args.cmd == "scan":
        results = register_from_dir(fdir, args.base, host=args.host, owner=args.owner,
                                    apply=args.apply)
        for r in results:
            verb = ("registered" if r.get("registered") else
                    "would register" if r.get("would_register") else
                    f"skip ({r.get('skipped')})")
            print(f"  {verb:<28} {r.get('repo', ''):<28} {r.get('cwd') or r.get('path', '')}")
        n = sum(1 for r in results if r.get("registered"))
        if not args.apply:
            n2 = sum(1 for r in results if r.get("would_register"))
            print(f"\ndry-run: {n2} would be registered. Re-run with --apply to write "
                  f"registry.yaml, then `python -m collectors.validate` and commit.")
        elif n:
            print(f"\nregistered {n}. Run `python -m collectors.validate` and commit "
                  f"registry.yaml.")
        return 0

    registry = yaml.safe_load((fdir / "registry.yaml").read_text())
    roots = [(r, "cli") for r in args.root] if args.root else DEFAULT_ROOTS
    found = discover_from_roots(registry, roots)
    if not found:
        print("No unregistered projects found in session history.")
        return 0
    reg_path = (fdir / "registry.yaml").resolve()
    print(f"Discovered {len(found)} unregistered project dir(s) with session activity:\n")
    for e in found:
        print(f"  {e['sessions']:>3} sessions  {e['cwd']}")
    print(f"\nRegister one:   python -m collectors.discover register {found[0]['cwd']}")
    print(f"Register a folder of repos: python -m collectors.discover scan ~ --apply")
    print(f"\n…or add this under `projects:` in {reg_path} by hand, then validate + commit:\n")
    print(suggest_stanza(found[0]["cwd"], args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared-key detection artifact for the hosted board.

The local dashboard can show which projects reuse the SAME value for an env key, because it
reads the live ``.env`` files on the host. The hosted (Vercel) board cannot -- and must not:
a value or its fingerprint in a committed file would breach invariant 3 (no secrets in
receipts or the index). So this collector does the comparison **locally, in memory** (via the
one-way value hashes already computed by ``secrets.env_key_digests``) and writes a committed
artifact that records only *group membership* -- "these projects share a value for KEY" -- with
no value, no fingerprint, and no hash. That artifact is safe to commit and safe to render
online; the online board gains shared-key detection without any secret material leaving the host.

Regenerate with ``python -m collectors.keyscan`` (or ``foreman keyscan``) whenever ``.env``
files change, then commit ``data/keyshare.json``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from collectors import secrets

ARTIFACT = "data/keyshare.json"


def _scan(registry: dict, host: str) -> dict[str, dict[str, str]]:
    """{project: {declared_key: value_hash}} for worktrees present on this host."""
    out: dict[str, dict[str, str]] = {}
    for p in registry.get("projects", []):
        wt = (p.get("worktree") or {}).get(host) or next(iter((p.get("worktree") or {}).values()), None)
        if not wt:
            continue
        wtp = Path(os.path.expanduser(wt))
        if not wtp.is_dir():
            continue
        declared = set(p.get("env_keys") or [])
        dig = secrets.env_key_digests(wtp) or {}
        keys = {k: v["hash"] for k, v in dig.items() if k in declared}
        if keys:
            out[p["slug"]] = keys
    return out


def build_keyshare(registry: dict, host: str, *, digests_by_project=None,
                   taken: str | None = None) -> dict:
    """Group projects that share the same value for a declared key. The value hash is used only
    to group here and is never emitted -- the result carries key names and project lists only."""
    dbp = digests_by_project if digests_by_project is not None else _scan(registry, host)
    groups: dict[tuple, set] = {}
    for slug, keys in dbp.items():
        for k, h in keys.items():
            groups.setdefault((k, h), set()).add(slug)
    shared = [{"key": k, "projects": sorted(ps)} for (k, _h), ps in groups.items() if len(ps) >= 2]
    shared.sort(key=lambda g: (g["key"], g["projects"]))
    return {"host": host, "taken": taken, "groups": shared}


def write_keyshare(foreman_dir, data: dict) -> Path:
    path = Path(foreman_dir) / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


def load_lookup(foreman_dir) -> dict[tuple, list]:
    """{(project, key): [other projects sharing the value]} for rendering. Empty if absent."""
    path = Path(foreman_dir) / ARTIFACT
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    lookup: dict[tuple, list] = {}
    for g in data.get("groups", []):
        ps = g.get("projects") or []
        for p in ps:
            lookup[(p, g["key"])] = [o for o in ps if o != p]
    return lookup


def main(argv: list[str] | None = None) -> int:
    import datetime as dt
    import yaml
    ap = argparse.ArgumentParser(prog="keyscan",
                                 description="regenerate data/keyshare.json (shared-value groups)")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    args = ap.parse_args(argv)
    fm = Path(os.path.expanduser(args.foreman_dir))
    registry = yaml.safe_load((fm / "registry.yaml").read_text())
    taken = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = build_keyshare(registry, args.host, taken=taken)
    path = write_keyshare(fm, data)
    print(f"wrote {path}: {len(data['groups'])} shared-value group(s)")
    for g in data["groups"]:
        print(f"  {g['key']}: {', '.join(g['projects'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

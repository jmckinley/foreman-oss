"""Secrets store and env injection (SPEC.md section 19).

Foreman holds API-key *values* once in a sops-encrypted store, pushes them into each
project's gitignored ``.env``, and rotates in one place. This lives outside the receipt/index
boundary so invariant 3 (no secrets in receipts or the index) stays intact: values touch only
the sops-encrypted store (ciphertext) and the gitignored ``.env``; the ``credential`` table
records names and expiry only, never a value.

Crypto is delegated to the ``sops`` and ``age`` binaries. The backend is injectable so the
push/inventory/rotate logic is testable without them; the real ``SopsBackend`` shells out and
fails loudly if the tools or store are missing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_ISO = "%Y-%m-%dT%H:%M:%SZ"


# ----------------------------------------------------------------------- backends

class SopsBackend:
    """The real store: a sops-encrypted flat ``NAME: value`` YAML, values age-encrypted."""

    def __init__(self, store_path: str | Path):
        self.store = Path(store_path)

    def available(self) -> bool:
        return bool(shutil.which("sops")) and self.store.is_file()

    def _require(self) -> None:
        if not shutil.which("sops"):
            raise RuntimeError("sops binary not found; install sops + age (SPEC §19)")

    def get(self, name: str) -> str | None:
        self._require()
        p = subprocess.run(["sops", "-d", "--extract", f'["{name}"]', str(self.store)],
                           capture_output=True, text=True)
        return p.stdout.rstrip("\n") if p.returncode == 0 else None

    def set(self, name: str, value: str) -> None:
        self._require()
        # sops --set takes a JSON path + JSON value; it creates the file if the recipients
        # are configured in .sops.yaml.
        subprocess.run(["sops", "--set", f'["{name}"] {json.dumps(value)}', str(self.store)],
                       check=True)

    def list_names(self) -> list[str]:
        self._require()
        p = subprocess.run(["sops", "-d", str(self.store)], capture_output=True, text=True)
        if p.returncode != 0:
            return []
        data = yaml.safe_load(p.stdout) or {}
        return sorted(data.keys())


class DictBackend:
    """In-memory backend for tests. No crypto, no disk."""

    def __init__(self, data: dict | None = None):
        self._d = dict(data or {})

    def available(self) -> bool:
        return True

    def get(self, name: str) -> str | None:
        return self._d.get(name)

    def set(self, name: str, value: str) -> None:
        self._d[name] = value

    def list_names(self) -> list[str]:
        return sorted(self._d)


# ------------------------------------------------------------------- env writing

def _dotenv_line(name: str, value: str) -> str:
    # single-quote and escape so arbitrary values survive direnv's dotenv loader.
    return f"{name}='{value.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"


def write_env(env_path: Path, values: dict[str, str]) -> None:
    """Atomically write a 0600 .env with the given values (never anything else)."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# Written by Foreman (collectors/secrets.py). Do not commit; sourced via direnv.\n"
            + "".join(_dotenv_line(k, v) for k, v in values.items()))
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env.")
    try:
        os.write(fd, body.encode())
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------- operations

def _project(registry: dict, slug: str) -> dict | None:
    return next((p for p in registry.get("projects", []) if p["slug"] == slug), None)


import re as _re
_ENV_LINE_RE = _re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# Templates and backups are not the live config -- they carry stale/placeholder keys.
_IGNORE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".bak", ".backup",
                    ".old", ".save", ".orig", "~")


def _live_env_files(project_dir: Path) -> list[Path]:
    """A project's live .env files, newest-config-first: plain .env wins, backups/templates
    excluded so results reflect the running config, not stale copies."""
    files = []
    for f in list(project_dir.glob(".env")) + list(project_dir.glob(".env.*")):
        n = f.name.lower()
        if f.is_file() and not (n.endswith(_IGNORE_SUFFIXES) or "backup" in n or ".bak" in n):
            files.append(f)
    files.sort(key=lambda f: (f.name != ".env", f.name))
    return files


def env_files(project_dir) -> list[Path]:
    """A project's live .env files on THIS host (empty if the worktree is absent). Public
    wrapper of :func:`_live_env_files` for surfaces that offer to open a .env for editing."""
    project_dir = Path(os.path.expanduser(project_dir))
    return _live_env_files(project_dir) if project_dir.is_dir() else []


def is_live_env_file(path, worktrees) -> bool:
    """True iff ``path`` resolves to a live .env file under one of ``worktrees``. The whitelist
    that keeps an 'open this .env' action from being coerced into opening an arbitrary file
    (directory traversal) -- only paths the scanner itself would surface are allowed."""
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
    except OSError:
        return False
    for wt in worktrees:
        if not wt:
            continue
        for f in env_files(wt):
            try:
                if f.resolve() == target:
                    return True
            except OSError:
                continue
    return False


def _parse_env(text: str) -> dict[str, str]:
    """KEY=value per line. Quoted values keep their content; an unquoted value drops a
    trailing ` # comment` (dotenv semantics). First occurrence of a key wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
            val = raw[1:-1]
        else:
            val = _re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        out.setdefault(key, val)
    return out


def scan_env_files(project_dir) -> dict[str, list[str]]:
    """Key NAMES found in a project's live .env files -> the files they appear in.

    Names only; values are never read. Surfaces which keys a project actually uses (often far
    more than it declares) so nothing is silently unmanaged. Templates/backups are skipped.
    """
    project_dir = Path(os.path.expanduser(project_dir))
    out: dict[str, list[str]] = {}
    if not project_dir.is_dir():
        return out
    for f in _live_env_files(project_dir):
        try:
            for key in _parse_env(f.read_text(errors="replace")):
                out.setdefault(key, []).append(f.name)
        except OSError:
            continue
    return out


def unmanaged_env_keys(project_dir, declared: list[str]) -> list[str]:
    """Keys present in the project's live .env files that the registry does not declare."""
    declared_set = set(declared or [])
    return sorted(k for k in scan_env_files(project_dir) if k not in declared_set)


def env_key_digests(project_dir, chars: int = 5) -> dict[str, dict]:
    """Per key: {'fp': '…abcde' display fingerprint, 'hash': sha256 of the full value}.

    The last-``chars`` fingerprint is for humans to eyeball; the hash is for collision-proof
    match detection (two keys are the same iff their hashes match). Both computed on demand
    and NEVER stored in the index or a receipt (invariant 3). Backups/templates skipped;
    plain .env wins over .env.<x>.
    """
    project_dir = Path(os.path.expanduser(project_dir))
    out: dict[str, dict] = {}
    if not project_dir.is_dir():
        return out
    for f in _live_env_files(project_dir):
        try:
            values = _parse_env(f.read_text(errors="replace"))
        except OSError:
            continue
        for key, val in values.items():
            if val:
                out.setdefault(key, {"fp": "…" + val[-chars:],
                                     "hash": hashlib.sha256(val.encode()).hexdigest()})
    return out


def env_fingerprints(project_dir, chars: int = 5) -> dict[str, str]:
    """Just the display fingerprints (see env_key_digests)."""
    return {k: d["fp"] for k, d in env_key_digests(project_dir, chars).items()}


def scoped_name(name: str, *, project: str | None = None, host: str | None = None) -> str:
    """Store key for a scoped override: 'project:<slug>/NAME' or 'host:<host>/NAME'."""
    if project:
        return f"project:{project}/{name}"
    if host:
        return f"host:{host}/{name}"
    return name


def resolve_key(backend, name: str, project: str, host: str) -> tuple[str | None, str]:
    """One default value, overridable per scope. Most specific wins: project > host > default.

    Returns (value, source) where source is 'project', 'host', or 'default' (or 'missing').
    """
    for key, src in ((scoped_name(name, project=project), "project"),
                     (scoped_name(name, host=host), "host"),
                     (name, "default")):
        v = backend.get(key)
        if v is not None:
            return v, src
    return None, "missing"


def push(backend, registry: dict, project: str, host: str,
         worktree: str | None = None) -> dict:
    """Decrypt a project's declared env_keys (default value, per-scope override) into .env."""
    proj = _project(registry, project)
    if proj is None:
        raise ValueError(f"unknown project {project!r}")
    names = proj.get("env_keys") or []
    wt = worktree or (proj.get("worktree") or {}).get(host)
    if not wt:
        raise ValueError(f"no worktree for {project} on {host}")
    values, missing, overrides = {}, [], {}
    for name in names:
        v, src = resolve_key(backend, name, project, host)
        if v is None:
            missing.append(name)
        else:
            values[name] = v
            if src != "default":
                overrides[name] = src
    env_path = Path(os.path.expanduser(wt)) / ".env"
    write_env(env_path, values)
    return {"project": project, "host": host, "path": str(env_path),
            "written": sorted(values), "missing": missing, "overrides": overrides}


def push_all(backend, registry: dict, host: str) -> list[dict]:
    out = []
    for p in registry.get("projects", []):
        wt = (p.get("worktree") or {}).get(host)
        if p.get("env_keys") and wt and Path(os.path.expanduser(wt)).exists():
            out.append(push(backend, registry, p["slug"], host))
    return out


def inventory(conn, backend, registry: dict, *, expiry: dict | None = None,
              now: dt.datetime | None = None) -> int:
    """Record credential NAMES + expiry in the index. Never a value (§16.8)."""
    from collectors import db
    now = now or dt.datetime.now(dt.timezone.utc)
    expiry = expiry or {}
    # Union of every declared env_key across projects.
    names: set[str] = set()
    for p in registry.get("projects", []):
        names.update(p.get("env_keys") or [])
    stored = set(backend.list_names()) if backend.available() else set()
    for name in sorted(names):
        db.upsert(conn, "credential", {
            "name": name,
            "kind": "env_key",
            "scope": "shared" if name in stored else "declared-only",
            "expires": expiry.get(name),
            "last_checked": now.strftime(_ISO),
        }, keys=["name"])
    # Scoped overrides present in the store are inventoried too (never a value).
    overrides = [s for s in stored if s.startswith(("project:", "host:"))]
    for sk in overrides:
        db.upsert(conn, "credential", {
            "name": sk, "kind": "env_key_override",
            "scope": sk.split("/", 1)[0], "expires": expiry.get(sk),
            "last_checked": now.strftime(_ISO),
        }, keys=["name"])
    conn.commit()
    return len(names) + len(overrides)


def rotate(backend, registry: dict, name: str, value: str, host: str, *,
           scope_project: str | None = None, scope_host: str | None = None) -> dict:
    """Set a new value (default, or a project/host override) and push affected projects."""
    key = scoped_name(name, project=scope_project, host=scope_host)
    backend.set(key, value)
    if scope_project:
        targets = [scope_project]
    else:
        targets = [p["slug"] for p in registry.get("projects", [])
                   if name in (p.get("env_keys") or [])]
    results = []
    for slug in targets:
        proj = _project(registry, slug) or {}
        wt = (proj.get("worktree") or {}).get(host)
        if name in (proj.get("env_keys") or []) and wt and Path(os.path.expanduser(wt)).exists():
            results.append(push(backend, registry, slug, host))
    return {"rotated": key, "pushed_to": [r["project"] for r in results]}


def expiring_credentials(conn, *, now: dt.datetime | None = None, days: int = 14) -> list[dict]:
    """Credentials whose expiry is within ``days`` -- amber in the brief (§16.8)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now + dt.timedelta(days=days)).strftime(_ISO)
    rows = conn.execute(
        "SELECT name, expires FROM credential WHERE expires IS NOT NULL AND expires <= ? "
        "ORDER BY expires", (cutoff,)).fetchall()
    out = []
    for r in rows:
        exp = dt.datetime.strptime(r["expires"], _ISO).replace(tzinfo=dt.timezone.utc)
        out.append({"name": r["name"], "expires": r["expires"],
                    "days_left": (exp - now).days})
    return out


# ---------------------------------------------------------------------------- CLI

def _load_registry(foreman_dir: str) -> dict:
    return yaml.safe_load((Path(foreman_dir) / "registry.yaml").read_text())


def _store_path(foreman_dir: str) -> Path:
    return Path(foreman_dir) / "secrets" / "store.sops.yaml"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="secrets")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR") or str(ROOT))
    ap.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    pg = sub.add_parser("get"); pg.add_argument("name")
    ps = sub.add_parser("set"); ps.add_argument("name")   # value read from stdin, never argv
    ps.add_argument("--project"); ps.add_argument("--scope-host", dest="host_scope")
    pr = sub.add_parser("rotate"); pr.add_argument("name")
    pr.add_argument("--project"); pr.add_argument("--scope-host", dest="host_scope")
    pp = sub.add_parser("push"); pp.add_argument("project")
    sub.add_parser("push-all")
    args = ap.parse_args(argv)

    backend = SopsBackend(_store_path(args.foreman_dir))
    registry = _load_registry(args.foreman_dir)

    if args.cmd == "list":
        for n in backend.list_names():
            print(n)
        return 0
    if args.cmd == "get":
        v = backend.get(args.name)
        if v is None:
            print(f"no such key {args.name!r}", file=sys.stderr)
            return 1
        sys.stdout.write(v)
        return 0
    if args.cmd in ("set", "rotate"):
        value = sys.stdin.read().rstrip("\n")   # value via stdin so it never lands in argv/history
        if args.cmd == "set":
            key = scoped_name(args.name, project=args.project, host=args.host_scope)
            backend.set(key, value)
            print(f"set {key}")
        else:
            print(json.dumps(rotate(backend, registry, args.name, value, args.host,
                                    scope_project=args.project, scope_host=args.host_scope)))
        return 0
    if args.cmd == "push":
        print(json.dumps(push(backend, registry, args.project, args.host)))
        return 0
    if args.cmd == "push-all":
        print(json.dumps(push_all(backend, registry, args.host)))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

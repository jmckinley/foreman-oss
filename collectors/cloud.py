"""The cloud routine — fire ``tier: cloud`` cadences unattended from CI (GitHub Actions).

The local scheduler (``scheduler.tick``) deliberately skips cloud cadences; this module is the
other half. It is built for an ephemeral CI checkout, which drives three differences from the
launchd path:

- **No spool tick file.** "Due" is derived from committed receipts, not ``scheduler.state.json``:
  a cloud cadence fires when its most recent scheduled occurrence post-dates its newest receipt
  (or it has never run). The receipt is the contract (invariant 2), so this needs no external
  state and is naturally idempotent + catch-up-safe across CI runs.
- **Fresh clone per run.** SPEC §3: a cloud run has no local filesystem and clones the project
  repo fresh, so it never depends on a host worktree.
- **Index unreachable.** The lock lives on a Tailscale-reachable host the CI runner can't see, so
  cloud receipts are marked ``lock: unverified`` (SPEC §14) rather than blocking. Concurrency is
  bounded instead by the workflow's own ``concurrency:`` group (one routine at a time).

A run that clones or launches but writes no partial still yields a red receipt (red by absence),
so a broken cloud cadence surfaces rather than silently doing nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import yaml

from collectors import build_receipt, decisions
from collectors.brief import _cadence_meta
from collectors.scheduler import effective_schedule, effective_tier, previous_fire

_ISO = "%Y-%m-%dT%H:%M:%SZ"
CLOUD_HOST = "cloud"


def _parse_iso(s: str) -> dt.datetime | None:
    """Parse a receipt timestamp to a naive-UTC datetime, so it compares against the naive
    ``now`` the schedule math uses. Tolerates the trailing ``Z`` and fractional seconds."""
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def latest_receipt_ended(state_dir: Path, project: str, cadence: str) -> dt.datetime | None:
    """The ``ended`` of the newest committed receipt for one (project, cadence), or None if it
    has never produced one. Receipt filenames are lexically sortable by compact-ended, so the
    last name wins without opening every file."""
    d = Path(state_dir) / "receipts" / project / cadence
    if not d.is_dir():
        return None
    names = sorted(p.name for p in d.glob("*.json"))
    if not names:
        return None
    try:
        receipt = json.loads((d / names[-1]).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return _parse_iso(receipt.get("ended", ""))


def due_cloud(foreman_dir: Path, state_dir: Path, *, now: dt.datetime) -> list[dict]:
    """Cloud-tier cadences whose latest scheduled occurrence has not yet been captured by a
    receipt — i.e. the ones the cloud routine should fire this pass. Uses committed receipts as
    the clock, so two CI runs in the same hour don't double-fire the same occurrence."""
    foreman_dir, state_dir = Path(foreman_dir), Path(state_dir)
    registry = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    cadences = _cadence_meta(foreman_dir)
    out = []
    for p in registry.get("projects", []):
        slug, repo = p["slug"], p.get("repo")
        for cadence in p.get("cadences", []):
            cad = cadences.get(cadence)
            if not cad:
                continue
            if effective_tier(registry, slug, cadence, cad.get("tier")) != "cloud":
                continue
            allowed = cad.get("allowed_hosts")
            if allowed and CLOUD_HOST not in allowed:
                continue  # a cloud cadence pinned away from the cloud host (unusual, but honoured)
            sched = effective_schedule(registry, slug, cadence, cad["schedule"])
            try:
                occ = previous_fire(sched, now)
            except ValueError:
                continue
            if occ is None:
                continue
            last = latest_receipt_ended(state_dir, slug, cadence)
            if last is not None and last >= occ:
                continue  # this occurrence is already captured by a receipt
            out.append({"project": slug, "cadence": cadence, "repo": repo,
                        "scheduled_for": occ.strftime("%Y-%m-%dT%H:%M")})
    return out


# --------------------------------------------------------------------------- run one

def _clone_repo(repo: str, dest: Path, *, token: str | None) -> bool:
    """Shallow-clone ``owner/name`` into ``dest``. A token (PAT or the workflow token) is used
    for private repos; without one only public repos clone. A seam so tests can stub it."""
    if not repo:
        return False
    if token:
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
    else:
        url = f"https://github.com/{repo}.git"
    try:
        p = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _run_claude(prompt: str, *, cwd: str, env: dict, timeout_s: int, log: Path) -> int:
    """Run ``claude -p`` synchronously with a hard timeout, all output to ``log``. Returns the
    exit code (ignored — the receipt, not the code, is the source of truth). A seam for tests."""
    # perl alarm bounds the run (macOS/CI have no portable `timeout`); `;` not `&&` so a non-zero
    # claude still falls through — the missing partial then makes the receipt red, not absent.
    inner = (f"command -v claude || echo 'claude: NOT FOUND on PATH'; "
             f"perl -e 'alarm shift @ARGV; exec @ARGV' {timeout_s} "
             f"claude -p {shlex.quote(prompt)}; echo \"[claude rc=$?]\"")
    with open(log, "w") as fh:
        try:
            proc = subprocess.run(["bash", "-lc", inner], cwd=cwd, env=env,
                                  stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout_s + 30)
            return proc.returncode
        except subprocess.TimeoutExpired:
            return 124


def run_one(foreman_dir: Path, state_dir: Path, spool: Path, *, project: str, cadence: str,
            repo: str | None, now: dt.datetime, clone_token: str | None = None) -> dict:
    """Clone the project repo fresh, run the cadence headless, and write its receipt. Everything
    is env-driven so ``build_receipt.build`` assembles the same receipt shape as every other tier;
    only host/tier/lock differ (cloud / cloud / unverified)."""
    foreman_dir, state_dir, spool = Path(foreman_dir), Path(state_dir), Path(spool)
    spool.mkdir(parents=True, exist_ok=True)
    cad = yaml.safe_load((foreman_dir / "cadences" / f"{cadence}.yaml").read_text())
    run_id = decisions.ulid()
    started = now.astimezone(dt.timezone.utc).strftime(_ISO)
    timeout_s = int(cad.get("timeout_minutes", 40)) * 60

    env = {**os.environ,
           "FOREMAN_RUN_ID": run_id, "FOREMAN_CADENCE": cadence, "FOREMAN_PROJECT": project,
           "FOREMAN_TIER": "cloud", "FOREMAN_HOST": CLOUD_HOST, "FOREMAN_STARTED": started,
           "FOREMAN_LOCK": "unverified",       # index unreachable from CI (SPEC §14)
           "FOREMAN_SPOOL": str(spool), "FOREMAN_STATE_DIR": str(state_dir),
           "FOREMAN_DIR": str(foreman_dir),
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}

    with tempfile.TemporaryDirectory(prefix=f"cloud-{cadence}-") as tmp:
        clone_dir = Path(tmp) / "repo"
        cloned = _clone_repo(repo or "", clone_dir, token=clone_token)
        if cloned:
            prompt = (foreman_dir / cad["prompt_ref"]).read_text()
            log = spool / f"cloud-{run_id}.log"
            _run_claude(prompt, cwd=str(clone_dir), env=env, timeout_s=timeout_s, log=log)
        # Build the receipt regardless: a failed clone or an empty run has no partial and so is
        # recorded red (red by absence), never silently dropped.
        receipt_path = _build_receipt_with_env(env)

    return {"project": project, "cadence": cadence, "run_id": run_id,
            "cloned": cloned, "receipt": str(receipt_path) if receipt_path else None}


def _build_receipt_with_env(env: dict) -> Path | None:
    """Call ``build_receipt.build`` with ``env`` applied to ``os.environ`` (it reads env), then
    restore — so building one cadence's receipt can't leak env into the next in the same tick."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return build_receipt.build()
    except SystemExit:
        return None                              # missing env / unbuildable — surfaced by absence
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def tick(foreman_dir: Path, state_dir: Path, spool: Path, *, now: dt.datetime | None = None,
         clone_token: str | None = None) -> dict:
    """Fire every due cloud cadence once. Idempotent per occurrence via committed receipts."""
    now = now or dt.datetime.now()
    fired = []
    for d in due_cloud(Path(foreman_dir), Path(state_dir), now=now):
        try:
            fired.append(run_one(foreman_dir, state_dir, spool, project=d["project"],
                                  cadence=d["cadence"], repo=d.get("repo"), now=now,
                                  clone_token=clone_token))
        except Exception as exc:                 # one bad cadence must not sink the whole routine
            fired.append({"project": d["project"], "cadence": d["cadence"], "error": str(exc)})
    return {"now": now.isoformat(), "fired": fired}


# ---------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cloud", description="Foreman cloud routine (CI).")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    ap.add_argument("--state-dir", default=os.environ.get("FOREMAN_STATE_DIR", "state"))
    ap.add_argument("--spool", default=os.environ.get("FOREMAN_SPOOL", "spool"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="fire all due cloud cadences")
    sub.add_parser("due", help="list due cloud cadences without running them")
    args = ap.parse_args(argv)

    foreman_dir, state_dir, spool = Path(args.foreman_dir), Path(args.state_dir), Path(args.spool)
    token = os.environ.get("FOREMAN_CLONE_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if args.cmd == "due":
        print(json.dumps(due_cloud(foreman_dir, state_dir, now=dt.datetime.now()), indent=2))
        return 0
    if args.cmd == "tick":
        out = tick(foreman_dir, state_dir, spool, clone_token=token)
        print(json.dumps(out))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Assemble a run receipt from the envelope (env) and the prompt's partial (spool).

Invoked by ``hooks/session_end.sh`` / ``hooks/stop.sh``. Kept in Python, not bash, so
verdict expressions are checked safely and the result is validated against
``schema/receipt.schema.json`` before it is written.

Verdict expressions (e.g. ``undocumented_public_symbols == 0 and ...``) are interpreted
with a hand-written AST walker over a whitelist of comparison and boolean nodes. The
builtin ``eval`` is never used; arbitrary code cannot run.

Contract (SPEC.md section 7):

- The prompt writes ``$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json`` with ``metrics``,
  ``deltas`` and ``next_action``.
- This builder stamps identity and timing, derives ``status`` and ``verdict``, and writes
  ``state/receipts/<project>/<cadence>/<ended>-<run_id>.json``.
- A missing partial, or metrics whose keys do not exactly match the cadence's ``metrics``
  list, yields ``status: failed`` and ``verdict: red`` -- a run that ended without
  reporting, which is exactly the signal we want visible.

Idempotent: if a receipt for this ``run_id`` already exists in the target directory the
builder prints its path and exits 0, so whichever of the two hooks fires first wins.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent

# Kept in sync with the schema so truncation and the maxLength never drift apart.
_NEXT_ACTION_MAX = json.loads(
    (ROOT / "schema" / "receipt.schema.json").read_text())["properties"]["next_action"]["maxLength"]

_ISO = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------- safe verdict interpretation

_ALLOWED_CMP = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


def _interp(node: ast.AST, env: dict[str, object]) -> object:
    """Interpret a whitelisted boolean/comparison AST. No builtin evaluation."""
    if isinstance(node, ast.Expression):
        return _interp(node.body, env)
    if isinstance(node, ast.BoolOp):
        vals = [_interp(v, env) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(vals)
        return any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _interp(node.operand, env)
    if isinstance(node, ast.Compare):
        left = _interp(node.left, env)
        for op, right_node in zip(node.ops, node.comparators):
            right = _interp(right_node, env)
            if not isinstance(op, _ALLOWED_CMP):
                raise ValueError(f"operator {type(op).__name__} not allowed")
            if not _apply_cmp(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ValueError(f"unknown name {node.id!r} in verdict expression")
    if isinstance(node, ast.Constant):
        return node.value
    raise ValueError(f"disallowed expression node {type(node).__name__}")


def _apply_cmp(op: ast.cmpop, a, b) -> bool:
    if isinstance(op, ast.Lt):
        return a < b
    if isinstance(op, ast.LtE):
        return a <= b
    if isinstance(op, ast.Gt):
        return a > b
    if isinstance(op, ast.GtE):
        return a >= b
    if isinstance(op, ast.Eq):
        return a == b
    return a != b


def check_expr(expr: str, metrics: dict[str, object]) -> bool:
    tree = ast.parse(expr, mode="eval")  # ast parse mode, not the builtin
    return bool(_interp(tree, dict(metrics)))


def compute_verdict(cadence: dict, metrics: dict[str, object]) -> str:
    """green -> amber -> red, first match wins; red's 'otherwise' is the fallthrough."""
    rules = cadence["verdict"]
    for level in ("green", "amber"):
        expr = rules[level].strip()
        if expr == "otherwise":
            return level
        if check_expr(expr, metrics):
            return level
    return "red"


# ------------------------------------------------------- independent verifier pass
#
# A red is only as good as the check that produced it, and a flaky or transient check yields
# a false red that pages a human for nothing. When a cadence opts in (`verify: {enabled}`), a
# metric-driven red is re-checked by a second, cheaper probe before it is trusted: a red the
# verifier cannot reproduce is downgraded to amber with a note (BUILD.md M8.2). Only a
# status=ok red is eligible -- a failed/timeout/stopped red is a hard outcome, not a metric
# call to second-guess. The verifier is best-effort: any error keeps the red, so a broken
# verifier never silently clears a genuine problem.


def _parse_reproduced(text: str) -> bool:
    """Extract a boolean ``reproduced`` from verifier output. Tolerant of the ``claude -p``
    JSON envelope (the model's JSON sits in a ``result`` string). Defaults True (keep the
    red) on anything unparseable."""
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return True
    if isinstance(data, dict):
        if "reproduced" in data:
            return bool(data["reproduced"])
        if isinstance(data.get("result"), str):
            return _parse_reproduced(data["result"])
    return True


def default_verifier(receipt: dict, cadence: dict) -> bool:
    """A cheap second opinion on a red. Returns True if the red reproduces (trust it), False if
    it looks like a false alarm. ``FOREMAN_VERIFY_CMD`` (a shell command printing JSON
    ``{"reproduced": bool}``) is honoured when set -- the operator wires a cheap model or a
    re-check tool the same way ``FOREMAN_QUOTA_CMD`` is wired; otherwise a headless
    ``claude -p`` is asked to judge. Any failure returns True."""
    import subprocess
    model = (cadence.get("verify") or {}).get("model")
    metrics_json = json.dumps(receipt.get("metrics") or {})
    cmd = os.environ.get("FOREMAN_VERIFY_CMD")
    if cmd:
        env = {**os.environ, "FOREMAN_VERDICT": "red", "FOREMAN_METRICS": metrics_json,
               "FOREMAN_NEXT_ACTION": receipt.get("next_action") or ""}
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90, env=env)
        except (OSError, subprocess.SubprocessError):
            return True
        return _parse_reproduced(p.stdout) if p.returncode == 0 else True

    prompt = (f"A monitoring run for cadence {receipt['cadence']} on {receipt['project']} "
              f"produced verdict RED. Metrics: {metrics_json}. Recommended action: "
              f"{receipt.get('next_action')!r}. Is this a genuine problem or a false alarm "
              'from a flaky/transient check? Reply JSON only: {"reproduced": true|false}, '
              "where reproduced=true means the problem is genuine.")
    argv = ["claude", "-p", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return True
    return _parse_reproduced(p.stdout) if p.returncode == 0 else True


_DELTA_DIRS = {"better", "worse", "flat"}
_DELTA_KINDS = {"metric", "state", "config"}


def _clean_deltas(raw) -> list[dict]:
    """Keep only well-formed delta objects; drop anything else.

    `deltas` is informational and model-authored (the prompt computes it), so a malformed
    value -- a dict instead of an array, missing keys, a bad direction -- must never sink the
    whole receipt. Return the valid subset (possibly empty) rather than fail schema validation.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for d in raw:
        if (isinstance(d, dict) and d.get("kind") in _DELTA_KINDS
                and isinstance(d.get("name"), str) and d.get("direction") in _DELTA_DIRS):
            out.append({k: d[k] for k in ("kind", "name", "direction", "from", "to") if k in d})
    return out


def apply_verifier(receipt: dict, cadence: dict, verifier=None) -> dict:
    """Second-opinion a metric-driven red when the cadence enables it; downgrade an
    unreproduced red to amber with a note. Mutates and returns the receipt."""
    if receipt.get("status") != "ok" or receipt.get("verdict") != "red":
        return receipt
    vcfg = cadence.get("verify") or {}
    if not vcfg.get("enabled"):
        return receipt
    reproduced = bool((verifier or default_verifier)(receipt, cadence))
    receipt["verification"] = {"reproduced": reproduced, "verifier": vcfg.get("model") or "probe"}
    if not reproduced:
        receipt["verdict"] = "amber"
        note = "red not reproduced by the verifier; downgraded to amber"
        receipt["notes"] = f"{receipt['notes']}; {note}" if receipt.get("notes") else note
    return receipt


# ------------------------------------------------------------------------- helpers

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO)


def _compact(iso: str) -> str:
    """Filesystem-safe, still lexically sortable: 2026-09-06T08:14:12Z -> 20260906T081412Z."""
    return iso.replace("-", "").replace(":", "")


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"build_receipt: missing required env {name}", file=sys.stderr)
        raise SystemExit(2)
    return val


def _load_partial(spool: Path, run_id: str) -> dict | None:
    path = spool / f"{run_id}.partial.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------- build

def build() -> Path:
    run_id = _require("FOREMAN_RUN_ID")
    cadence_slug = _require("FOREMAN_CADENCE")
    project = _require("FOREMAN_PROJECT")
    host = _require("FOREMAN_HOST")
    tier = _require("FOREMAN_TIER")
    started = _require("FOREMAN_STARTED")
    spool = Path(os.path.expanduser(_require("FOREMAN_SPOOL")))
    state_dir = Path(os.path.expanduser(_require("FOREMAN_STATE_DIR")))
    foreman_dir = Path(os.path.expanduser(os.environ.get("FOREMAN_DIR", str(ROOT))))

    ended = os.environ.get("FOREMAN_ENDED") or _now_iso()

    existing = existing_receipt(state_dir, project, cadence_slug, run_id)
    if existing is not None:
        print(str(existing))
        return existing

    cadence = yaml.safe_load((foreman_dir / "cadences" / f"{cadence_slug}.yaml").read_text())
    expected_metrics = set(cadence["metrics"])

    partial = _load_partial(spool, run_id)

    status = "ok"
    fail_reason: str | None = None
    metrics: dict[str, object] = {}
    deltas = []
    next_action = ""

    if partial is None:
        status = "failed"
        fail_reason = "run ended without a partial; no metrics were reported"
    else:
        metrics = partial.get("metrics") or {}
        deltas = _clean_deltas(partial.get("deltas"))
        next_action = (partial.get("next_action") or "").strip()
        # Truncate to the schema's next_action maxLength (200): a completed run must not vanish
        # ("red by absence") just because its one-sentence next step ran a little long.
        if len(next_action) > _NEXT_ACTION_MAX:
            next_action = next_action[:_NEXT_ACTION_MAX - 1].rstrip() + "…"
        if set(metrics.keys()) != expected_metrics:
            status = "failed"
            missing = sorted(expected_metrics - set(metrics.keys()))
            extra = sorted(set(metrics.keys()) - expected_metrics)
            bits = []
            if missing:
                bits.append(f"missing {missing}")
            if extra:
                bits.append(f"unexpected {extra}")
            fail_reason = "metrics keys do not match cadence: " + ", ".join(bits)
        elif not next_action:
            status = "failed"
            fail_reason = "no next_action was written"

    if status == "failed":
        verdict = "red"
        if not next_action:
            next_action = f"Investigate why {cadence_slug} produced no verdict on {project}."
    else:
        verdict = compute_verdict(cadence, metrics)

    receipt: dict = {
        "schema_version": 1,
        "run_id": run_id,
        "cadence": cadence_slug,
        "project": project,
        "host": host,
        "tier": tier,
        "started": started,
        "ended": ended,
        "status": status,
        "verdict": verdict,
        "cc_version": os.environ.get("FOREMAN_CC_VERSION", "unknown"),
        "metrics": metrics,
        "next_action": next_action,
    }
    if model := os.environ.get("FOREMAN_MODEL"):
        receipt["model"] = model
    if lock := os.environ.get("FOREMAN_LOCK"):
        receipt["lock"] = lock
    if deltas:
        receipt["deltas"] = deltas
    if status == "failed":
        receipt["escalations"] = [
            {
                "severity": "red",
                "summary": f"{cadence_slug} run failed on {project}",
                "evidence": (fail_reason or "unknown failure")[:500],
            }
        ]
        if fail_reason:
            receipt["notes"] = fail_reason

    # Independent verifier pass: a metric-driven red the verifier can't reproduce becomes an
    # amber-with-note before the receipt is trusted (opt-in per cadence).
    apply_verifier(receipt, cadence)

    out = write_receipt(state_dir, receipt)
    print(str(out))
    return out


def existing_receipt(state_dir: Path, project: str, cadence: str, run_id: str) -> Path | None:
    """One receipt per run_id: return it if already written, so emission is idempotent."""
    target_dir = state_dir / "receipts" / project / cadence
    for path in target_dir.glob(f"*-{run_id}.json"):
        return path
    return None


def write_receipt(state_dir: Path, receipt: dict) -> Path:
    """Validate against the contract and write to the canonical receipt path.

    Shared by the hook path (build) and the lock path (a status=locked receipt). Idempotent
    per run_id.
    """
    project, cadence, run_id = receipt["project"], receipt["cadence"], receipt["run_id"]
    existing = existing_receipt(state_dir, project, cadence, run_id)
    if existing is not None:
        return existing
    _validate(receipt)
    target_dir = state_dir / "receipts" / project / cadence
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{_compact(receipt['ended'])}-{run_id}.json"
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return out


def _validate(receipt: dict) -> None:
    schema = json.loads((ROOT / "schema" / "receipt.schema.json").read_text())
    errors = sorted(
        Draft202012Validator(schema).iter_errors(receipt),
        key=lambda e: list(e.path),
    )
    if errors:
        print("build_receipt: assembled receipt fails schema:", file=sys.stderr)
        for e in errors:
            loc = "/".join(str(p) for p in e.path) or "<root>"
            print(f"  - {loc}: {e.message}", file=sys.stderr)
        raise SystemExit(3)


if __name__ == "__main__":
    build()

---
name: brief
description: Render the Foreman consolidated brief — the one-screen board of every project's cadence verdicts, what needs a decision, the operator decision queue, and quiet green work. Use when the operator asks for the brief, the board, project status across projects, or "what needs me". Reads receipts straight from git; no index required.
---

# /brief

Render the Foreman board (SPEC.md section 12) straight from the receipts on the `state`
branch. This is a pure read: it writes nothing.

## What it shows

- **NEEDS YOU** — the escalation queue: red verdicts, ambers that have aged to red, and
  cadences gone stale (no receipt within twice their schedule interval). Sorted by severity
  then age, each line carries the `next_action` verbatim from its receipt.
- **CADENCES** — the project × cadence grid, each cell a verdict and age. `-` means the
  cadence does not apply (or is not defined yet); `stale` means a receipt is overdue, which
  is how a dead scheduler becomes visible.
- **DRIFT** — pin drift, shadowing surprises, and budget breach, from the config resolver
  (needs the index; populated once collectors have run).
- **QUEUED** — operator decisions waiting to be applied (SPEC.md section 18).
- **QUIET** — green work that needs no attention.

## How to run it

The renderer is deterministic and lives in `collectors/brief.py`. Do not eyeball receipts by
hand — run the module and present its output verbatim.

1. Resolve the two directories:
   - `FOREMAN_DIR` — this Foreman repo (holds `registry.yaml` and `cadences/`). Defaults to
     the repo root if unset.
   - `FOREMAN_STATE_DIR` — the checkout of the `state` branch (holds `receipts/` and
     `queue/`). Typically a worktree at `<repo>/state`: create it once with
     `git worktree add state state` if it is missing.
2. Run:

   ```bash
   python -m collectors.brief --state-dir "$FOREMAN_STATE_DIR" --foreman-dir "$FOREMAN_DIR" \
     --index "$FOREMAN_INDEX"
   ```

   (All three also read from the `FOREMAN_STATE_DIR` / `FOREMAN_DIR` / `FOREMAN_INDEX`
   environment variables. `--index` is optional; without it the DRIFT section is omitted.)
3. Present the output exactly as printed — it is already formatted for one screen. Do not
   summarize or re-order it; the ordering is the point.

## Notes

- If `state/` has no receipts yet, the grid renders all cadences as `-`; that is correct,
  not an error.
- RUNNING is intentionally absent until run-state tracking lands. DRIFT requires the index
  (`--index`); without it the section says so rather than faking data.

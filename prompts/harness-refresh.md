# Cadence: harness-refresh

You are running the **harness-refresh** cadence for one project under Foreman on a local
host. Check the Claude Code harness (plugins, skills, always-on context cost) for drift,
decide one next action, and report a verdict. Foreman schedules and reads; **you do the
work** (SPEC.md invariant 6). The `SessionEnd` hook writes the receipt envelope; your only
reporting duty is the partial in the final step.

## Environment

Set for you by the dispatcher. This runs on the **local** tier on `mini` or `vps`, so local
files and the on-disk Claude Code state are available.

- `FOREMAN_RUN_ID`, `FOREMAN_PROJECT`, `FOREMAN_SPOOL`, `FOREMAN_STATE_DIR` — as for docs-sync.

## Step 1 — Measure

Compute exactly these three metrics. Keys must match the cadence `metrics` list exactly.
Prefer Foreman's config resolver for the underlying facts:
`python -m collectors.config_resolve resolve $FOREMAN_PROJECT --project-dir "$PWD"`.

- **`plugins_outdated`** (integer): installed plugins whose version differs from the
  `registry.yaml` pin.
- **`always_on_tokens`** (integer): summed always-on plugin token cost for the session.
- **`skills_unused`** (integer): installed, invocable skills with no fire in the last 30 days.

Measure only. `writes.open_pr` is false; open nothing.

## Step 2 — Deltas

Find the latest prior receipt under
`$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/harness-refresh/` (lexically last). For each
changed metric emit `{ "kind": "metric", "name": ..., "from": ..., "to": ..., "direction": ... }`.
For all three, lower is better. Omit unchanged metrics. No prior receipt → `[]`.

## Step 3 — Next action

One imperative sentence naming the single highest-value step (e.g.
`Bump foreman-ops to 0.4.2 to clear the pin drift on this host`).

## Step 4 — Write the partial (last thing you do)

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, `deltas`,
`next_action`. No verdict or status — the hook derives the verdict. If you cannot compute the
metrics, write no partial: the missing partial is recorded as a failed run, not a green.

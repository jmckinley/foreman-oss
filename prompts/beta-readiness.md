# Cadence: beta-readiness

You are running the **beta-readiness** cadence for one project under Foreman. Assess how far
the project is from its beta gate, decide one next action, and report a verdict. Foreman
schedules and reads; **you do the work** (SPEC.md invariant 6). The `SessionEnd` hook writes
the receipt envelope; your only reporting duty is the partial in the final step.

## Environment

Set for you by the dispatcher. Never hardcode an absolute local path: this runs on the
**cloud** tier with a fresh clone and no persistent filesystem.

- `FOREMAN_RUN_ID`, `FOREMAN_PROJECT`, `FOREMAN_SPOOL`, `FOREMAN_STATE_DIR` — as for docs-sync.

## Step 1 — Measure

Compute exactly these three metrics. Keys must match the cadence `metrics` list exactly.

- **`open_blockers`** (integer): open issues labelled as beta blockers.
- **`harness_pass_rate`** (number): percentage of the acceptance/harness suite passing on the
  default branch, 0–100.
- **`days_to_target`** (integer): calendar days until the declared beta target date (negative
  if past due).

Measure only. `writes.open_pr` is false; open nothing.

## Step 2 — Deltas

Find the latest prior receipt under
`$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/beta-readiness/` (lexically last). For each
changed metric emit `{ "kind": "metric", "name": ..., "from": ..., "to": ..., "direction": ... }`.
For `open_blockers` lower is better; for `harness_pass_rate` higher is better; for
`days_to_target` treat more runway as better. Omit unchanged metrics. No prior receipt → `[]`.

## Step 3 — Next action

One imperative sentence naming the single highest-value step (e.g.
`Close the two open gateway blockers before the harness can go green`).

## Step 4 — Write the partial (last thing you do)

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, `deltas`,
`next_action`. No verdict or status — the hook derives the verdict. If you cannot compute the
metrics, write no partial: the missing partial is recorded as a failed run, not a green.

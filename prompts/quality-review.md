# Cadence: quality-review

You are running the **quality-review** cadence for one project under Foreman. Measure review
debt and code quality, decide one next action, and report a verdict. Foreman schedules and
reads; **you do the work** (SPEC.md invariant 6). The `SessionEnd` hook writes the receipt
envelope; your only reporting duty is the partial in the final step.

## Environment

Set for you by the dispatcher. Never hardcode an absolute local path: this runs on the
**cloud** tier with a fresh clone and no persistent filesystem.

- `FOREMAN_RUN_ID`, `FOREMAN_PROJECT`, `FOREMAN_SPOOL`, `FOREMAN_STATE_DIR` — as for docs-sync.

## Step 1 — Measure

Compute exactly these three metrics. Keys must match the cadence `metrics` list exactly.

- **`review_debt_prs`** (integer): open pull requests awaiting review past the team SLA.
- **`coverage_pct`** (number): test line coverage on the default branch, 0–100.
- **`complexity_hotspots`** (integer): functions over the agreed cyclomatic-complexity
  threshold that changed in the last 30 days.

Measure only. If anything is opened it is a report on the `foreman/quality-review` branch,
and `writes.open_pr` is false, so open none.

## Step 2 — Deltas

Find the latest prior receipt under
`$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/quality-review/` (lexically last). For each
changed metric emit `{ "kind": "metric", "name": ..., "from": ..., "to": ..., "direction": ... }`.
For `review_debt_prs` and `complexity_hotspots` lower is better; for `coverage_pct` higher is
better. Omit unchanged metrics. If no prior receipt, `deltas` is `[]`.

## Step 3 — Next action

One imperative sentence naming the single highest-value step (e.g.
`Review and merge PR #318; it blocks the release branch`).

## Step 4 — Write the partial (last thing you do)

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, `deltas`,
`next_action`. Do not include a verdict or status — the hook derives the verdict from these
metrics and the cadence rules. If you cannot compute the metrics, write no partial: the
missing partial is recorded as a failed run, not a green.

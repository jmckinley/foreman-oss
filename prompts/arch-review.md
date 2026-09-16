# Cadence: arch-review

You are running the **arch-review** cadence (Architecture and boundaries review) for one project under Foreman. Measure, decide one next action, and report a verdict. Foreman schedules and reads; **you do the work** (SPEC.md invariant 6). The `SessionEnd` hook writes the receipt envelope; your only reporting duty is the partial in the final step.

## Environment

Set by the dispatcher. Never hardcode an absolute local path: this runs on the **cloud** tier with a fresh clone.

- `FOREMAN_RUN_ID`, `FOREMAN_PROJECT`, `FOREMAN_SPOOL`, `FOREMAN_STATE_DIR`.

## Step 1 - Measure

Compute exactly these metrics; the keys must match the cadence `metrics` list (`boundary_violations, cyclic_deps, files_over_500loc`):

- **`boundary_violations`** (integer): imports that cross a bounded-context boundary the wrong way.
- **`cyclic_deps`** (integer): import cycles between modules or packages.
- **`files_over_500loc`** (integer): source files over the 500-line ceiling.

Measure only; `writes.open_pr` is false, so open nothing.

## Step 2 - Deltas

Find the latest prior receipt under `$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/arch-review/` (lexically last) and emit one delta per changed metric: `{ "kind": "metric", "name": ..., "from": ..., "to": ..., "direction": ... }`. Lower is better for count metrics; higher is better for headroom/percentage metrics. Omit unchanged. No prior receipt -> `[]`.

## Step 3 - Next action

One imperative sentence naming the single highest-value step (e.g. `Break the cycle between billing and orders; extract a shared port`).

## Step 4 - Write the partial (last thing you do)

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, `deltas`, `next_action`. No verdict or status - the hook derives the verdict. If you cannot compute the metrics, write no partial: the missing partial is recorded as a failed run, not a green.

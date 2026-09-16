# Cadence: docs-sync

You are running the **docs-sync** cadence for a single project under Foreman. Your job is
to measure documentation drift, decide one concrete next action, and report a verdict.
Foreman schedules and reads; **you do the work** (SPEC.md invariant 6).

The receipt envelope (identity, timing, status, verdict) is written by the `SessionEnd`
hook. Your only reporting duty is to write the partial described in the final step. Do not
write the receipt yourself.

## Environment

These variables are set for you by the dispatcher. Never hardcode an absolute local path:
this cadence runs on the **cloud** tier with a fresh clone and no persistent filesystem.

- `FOREMAN_RUN_ID`   — ULID for this run; names your partial file.
- `FOREMAN_PROJECT`  — project slug (e.g. `acmeapi`).
- `FOREMAN_SPOOL`    — directory to write the partial into.
- `FOREMAN_STATE_DIR`— checkout of the receipts (`state`) branch, read-only, for deltas.

## Step 1 — Measure

Work only from the checked-out project tree and its git history. Compute exactly these
three metrics. The keys must match the cadence `metrics` list exactly; a missing or extra
key makes the run fail.

- **`undocumented_public_symbols`** (integer): count exported/public symbols (functions,
  classes, types, CLI commands) that have no doc entry in the user guide or docstring.
- **`guide_sections_stale_days_max`** (integer): for each user-guide section, the age in
  days between the newest commit touching the code it documents and the newest commit
  touching that section; report the largest such age across all sections. `0` if every
  section is at least as fresh as the code it covers.
- **`changelog_commit_delta`** (integer): number of commits on the default branch since the
  last changelog entry that are user-visible and unmentioned.

Measure; do not fix code. If this cadence opens anything, it is a docs PR on the
`foreman/docs-sync` branch, and only when `writes.open_pr` and the file budget allow.

## Step 2 — Compute deltas against the previous receipt

Find the most recent prior receipt for **this** project and cadence under
`$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/docs-sync/`. Receipts are named so lexical
order is chronological; take the last one. If none exists, `deltas` is an empty list.

For each of the three metrics whose value changed, emit one delta object:

```json
{ "kind": "metric", "name": "<metric>", "from": <prev>, "to": <now>, "direction": "better|worse|flat" }
```

`direction` is from the reader's point of view: for all three metrics **lower is better**,
so a decrease is `better`, an increase is `worse`, and an unchanged value is `flat` (omit
flat metrics). The brief renders these verbatim; you compute them, the brief does not.

## Step 3 — Decide the next action

Write one imperative sentence naming the single highest-value next step (e.g.
`Rewrite docs/deploy.md sections 3-5`). This is what appears in the brief. A run that
cannot name one is not green.

## Step 4 — Write the partial (must be the last thing you do)

Write JSON to `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly these three keys:

```json
{
  "metrics": {
    "undocumented_public_symbols": 0,
    "guide_sections_stale_days_max": 3,
    "changelog_commit_delta": 2
  },
  "deltas": [
    { "kind": "metric", "name": "guide_sections_stale_days_max", "from": 7, "to": 3, "direction": "better" }
  ],
  "next_action": "Document the three new public symbols in docs/api.md"
}
```

Do not include a verdict, status, or timing — the hook derives the verdict from these
metrics and the cadence rules, and stamps the envelope. If you cannot compute the metrics,
write no partial: the missing partial is itself the signal, and the hook records the run as
failed rather than inventing a green.

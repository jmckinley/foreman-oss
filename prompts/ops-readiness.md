# ops-readiness

Track Foreman's own deferred finish-line items and report what's still outstanding. This is a
**report-only** loop — do **not** SSH to other hosts, push to remotes, install packages, or run
other loops. Just check status from what's observable on this host, count the gaps, and name the
next action. (SPEC invariant 6: Foreman schedules and reads; the operator does the host-bound work.)

## Environment

Runs on the **local** tier on `mbp`. `FOREMAN_DIR` is the repo. `FOREMAN_RUN_ID`, `FOREMAN_SPOOL`,
`FOREMAN_STATE_DIR` are set by the dispatcher. Never hardcode an absolute path beyond these.

## Step 1 — Measure

Compute exactly these four metrics (keys must match the cadence `metrics` list):

- **`deploy_ops_gaps`** (integer, 0–5): how many of these host-bound deploy/ops items show **no
  evidence of completion** on this host. Count one for each that is NOT yet done:
  1. **Telemetry receiver running on `mini`** — check `config/telemetry.env` exists and is
     committed, and look for any marker/receipt that the OTLP receiver is installed on `mini`
     (`telemetry install` output, a launchd/systemd unit reference). No evidence → a gap.
  2. **`config/telemetry.env` pushed to hosts** — file present and referenced by the deploy path.
  3. **Cloud routine wired** (M1 task 5) — a scheduled entry that fires the cloud-tier cadences.
     Shipped as a GitHub Actions workflow: check `.github/workflows/cloud-routine.yml` exists and
     that `collectors/cloud.py` is present (its `due_cloud`/`tick`). Present → **done, not a gap**.
     (Activating it still needs the `ANTHROPIC_API_KEY` repo secret, but the routine itself is
     wired.) Absent → a gap.
  4. **`sops` + `age` provisioned** — the `sops` and `age` binaries are on PATH and `secrets/`
     holds at least one `*.sops.yaml` with a usable age key. Missing → a gap.
  5. **Real `FOREMAN_QUOTA_CMD`** — set to an actual command, not unset/placeholder.
- **`pii_masking_gap`** (integer, 0 or 1): `1` if `python -c "import presidio_analyzer"` fails
  (prompt-history PII masking is secrets-only until Presidio is installed), else `0`.
- **`loops_unrun`** (integer): how many loops enabled for `foreman` have **never produced a
  receipt** — no directory/files under `$FOREMAN_STATE_DIR/receipts/foreman/<loop>/`. `intuitive-ux`
  is the known one; count any others too.
- **`approval_gate_unverified`** (integer, 0 or 1): `1` if the approval-gate push path has **never
  been exercised against a live remote** — i.e. no receipt shows an `approve_push` decision that
  ran `git push` + `gh pr create` for real (the mechanism is built and unit-tested with the remote
  stubbed, but not yet run end-to-end). `0` once there is evidence it ran.

Measure only. Report status; change nothing.

## Step 2 — Deltas

Find the latest prior receipt under `$FOREMAN_STATE_DIR/receipts/foreman/ops-readiness/` (lexically
last) and emit one delta per changed metric:
`{ "kind": "metric", "name": ..., "from": ..., "to": ..., "direction": "better|worse|flat" }`.
Lower is better for every metric here. Omit unchanged. No prior receipt → `[]`.

## Step 3 — Next action

One imperative sentence naming the single highest-leverage step to close the most impactful open
gap (e.g. `pip install presidio-analyzer to close the PII-masking gap` or
`run intuitive-ux to clear the last unrun loop` or `install the OTLP receiver on mini`).

## Step 4 — Write the partial (last thing you do)

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, `deltas`, `next_action`.
No verdict or status — the hook derives the verdict. If you cannot compute the metrics, write no
partial: the missing partial is recorded as a failed run, not a green.

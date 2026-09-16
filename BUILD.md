# Build order

Each milestone is independently useful. Do not start the next one until the acceptance
criteria for the current one pass. M1 and M2 are the whole idea in working form; if they are
not useful on their own, the rest will not rescue them.

---

## M1 — The contract, end to end, one cadence, one project

**Ships:** `registry.yaml`, all three schemas, `cadences/docs-sync.yaml`,
`prompts/docs-sync.md`, the hook pair, and a cloud routine that writes receipts to the
`state` branch.

Tasks:

1. Write `collectors/validate.py`: loads `registry.yaml` and every `cadences/*.yaml`,
   validates against the schemas, and enforces the tier constraints in SPEC.md section 6
   (cloud rejects `allowed_hosts` and absolute local paths; session rejects
   `escalate_when`).
2. Write `prompts/docs-sync.md`. It must end by writing
   `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` containing `metrics`, `deltas` and
   `next_action`, computing `deltas` against the previous receipt for the same cadence and
   project.
3. Write `hooks/session_end.sh` and `hooks/stop.sh`. The hook writes the envelope and merges
   the partial. A missing partial yields `status: failed`.
4. Create the `state` branch. Commit path is
   `state/receipts/<project>/<cadence>/<ended-iso>-<run_id>.json`.
5. Create the cloud routine for `docs-sync` on `acmeapi` only.

**Acceptance:**

- `python -m collectors.validate` exits 0.
- A manual `/dispatch acmeapi docs-sync` produces a receipt on the `state` branch that
  validates against `schema/receipt.schema.json`.
- Killing the run mid-flight produces a receipt with `status: failed`, not silence.

---

## M2 — `/brief` reading receipts straight from git

**Ships:** the `foreman-ops` plugin with the `/brief` skill, no index.

Tasks:

1. Scaffold `.claude-plugin/marketplace.json` and `foreman-ops/.claude-plugin/plugin.json`.
2. Write `skills/brief/SKILL.md`. It reads `state/receipts/`, groups by project and cadence,
   takes the latest receipt per pair, and renders the board in SPEC.md section 12.
3. Implement staleness: a cadence with no receipt within twice its schedule interval renders
   as stale and enters NEEDS YOU.
4. Implement amber aging from the first amber in an unbroken amber run.

**Acceptance:**

- `/brief` renders correctly with three projects and at least one stale cadence.
- Deleting the most recent receipt makes that cadence show stale rather than disappear.

---

## M3 — Index, repository collectors, locking

**Ships:** `sql/schema.sql` applied on `mini`, collectors C4 and C5, the lock table.

Tasks:

1. Apply the DDL. Write `collectors/db.py` with connect, migrate and upsert helpers.
2. Write `collectors/git_state.py`: branch, ahead/behind, dirty files, worktrees and
   orphans, stale branches, unmerged agent branches, largest blob, force pushes in 7 days.
   Runs per host per worktree.
3. Write `collectors/github_state.py`: open PRs, agent-authored PRs, oldest PR age, failing
   checks grouped by failure class, security and dependabot alerts, Actions minutes, open
   issues.
4. Implement the lock acquire in SPEC.md section 14, including the `status: locked` receipt
   path and the `"lock": "unverified"` cloud fallback.
5. Add receipt ingestion into `run`, `metric` and `escalation`.

**Acceptance:**

- Two simultaneous `/dispatch` calls for the same project and cadence produce one run and
  one `status: locked` receipt.
- A crashed run's lock is stealable after `timeout_minutes + 5`.
- Dropping `index.db` and re-running all collectors reproduces the same rows.

---

## M4 — Config resolver and the DRIFT section

**Ships:** `collectors/config_resolve.py`, `/explain-config`, drift in the brief.

Tasks:

1. Implement the layer merge in SPEC.md section 10, including list-key concatenation and the
   per-pair environment overlay for `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_MODEL`.
2. Walk every activation source in the section 10 table. Compute the AND across the four MCP
   gates. Record `invocable` per skill, not just installed.
3. Walk memory imports transitively and record depth.
4. Implement the `claude -p --output-format json` probe and reconcile against the
   prediction. Disagreement writes `winning_layer = 'probe'` and raises amber.
5. Sum always-on plugin token cost from `claude plugin details` into
   `component.always_on_tokens` and compare against `context_budget_tokens`.
6. Add the DRIFT section to `/brief`: pin drift, shadowing surprises, budget breach.

**Acceptance:**

- `/explain-config permissions.deny paysvc` names the winning layer and every shadowed
  layer.
- Deliberately shadowing a key produces a drift line in the next brief.
- A skill with `disable-model-invocation: true` is recorded as installed and not invocable.

---

## M5 — Telemetry and transcripts

**Ships:** C2 and C1, cost attribution, unused-skill pruning.

Tasks:

1. Stand up an OTLP receiver on `mini`, roll events into `telemetry_day`.
2. Push the telemetry env block to all hosts. Remember it is read only at session start.
3. Write `collectors/transcripts.py` with byte-offset tailing, the 25 MB cap, the
   `parser_version` pin, and hard stop on envelope shape change.
4. Extract decisions and skill fires only. No numeric extraction from transcripts.
5. Add cost per project per cadence and the unused-skill list to the QUIET section.

**Acceptance:**

- Edit accept/reject rate per project appears in the index and is non-zero.
- A synthetic 200 MB transcript is skipped and marked oversized, with no memory spike.
- A synthetic unknown envelope type halts C1 and raises red without writing rows.

---

## M7 — Async operator decisions (forward channel)

**Ships:** `schema/decision.schema.json`, `collectors/decisions.py`, `hooks/session_start.sh`,
and the `queue/` path on the `state` branch. Implements SPEC.md section 18. Depends on M1
(receipts, state branch, hook pattern); integrates with M2's brief and M6's `/dispatch` and
`/close` as they land.

Tasks:

1. Write `schema/decision.schema.json`: the durable decision contract, ULID identity,
   `pending → applied|failed|expired|superseded`, per-kind payloads.
2. Write `collectors/decisions.py`: `enqueue`, `load_pending`, project resolution from cwd,
   app-side apply (`apply_setting` edits settings; `dispatch_cadence`/`answer_question`/
   `note` surface as context), supervisor-side apply (`set_pin` edits `registry.yaml` with
   comments preserved; `close_escalation`/`pause_cadence`/`resume_cadence` record), `board`,
   and `gc`.
3. Write `hooks/session_start.sh`: resolve the project from the session cwd, drain pending
   app-side decisions in ULID order, emit the SessionStart `additionalContext`. Never abort
   session startup.
4. Add the QUEUED section to `/brief` and a Decisions row to retention (SPEC.md sections 12
   and 15).

**Acceptance:**

- A decision enqueued for a project with no running session is applied, in order, at the
  next SessionStart, and re-running the drain applies nothing (idempotent).
- `set_pin` updates `registry.yaml` and the file still passes `collectors.validate` with its
  comments intact.
- A pending decision past its `expires` renders as expired in the next brief rather than
  disappearing, and an app-side decision that raises is marked `failed` without blocking the
  session.

---

## M6 — Steady state

**Ships:** remaining cadences across all three projects, `/promote`, `/dispatch`, `/close`,
escalation aging, retention jobs.

Tasks:

1. Write `quality-review`, `beta-readiness` and `harness-refresh` cadences.
2. Implement `/promote`: move a skill into the plugin, bump version, update pins in
   `registry.yaml`, then verify installation per project via the C6 probe rather than
   assuming propagation.
3. Implement GitHub issue open, comment and close per SPEC.md section 13.
4. Implement the retention table in section 15 as a weekly local cadence.
5. Implement the quota guard: below 15% headroom, defer non-red cadences and say so.

**Acceptance:**

- All three projects run all applicable cadences for one full week with no manual
  intervention.
- Every red in that week has a GitHub issue, and every issue closed by a green run links to
  the clearing receipt.

---

## M8 — Competitive parity & differentiation (post-1.0)

Drawn from the 2026 competitive scan (`docs/COMPETITIVE.md`). Adopt the strongest
table-stakes ideas the field has converged on, and widen the lead on the one thing no
competitor has (effective-config / drift). Each item is independent; pick by leverage.

### Adopt (table stakes seen in MartinLoop, 5dive, Bernstein, agent-fleet-o)

1. **Run budgets + stop conditions** (MartinLoop). Per-cadence token/cost budget, iteration
   cap, and wall-clock stop in the cadence schema; the run aborts and writes a `status:
   stopped` receipt when hit, distinct from `timeout`. Complements the existing quota guard
   (which is global headroom) with per-run limits.
2. **Independent verifier pass** (MartinLoop). Optionally re-check a run's own verdict with a
   second, cheaper probe before it is trusted — a red that a verifier can't reproduce is
   downgraded to amber with a note, cutting false reds.
3. **One-command install** (5dive, AgentsRoom). `pipx install foreman` + `foreman init`
   scaffolds the instance dir, the `state` worktree, the launchd/systemd agent, and a first
   project — replacing today's manual `foreman-test` setup.
4. **Worktree-isolated launch + approval gate** (Bernstein, agent-fleet-o). When the scheduler
   runs a loop in `launch` mode, run it in a throwaway git worktree; loops that open a PR pause
   for a human approval in the brief/dashboard before pushing. Keeps unattended runs safe.
5. **Backfill / catch-up scheduling.** After downtime, the scheduler should fire the most
   recent missed occurrence per cadence (not every missed one), so a slept laptop doesn't skip
   a week silently — pairs with the existing receipt-absence staleness.

### Widen the lead (Foreman's moat — nobody else has this)

6. **Deeper effective-config / drift.** Finish walking every activation source in SPEC §10
   (four MCP gates, transitive memory imports, plugin always-on budget), reconcile against the
   `claude -p` probe on every host, and make DRIFT the headline of the brief — the
   "supervisor you can trust and reconstruct" story is the wedge.
7. **Fleet config diff.** "What is activated on mbp but not vps?" — a cross-host diff of the
   resolved config, surfacing accidental per-host divergence.

### Remaining from earlier milestones (still open)

8. **C1/C2 deploy** — stand up the OTLP receiver on `mini`; push `config/telemetry.env`.
9. **Cloud routine** — wire the `docs-sync` cloud routine (M1 task 5) once the remote + scheduler exist.
10. **Tests already live** under `tests/` (pytest); keep them the pre-commit gate.

**Acceptance (per item, when built):** the feature has a test in `tests/`, `collectors.validate`
still exits 0, and — for anything that changes a receipt shape or the registry schema — the
schemas and SPEC are updated in the same change.

# Foreman v0.1 — Cross-Project Supervisor Specification

Foreman decides what should run across multiple Claude Code projects, launches it on the
right tier, collects a verdict from every run, and holds one catalog of skills plus one
resolved picture of what is actually activated where.

Foreman is a control repo, a contract, and a small index. It is not a daemon and not an
agent runtime.

Machine-readable companions to this document:

| File | Holds |
| --- | --- |
| `registry.yaml` | Projects, hosts, pins |
| `cadences/*.yaml` | One recurring loop per file |
| `schema/receipt.schema.json` | The receipt contract |
| `schema/registry.schema.json` | Registry validation |
| `schema/cadence.schema.json` | Cadence validation |
| `schema/decision.schema.json` | Operator decision contract (§18) |
| `sql/schema.sql` | Index DDL |
| `BUILD.md` | Milestones with acceptance criteria |

---

## 1. Decision record

The state store was not specified, so this spec picks one. Change this section and the
rest follows.

### Decided: hybrid, three stores with different jobs

| Store | Holds | Lives | Why |
| --- | --- | --- | --- |
| Receipts | One JSON verdict per run | Git, `state` branch of this repo | Auditable, diffable, survives every machine, small enough that write contention is rare at one run per hour |
| Index | Parsed transcripts, config snapshots, git and GitHub state, telemetry rollups | SQLite at `~/foreman/index.db` on the always-on mini PC, reachable over Tailscale | Too large and too churny for git, needs indexed queries, rebuildable from source |
| Event stream | Hook emissions and OTLP metrics and events | Append-only JSONL spool, drained into the index | Decouples the emitting session from index availability |

### Rejected

- **All git.** Transcript-derived rows and config snapshots produce merge conflicts across
  three hosts and bloat the repo within weeks.
- **All SQLite.** Verdicts become invisible to code review and unrecoverable if the mini PC
  dies. The receipt is the one artifact that must outlive the tooling.
- **Postgres.** Correct at ten times this scale. At three projects it is a service to
  babysit.

### Invariant

The index is a cache. Deleting `index.db` must cost only rebuild time, never information.
Anything that cannot be reconstructed from receipts, git, GitHub, and on-disk Claude Code
state does not belong in the index.

---

## 2. Scope and non-goals

### In scope

- A registry of projects, hosts, and recurring cadences.
- Tier routing: cloud routine, desktop scheduled task, or in-session loop, declared once
  per cadence.
- A receipt contract every run honors regardless of tier.
- Collectors for session history, telemetry, git, GitHub, and activated configuration.
- A resolver that answers what is activated, in which scope, and which layer won.
- One consolidated brief, an escalation queue, and cross-project skill distribution through
  a private plugin marketplace.

### Non-goals

- Not an agent runtime. Foreman schedules and reads; it never executes the work itself.
- Not a session viewer. Live session watching stays in `claude agents`.
- Not a replacement for CI. If a check belongs in GitHub Actions it stays there and Foreman
  reads the result.
- No multi-user model. Single operator, three hosts.

---

## 3. Topology

| Host | Role | Runs |
| --- | --- | --- |
| `mini` | System of record | Index, spool drain, desktop scheduled tasks, brief renderer |
| `mbp` | Interactive | Foreground sessions, in-session loops, ad-hoc dispatch |
| `vps` | Long-running | Heavy local-file cadences, media and deck generation |
| `cloud` | Unattended | Routines. No local filesystem, fresh clone per run |

Every receipt records its host. Every cadence declares which hosts may run it. A cadence
with `tier: cloud` has host `cloud` implicitly and must not depend on local paths.

---

## 4. Repository layout

```
foreman/
├── CLAUDE.md                      # context for Claude Code sessions in this repo
├── BUILD.md                       # milestones and acceptance criteria
├── registry.yaml
├── cadences/
│   ├── docs-sync.yaml
│   ├── quality-review.yaml
│   ├── beta-readiness.yaml
│   └── harness-refresh.yaml
├── prompts/
│   └── docs-sync.md
├── schema/
│   ├── registry.schema.json
│   ├── cadence.schema.json
│   ├── receipt.schema.json
│   └── decision.schema.json       # §18
├── collectors/
│   ├── validate.py                # registry + cadence validation
│   ├── build_receipt.py           # C3 receipt assembly
│   ├── brief.py                   # §12 consolidated brief renderer
│   ├── decisions.py               # §18 operator decision queue
│   ├── db.py                      # index connect / migrate / upsert
│   ├── ingest.py                  # receipts -> run, metric
│   ├── escalations.py             # §13 escalation + GitHub issue lifecycle
│   ├── lock.py                    # §14 locking + status=locked receipt
│   ├── dispatch.py                # /dispatch (§11) with quota guard
│   ├── promote.py                 # /promote (§11)
│   ├── quota.py                   # §16.3 quota guard
│   ├── retention.py               # §15 retention job
│   ├── secrets.py                 # §19 secret store + env injection
│   ├── loops.py                   # standard-loops catalog + one-click enable
│   ├── discover.py                # assisted project discovery
│   ├── scheduler.py               # tick: fire due cadences (launchd agent)
│   ├── web.py                     # §12 local web dashboard
│   ├── collect.py                 # run all collectors
│   ├── transcripts.py             # C1
│   ├── telemetry.py               # C2
│   ├── git_state.py               # C4
│   ├── github_state.py            # C5
│   ├── config_resolve.py          # C6
│   └── runstate.py                # C7
├── hooks/
│   ├── session_start.sh           # §18 app-side decision drain
│   ├── session_end.sh             # C3
│   └── stop.sh
├── sql/schema.sql
├── config/telemetry.env           # C2 telemetry env block (§9), pushed to every host
├── docs/SPEC.md                   # this file
├── .claude-plugin/
│   ├── marketplace.json
│   └── foreman-ops/
│       ├── .claude-plugin/plugin.json
│       └── skills/{brief,dispatch,promote,explain-config,close}/SKILL.md
└── state/                         # branch: state, checked out as a worktree here
    ├── receipts/<project>/<cadence>/<iso8601>-<run_id>.json
    └── queue/<project>/<decision_id>.json          # §18
```

---

## 5. Registry

Full schema in `schema/registry.schema.json`. Working example in `registry.yaml`.

Field notes:

- `worktree` is a map keyed by host. A cadence with `tier: local` runs only on hosts
  present in this map.
- `marketplace_pin` at project level overrides `defaults`. Drift from the pin is amber.
  Drift plus a failing cadence is red.
- `large_artifacts: true` suppresses the repo-size check that would otherwise fire
  permanently on projects that produce video and decks.
- `context_budget_tokens` is the always-on plugin cost ceiling per session, summed across
  active plugins. Breaching it is amber on every project at once.

---

## 6. Cadences

Full schema in `schema/cadence.schema.json`. Working example in `cadences/docs-sync.yaml`.

### Tier constraints, enforced at validation time

| Tier | Min interval | Local files | Persistence | Rejects |
| --- | --- | --- | --- | --- |
| `cloud` | 1 hour | No, fresh clone | Survives restarts | `allowed_hosts`, any absolute local path in the prompt |
| `local` | 1 minute | Yes | Survives restarts | `connectors` without a matching local MCP config |
| `session` | 1 minute | Yes | Dies with the session, expires at 7 days | `escalate_when`, since nothing durable is guaranteed to run |

### Run budgets

A cadence may declare a `budget` with any of `max_tokens`, `max_cost_usd`, `max_iterations`,
`wall_clock_minutes`. These are per-run ceilings enforced on launch-mode runs: the scheduler
records each headless run it starts (`collectors/budget.py`) and every subsequent `tick`
reaps the in-flight launches, terminating any that breached a budget and writing a
`status: stopped` receipt (verdict amber). This is distinct from `timeout` (lock expiry, a
crashed or unresponsive run) and from the quota guard (§16.3, fleet-wide headroom, not a
per-run limit). Wall-clock is always enforceable; token/cost/iteration are enforced only when
a usage source supplies the number — a dimension with no observed figure is never guessed.

### Worktree-isolated launch

A launch-mode run of a cadence that `writes` (has a `writes` block) runs in a throwaway
detached git worktree under `spool/worktrees/`, not the project's own tree, so an unattended
run never dirties the working copy an operator might be using. Committed work survives the
worktree (branches live in the shared object store); only the uncommitted working copy is
discarded. The scheduler records the run in the launch registry and the tick reaper removes
the worktree when the process ends (or when a budget stop terminates it). A read-only cadence
runs in place; if the worktree can't be created (e.g. not a git repo) the run falls back to
in-place rather than being skipped.

**Approval gate.** A `writes.open_pr` run is never pushed automatically. When such a run
finishes and left commits on its branch, the tick reaper enqueues an `approve_push` decision
(§18) — a manual-only kind that the normal drain skips. It surfaces in the brief/dashboard
QUEUED section with Approve and Reject: Approve pushes the branch and opens the PR
(`git push` + `gh pr create`), Reject dismisses it. Branch refs survive worktree removal, so
the push works even after the throwaway worktree is reaped. This keeps unattended runs from
opening PRs without a human in the loop.

### Verifier pass

A cadence may set `verify: {enabled: true, model: <cheap-model>}`. When a run produces a
*metric-driven* red (`status: ok`, `verdict: red`), a second cheaper probe re-checks it before
the red is trusted: a red the verifier cannot reproduce is downgraded to amber with a note and
a `verification` block on the receipt. Only `status: ok` reds are eligible — a `failed`,
`timeout`, or `stopped` red is a hard outcome, not a metric call to second-guess. The verifier
is best-effort: any error keeps the red, so a broken verifier never silently clears a genuine
problem. The probe is `claude -p`, or a command in `FOREMAN_VERIFY_CMD` (printing
`{"reproduced": bool}`), wired like `FOREMAN_QUOTA_CMD`. Cuts false reds from flaky checks.

### Scheduling rules

- Recurring session-tier tasks fire up to 30 minutes after the scheduled time, with a
  deterministic offset derived from the task ID. Never schedule two cadences that must be
  ordered relative to each other on the session tier. Order them inside one prompt or use
  the cloud tier.
- Avoid `:00` and `:30` minute fields. One-shot jitter does not apply to other minutes.
- **Time-of-day window.** `autorun_window: {start_hour, span_hours, start_minute}` in `defaults`
  (fleet-wide) or a project (override) sets when preset-scheduled loops fire. `start_hour` +
  `span_hours` spreads loops across a band with a jittered minute (default `08:xx–11:xx`);
  `start_minute` pins an exact minute instead — e.g. `{start_hour: 16, start_minute: 20,
  span_hours: 1}` fires at 16:20. Applies only to loops with a frequency preset (`schedules`),
  and only under auto-run.
- Session-tier recurring tasks expire 7 days after creation. Anything that must outlive a
  week is `cloud` or `local`.
- **Catch-up after downtime.** A tick fires the most recent scheduled occurrence that falls
  after the last tick, and only that one (`Cron.previous_fire`). A laptop asleep across
  several occurrences backfills a single run — the latest — never a stampede of every missed
  one. The lookback horizon is 45 days (covers daily/weekly/monthly); a sparser cadence that
  slept past it falls back to receipt-absence staleness (§16.6), which still surfaces it.

### Standard loops

`collectors/loops.py` holds a catalog of ready-made review loops: `docs-sync`,
`quality-review`, `beta-readiness`, `harness-refresh` (built in) plus `arch-review`,
`security-review`, `soc2-readiness`, `perf-review`, `prod-readiness`, `pen-test`, `test-review`
(materialized on demand).
`loops enable <project> <loop>` writes the cadence + prompt (if new) and wires the project's
`registry.cadences` and the cadence's `applies_to` -- one-click. The generated cadence is an
ordinary one and passes validation. Last-run time per (project, loop) is derivable from the
`run` table (`MAX(ended)`); `loops status <project>` reports it.

---

## 7. Receipt contract

Full schema in `schema/receipt.schema.json`.

The receipt is the only thing the brief reads. Every tier writes the same shape. A run that
produces no receipt is red by absence, which is what makes silent scheduler failure visible.

### Rules

1. `metrics` keys must exactly match the cadence `metrics` list. A missing key is
   `status: failed`, not a partial verdict.
2. `deltas` compare against the previous receipt for the same cadence and project. The run
   computes them. The brief does not.
3. `next_action` is one imperative sentence and is what appears in the brief. A run that
   cannot write one is not green.
4. `status` is one of `ok`, `failed`, `timeout`, `stopped`, `skipped`, `locked`. `stopped`
   is a run the §6 budget guard terminated: verdict amber, empty metrics, the reason in
   `notes`. It is not `failed` (nothing broke) and not `timeout` (the run was responsive).
5. No secrets, no file contents, no transcript excerpts. Receipts are committed to git.
6. Path: `state/receipts/<project>/<cadence>/<ended-iso>-<run_id>.json`.
7. `run_id` is a ULID so lexical order is chronological order.
8. `verification` records the §6 verifier-pass outcome on a red: `{reproduced, verifier}`. A
   red downgraded to amber because the verifier could not reproduce it carries this block.

### Emission mechanics

The `SessionEnd` hook writes the envelope. The cadence prompt writes `metrics`, `deltas`
and `next_action` to `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` during the run, and the
hook merges them. A missing partial yields `status: failed`, which is correct: the run
ended without reporting.

---

## 8. Index

Full DDL in `sql/schema.sql`. Table groups:

| Group | Tables | Fed by |
| --- | --- | --- |
| Runs | `run`, `metric`, `escalation` | Receipts |
| Sessions | `session`, `session_decision`, `skill_fire` | C1 |
| Telemetry | `telemetry_day` | C2 |
| Activation | `config_snapshot`, `config_key`, `component` | C6 |
| Repository | `git_state`, `github_state` | C4, C5 |
| Coordination | `lock`, `quota` | C3, C7 |

---

## 9. Collectors

| ID | Source | Cadence | Writes | Primary failure mode |
| --- | --- | --- | --- | --- |
| C1 | `~/.claude/projects/<enc-cwd>/<uuid>.jsonl`, subagent logs, desktop session dir | 15 min, incremental | `session`, `session_decision`, `skill_fire` | Format changes between Claude Code versions |
| C2 | OTLP metrics and events from Claude Code | Continuous, rolled daily | `telemetry_day` | Telemetry silently off, export not retried |
| C3 | `SessionEnd`, `Stop`, `PostToolUse` hooks | Event | Spool, then `run`, `escalation` | Hook not installed in a project |
| C4 | `git` in each worktree per host | Hourly | `git_state` | Host asleep, worktree moved |
| C5 | `gh` API per repo | Hourly | `github_state` | Rate limit, token expiry |
| C6 | Settings layers, plugin state, skills, MCP, memory, live `claude -p` probe | Daily and on session start | `config_snapshot`, `config_key`, `component` | Declared read succeeds while effective probe fails, producing false confidence |
| C7 | Routine run history, desktop task history, quota | Hourly | `run` status, `quota` | Undocumented surface changes |

### C1 rules

- Never grep transcripts at query time. Tail by byte offset from `session.last_offset`,
  parse forward, commit the offset.
- Hard cap per pass: 25 MB read. Beyond that, record `transcript_bytes`, mark the session
  oversized, and skip. A 102 MB transcript has been reported to hang Claude Code at 90% RAM.
  The collector must never be the thing that reproduces it.
- Extract two things only: decisions and skill fires. Everything numeric comes from C2,
  which is stable and cheap.
- Pin `parser_version` against the `cc_version` that produced the file. On an unrecognized
  record type, log and continue. On a changed envelope shape, stop and raise red rather
  than write wrong rows.
- Transcript path encoding: working directory with non-alphanumeric characters replaced by
  `-`, truncated to 200 characters with a hash of the full path appended when longer.

### C2 setup

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://mini.tailnet.example:4318
OTEL_RESOURCE_ATTRIBUTES=foreman.host=mbp
```

The receiver runs as a persistent service on the collector host (`mini`):
`python -m collectors.telemetry install` writes a launchd agent (macOS) or systemd user unit
(Linux), both KeepAlive/Restart, so the OTLP endpoint outlives any session. The rollup is not
a service — `collect` drains the spool idempotently. Every host's env block is
`config/telemetry.env` (read only at session start).

Metrics consumed: session count, cost, token usage, lines added and removed, commits, pull
requests, edit tool accept and reject decisions, active time. Events carry a `prompt.id`
that links every API request and tool execution from one prompt, which is how a cadence run
is attributed end to end. Claude Code reads telemetry configuration only at startup, so a
config push takes effect on the next session.

---

## 10. Config resolver

Answers three questions per project per host: what is declared, what is effective, what
actually fired.

### Layer order, highest first

1. Managed policies (`managed-settings.json`). Cannot be overridden, except that a stricter
   value from a lower layer wins for a small set of security-sensitive keys.
2. Command line (`--settings` file or JSON), session-scoped.
3. `.claude/settings.local.json`
4. `.claude/settings.json`
5. `~/.claude/settings.json`

### Algorithm

```python
for key in union(all_layers.keys()):
    candidates = [(layer, value) for layer in ORDER if key in layer]
    if is_list_key(key):                       # permissions.allow, deny, etc.
        effective = concat_in_order(candidates)
        winning   = "merged"
    else:
        winning, effective = candidates[0]
    shadowed = [layer for layer, _ in candidates[1:]]
    emit(key, effective, winning, shadowed)

# Environment overlay is applied per pair, not as a layer.
apply_env_pair("model", hard="ANTHROPIC_MODEL", soft="ANTHROPIC_DEFAULT_MODEL")
# hard wins over any file value; soft applies only when no file sets the key.
```

### Two documented traps the resolver must encode, not assume

- A deny rule in a lower layer has been reported to defeat an allow in a higher one,
  contrary to the stated precedence.
- Memory precedence between `CLAUDE.md` and `CLAUDE.local.md` is described inconsistently
  across the docs.

For both, record the file-derived answer and the probe answer, and flag disagreement as
amber rather than choosing.

### Effective probe

File resolution is a prediction. The probe is the measurement. Once daily and on session
start, from inside the project directory:

```bash
claude -p --output-format json \
  "Report as JSON only: active model, permission mode, every enabled plugin with
   version and source, every skill with scope and whether you may invoke it
   unprompted, every connected MCP server, every hook, and every memory file in
   your context with its import chain."
```

Reconcile probe against prediction. Any key where they disagree is written to `config_key`
with `winning_layer = 'probe'` and raises a drift finding. Reconciliation also runs at the
*component* level (`reconcile_components`): a skill the files predict invocable that the
probe says will not fire unprompted is flagged `disagrees_with_probe` and surfaces as drift —
this is the silent-no-op a scheduled cadence would hit in production, caught before it does.
MCP servers predicted enabled but not reported connected, and plugins predicted active but
not reported loaded, are flagged the same way. Reconciliation is conservative: a probe that
does not enumerate a kind is treated as unknown, never as "absent", so an incomplete
model-authored probe cannot manufacture a false drift.

### Activation sources to walk

| Kind | Locations | Trap |
| --- | --- | --- |
| Plugins | `~/.claude/plugins/installed_plugins.json`, cache at `plugins/cache/<mkt>/<plugin>/<ver>/`, `known_marketplaces.json` | Cache and marketplace list can be populated while the plugin is not active. Only `installed_plugins.json` counts. Separately, any folder under a skills directory containing `.claude-plugin/plugin.json` loads as `<name>@skills-dir` with no install step. |
| Skills | `~/.claude/skills/`, `.claude/skills/`, plugin-bundled | Installed is not invocable. `disable-model-invocation: true`, a `skillOverrides` setting, or a `Skill` deny rule means a scheduled fire delivers the text instead of running it. Store `invocable` per skill or cadences silently no-op. |
| MCP | `~/.claude.json` `mcpServers` and per-project `disabledMcpServers`; `.mcp.json` gated by `enableAllProjectMcpServers`, `enabledMcpjsonServers`, `disabledMcpjsonServers`; claude.ai connectors; separate Claude Desktop config | Four independent gates. Compute the AND, do not report the file. |
| Memory | `~/.claude/CLAUDE.md`, project `CLAUDE.md`, `CLAUDE.local.md`, `~/.claude/memory/*.md`, plus the full import chain | Imports are where unaccounted context enters. Walk transitively and record depth. |
| Hooks, agents, commands | Settings files plus plugin-contributed | Plugin-contributed hooks are the ones you forget you enabled. |

### Fleet config diff

`config_resolve fleet-diff <project>` compares the latest snapshot per host and reports where
the resolved config diverges — config keys whose effective value differs (present-vs-absent
counts) and components whose presence or invocability differs ("skill X invocable on mbp,
inert on vps; MCP gh activated on mbp only"). A single-host project diffs to empty. This
catches accidental per-host divergence that no single-host view can show.

### Context budget

`claude plugin details <name>` reports a component inventory and a projected token cost
split into always-on and on-invoke. Sum always-on across active plugins into
`component.always_on_tokens`. Exceeding `context_budget_tokens` is amber on every project
at once, because promoting a plugin taxes all three.

---

## 11. Supervisor skills

Distributed as the `foreman-ops` plugin. Installed in this repo for supervision and in each
project for hook emission.

| Skill | Does | Reads | Writes |
| --- | --- | --- | --- |
| `/brief` | Renders the consolidated board | Index, receipts, roster | Nothing |
| `/dispatch <project> <cadence>` | Fires a cadence off-cycle on the correct tier | Registry, lock | Lock, run row |
| `/promote <skill>` | Moves a skill into the marketplace plugin, bumps version, updates pins | Registry | Marketplace, `registry.yaml` |
| `/explain-config <key> [project]` | Names the winning layer and every shadowed layer | `config_key` | Nothing |
| `/close <escalation-id>` | Resolves an escalation with a reason | `escalation` | `escalation` |

**Distribution caveat.** There is an open report that `extraKnownMarketplaces` plus
`enabledPlugins` in project `.claude/settings.json` does not trigger the documented install
prompt, leaving skills unloaded despite a populated cache. Until confirmed fixed on the
running version, `/promote` must verify installation per project through the C6 probe
rather than assume propagation.

---

## 12. Brief format

One screen, ordered by what needs a decision rather than by project.

```
FOREMAN — Sat 06 Sep, 08:14 ET          quota 61% · $18.40 wk · 3 projects

DRIFT (4)
  paysvc      foreman-ops 0.3.9, pinned 0.4.2
  paysvc      skill docs-sync would not fire (silent no-op)
  mbp          permissions.deny shadows local allow for Bash(rg:*)
  all          always-on plugin cost 2,610 tok, budget 2,400

NEEDS YOU (2)
  R  sentrygw / beta-readiness      red 2d    12 harness cases failing since
     -> Triage tests/sim/replay_*; blocked on the gateway rewrite      PR #204
  A  acmeapi / docs-sync      amber 21d    ages to red today
     -> Rewrite docs/deploy.md sections 3-5                            PR #318

CADENCES
  project      docs-sync   quality-review   beta-readiness   harness-refresh
  acmeapi    amber 3h    green 3h         green 1d         -
  sentrygw      green 9h    running          red 2d           green 6h
  paysvc      green 11h   green 11h        -                -

QUEUED (2)
  paysvc      2 pending, oldest 5d   apply_setting, note   awaiting session (2)
  acmeapi    1 pending, oldest 3h   set_pin

QUIET
  9 green runs, 4 dependabot PRs auto-merged, 2 skills unused for 30d
```

- **Needs you** is the escalation queue, sorted by severity then age. Each line carries the
  `next_action` verbatim from its receipt.
- **Cadences** shows verdict and age. A blank cell means the cadence does not apply. A cell
  older than twice its schedule interval renders as stale, which catches a dead scheduler.
- **Drift** leads the board — the effective-vs-declared gap is Foreman's headline signal. It
  is the C6 output: pin drift, shadowing surprises, component drift (a skill the probe says
  will silently no-op, an MCP/plugin the probe does not confirm), and always-on budget breach.
- **Queued** is the operator decision backlog (§18): decisions waiting to be applied,
  grouped by project with the oldest age. `awaiting session` counts app-side decisions that
  will only apply once a Claude Code session starts in that project. A project whose oldest
  queued decision is older than a threshold is a signal the project is not being worked.
- **Quiet** keeps green work visible without occupying attention and surfaces unused skills
  for pruning.

---

## 13. Escalation policy

- Red opens a GitHub issue in the owning repo, labeled per `registry.escalation.labels`,
  and enters the queue. One issue per cadence and project. A repeat red comments on the
  open issue rather than opening another.
- Amber accumulates silently and ages. It converts to red after
  `amber_ages_to_red_after_days`. Age is measured from the first amber in an unbroken amber
  run, not from the last one.
- A green run closes the open issue with a link to the receipt that cleared it.
- Escalations resolve only through `/close` or a green run. Nothing expires quietly.
- Absence of a receipt within twice the schedule interval is its own escalation, owned by
  the scheduler rather than the project.

---

## 14. Locking and identity

Three hosts and a cloud tier can all be told to run `docs-sync` on `sentrygw`. The first
outage this design produces will be two agents pushing the same branch.

```sql
INSERT INTO lock(key, holder_host, run_id, acquired, expires)
VALUES (:key, :host, :run, :now, :now_plus_timeout)
ON CONFLICT(key) DO UPDATE SET
  holder_host = excluded.holder_host,
  run_id      = excluded.run_id,
  acquired    = excluded.acquired,
  expires     = excluded.expires
WHERE lock.expires < :now;              -- steal only an expired lock

-- if changes() = 0 the lock is held: emit receipt with status = "locked"
```

- Lock key is the cadence `lock_key` template rendered with the project.
- Expiry is `timeout_minutes` plus 5. A crashed run never blocks forever.
- Cloud runs acquire over the Tailscale-reachable index. If the index is unreachable the
  cloud run proceeds and marks the receipt `"lock": "unverified"`, because refusing to run
  unattended work over a monitoring outage is worse than a rare duplicate.
- Every receipt records `host`. Any metric compared across time must be grouped by host or
  you will chase phantom regressions between the Mac and the VPS.

---

## 15. Retention

| Data | Keep | Then |
| --- | --- | --- |
| Receipts | Forever | Git history, never pruned |
| Config snapshots | Every snapshot for 90 days | Keep one per week plus every snapshot whose hash differs from its predecessor |
| Session rows and decisions | 180 days | Delete rows; on-disk transcripts managed separately |
| Telemetry rollups | Daily for 2 years | Monthly aggregate |
| git and github state | 30 days hourly | Daily last-of-day |
| Spool | Until drained plus 7 days | Delete |
| Decisions (§18) | Pending: until applied or expired. Applied/expired: 30 days | Pending never auto-deleted, only marked expired; terminal ones pruned after the window |

Transcript files are not Foreman's to delete. Report total on-disk size per host in the
brief once it passes a threshold and let the operator decide.

---

## 16. Failure guards

1. **Parser version pin.** The transcript entry format is internal to Claude Code and
   changes between versions. Record the `cc_version` that wrote each file. On an
   unrecognized envelope shape, stop C1, keep the offset, raise red. Silent partial parsing
   is worse than no parsing.
2. **Oversized transcript.** Hard read cap per pass; sessions past the cap are marked and
   skipped.
3. **Quota guard.** Cadences consume the same usage limits as interactive work. Below 15%
   headroom, defer every non-red cadence to the next window and say so in the brief.
4. **Blast radius.** `writes.max_files_changed` per cadence. A run exceeding it reports and
   opens no PR.
5. **Probe disagreement.** Prediction versus probe mismatch is amber, never a silent
   overwrite.
6. **Receipt absence.** Treated as a verdict, not a gap.
7. **Surface fragmentation.** The CLI, desktop app, web, and VS Code extension each keep
   their own session history in different roots. C1 walks all configured roots or the brief
   reports a partial picture, which it must say out loud.
8. **Token and secret hygiene.** Per-routine bearer tokens, `gh` tokens, and connector
   credentials are inventoried with expiry dates. Expiry inside 14 days is amber. No
   credential value is ever written to a receipt or the index.
9. **Run budgets.** A cadence's `budget` (§6) caps a single run's tokens, cost, iterations,
   and wall-clock. A launch that breaches one is terminated and gets a `status: stopped`
   receipt, its lock released so the pair is immediately re-runnable. Per-run ceiling,
   complementary to the fleet-wide quota guard (#3).
10. **False-red suppression.** A cadence with `verify` enabled re-checks a metric-driven red
    with a cheaper probe (§6); a red the verifier cannot reproduce is downgraded to amber with
    a note rather than paging a human. Best-effort — a verifier error keeps the red.

---

## 17. Open questions

1. Does `docs-sync` need local files? If the user guide build produces media it cannot be a
   cloud cadence and that cadence's tier default changes.
2. Index on the mini PC or the VPS? The mini PC is closer to interactive work; the VPS has
   better uptime and is already the media worker.
3. Does `/promote` bump one version for the whole plugin or per skill? One version makes
   pin drift a single number; per skill is finer but multiplies the pin surface.
4. Are receipts committed by the cloud run itself, or written to a routine artifact and
   pushed by the mini PC on drain? Direct commit is simpler; drain gives one writer and no
   branch contention.
5. Confirm on the running Claude Code version: the permissions precedence report, the
   memory precedence inconsistency, and the marketplace auto-install behavior. All three
   are encoded here as flagged unknowns rather than assumptions.

---

## 18. Async operator decisions

Receipts are the reverse channel: a run reports a verdict, Foreman reads it. This section
adds the forward channel. The operator makes a call in the supervisor, and if the target
project has no live Claude Code session the decision is queued durably and applied the next
time a session for that project starts. Nothing is lost because a machine was asleep.

### Artifact

A decision is a durable JSON artifact, the mirror of a receipt, validated against
`schema/decision.schema.json`. It lives on the `state` branch at
`queue/<project>/<decision_id>.json` — `state/queue/...` from the main checkout. Git, not
the index: a queued decision must survive deletion of `index.db` (invariant 1).

- `decision_id` is a ULID, so lexical order is application order.
- `status` moves `pending → applied | failed | expired | superseded`. The terminal write
  carries an `applied` block recording when, on which host, and by which session.
- `requires_session` routes the drain. It is derived from `kind` and must agree with it.

### Kinds

| Kind | Side | Effect |
| --- | --- | --- |
| `dispatch_cadence` | app | Surfaces a request to run a cadence off-cycle in-session |
| `answer_question` | app | Delivers the operator's answer to a question the app raised |
| `apply_setting` | app | Edits the project's `.claude/settings.json` (or `.local`) |
| `note` | app | Surfaces a free-text note into the next session's context |
| `set_pin` | supervisor | Edits `marketplace_pin` in `registry.yaml`, comments preserved |
| `close_escalation` | supervisor | Records the operator's resolution (issue close is §13) |
| `pause_cadence` / `resume_cadence` | supervisor | Records a scheduling change |
| `approve_push` | supervisor (manual-only) | The §6 approval gate: on Approve, pushes a run's branch and opens its PR. Never auto-drained — waits for explicit operator approval. |

### Two drain points

- **Supervisor-side** (`requires_session: false`): applied on `mini` during the normal
  drain, `python -m collectors.decisions drain-supervisor`. These need no project session.
- **App-side** (`requires_session: true`): applied by `hooks/session_start.sh` at the next
  SessionStart for the project. The hook resolves the project from the working directory via
  the registry worktree map, applies pending decisions in ULID order, and emits the
  SessionStart `additionalContext` so delivered decisions enter the session's context.
  Because C6 already runs on session start, this adds no new hook surface for the operator.

### Rules

1. **Applying is idempotent and ordered.** Decisions apply in ULID order; a non-pending
   decision is skipped; writing the terminal status is the claim. Re-running a drain is safe.
2. **A bad decision never blocks a session.** An app-side apply that raises is marked
   `failed` with the reason and the drain continues. Session startup is never aborted.
3. **Nothing expires quietly.** A pending decision past its `expires` is marked `expired`
   and stays visible; it is not deleted. A project that never drains its queue shows in the
   brief's QUEUED section rather than silently swallowing operator intent.
4. **`/dispatch` and `/close` are decision producers.** The §11 skills enqueue decisions
   rather than acting directly, which is what makes them work against a project whose
   session is not running.
5. **Cross-host safety.** Two hosts draining the same project race only on the terminal
   write; the loser's write is a no-op because the decision is already non-pending. The §14
   lock hardens this once the index exists.

### CLI

```
python -m collectors.decisions enqueue --project P --kind K --payload '{...}'
python -m collectors.decisions drain-session       # app-side, from a project cwd
python -m collectors.decisions drain-supervisor     # supervisor-side, on mini
python -m collectors.decisions board                # QUEUED render for the brief
python -m collectors.decisions gc                   # expire past-due, prune old applied
```

---

## 19. Secrets and env injection

Foreman inventories credentials (§16.8) but the operator also needs a way to *hold* the
actual key values (LLM, ElevenLabs, Hostinger, ...) once and have Foreman push them into each
project's environment, rotating in one place. This is a separate opt-in subsystem that sits
outside the git-receipt and index boundary, so invariant 3 (no secrets in receipts or the
index) stays intact.

### Store

- Values live only in a **sops-encrypted file**, `secrets/store.sops.yaml`, a flat
  `NAME: value` map. sops encrypts the values with **age** recipients declared in `.sops.yaml`;
  the structure and keys stay readable, the values are ciphertext. The encrypted file is safe
  to commit and gives an auditable history of rotations. **Only sops-encrypted files may live
  under `secrets/`; plaintext is gitignored.**
- age **private** keys live per host (`SOPS_AGE_KEY_FILE`), never committed. Each host that
  must decrypt (mini, mbp, vps) is an age recipient.
- Crypto is delegated to the `sops` and `age` binaries. Foreman never hand-rolls crypto; if
  the binaries are absent the operation fails loudly rather than falling back.

### Declaration

The registry declares, per project, which keys that project needs — **names only**, which is
safe to commit:

```yaml
env_keys: [ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, HOSTINGER_TOKEN]
```

### Default and overrides

One key can have a default value plus per-scope overrides, resolved most-specific-first:
**project > host > default**. The store holds `NAME` (default) and, optionally,
`project:<slug>/NAME` or `host:<host>/NAME`. So two projects that both declare
`ANTHROPIC_API_KEY` share the default unless one has its own `project:<slug>/ANTHROPIC_API_KEY`
(e.g. a different org's billing). `push` records which keys resolved via an override; the
`credential` inventory lists overrides too (names/scopes only, never values).

```
secrets set ANTHROPIC_API_KEY                       # the default (stdin)
secrets set ANTHROPIC_API_KEY --project acmeapi   # override for one project
secrets set ANTHROPIC_API_KEY --scope-host vps       # override for one host
```

### Operations

| Op | Does |
| --- | --- |
| `set NAME [--project P \| --scope-host H]` / `rotate NAME [...]` | Set/replace the default or a scoped override; rotate also pushes and re-inventories |
| `get NAME` | Decrypt one value to stdout (for scripts; never logged) |
| `list` | Key **names** only, never values |
| `push <project> <host>` | Decrypt that project's `env_keys` and write them to the worktree's gitignored `.env` (0600), atomically |
| `inventory` | Populate the `credential` table with name, kind, and expiry — **never a value** |

### Injection

`push` writes `<worktree>/.env` (gitignored, mode 0600). The project's `.envrc` (direnv) or a
launch wrapper sources it before Claude Code starts. Foreman never writes a secret to
`.claude/settings.json` (git-trackable), never into a live process, and never into a receipt
or the index — consistent with "Foreman never does the work": it maintains the `.env`, the
shell loads it.

### Invariant

No secret value is ever written to a receipt, the index, or any git-tracked file other than
the sops-encrypted store (ciphertext). The `credential` table holds names and expiry only;
expiry inside 14 days is amber in the brief (§16.8).

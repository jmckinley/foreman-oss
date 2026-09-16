# Foreman

**The supervisor for a portfolio of software projects built and maintained by coding agents.**
Foreman schedules recurring AI reviews across all your projects, reduces every run to a durable
green / amber / red **verdict**, and shows the whole fleet on one board — with a git-committed
receipt trail behind every verdict.

It is a control repo, a contract, and a small index — **not** a daemon and not an agent runtime.
*Foreman schedules and reads; the cadence prompts do the work.*

- **Positioning & who it's for:** [`docs/ONE-PAGER.md`](docs/ONE-PAGER.md)
- **How it compares:** [`docs/COMPARISON.md`](docs/COMPARISON.md) · tool-by-tool scan in [`docs/COMPETITIVE.md`](docs/COMPETITIVE.md)
- **Design:** [`docs/SPEC.md`](docs/SPEC.md) · **Build order:** [`BUILD.md`](BUILD.md) · **Conventions & invariants:** [`CLAUDE.md`](CLAUDE.md)

## Why

Coding agents made it cheap to *create and maintain* far more software per person. The bottleneck
moved from writing code to **supervising many autonomous streams of it** — is each project still
documented, secure, prod-ready, within budget? Doing that by hand across 10–30 repos doesn't
scale, and per-repo CI only fires on commits and only runs deterministic checks. Foreman is the
standing supervisor for the whole portfolio: it turns open-ended review work into scheduled
agentic **loops**, each emitting an actionable verdict, and escalates only what needs you.

## How it works

Two durable contracts, mirror images of each other, both living in git so they survive any
machine dying and are diffable in review:

- **Receipts — the reverse channel (app → Foreman).** Every run on every tier writes the same
  JSON shape (`schema/receipt.schema.json`) to the `state` branch at
  `receipts/<project>/<cadence>/<ended-iso>-<run_id>.json`. A run with no receipt is **red by
  absence** — which is what makes a silent scheduler failure visible.
- **Decisions — the forward channel (Foreman → app).** You make a call in the supervisor; if the
  target project has no live session, the decision is queued at `queue/<project>/<decision_id>.json`
  and applied at the next SessionStart for that project (SPEC §18).

State lives in three stores (SPEC §1): **receipts** in git, a rebuildable **index** (SQLite), and
an append-only **spool**. The index is a cache — deleting it costs only rebuild time.

**Loops** are the unit of work: a recurring cadence (`cadences/*.yaml`) with a prompt
(`prompts/*.md`) that measures a few metrics and a verdict rule that turns them into
green/amber/red. Amber ages to red; a red opens a GitHub issue; a later green closes it with a
link to the clearing receipt. **Where a loop runs** is its tier — `local` (this host's
scheduler), `cloud` (the routine), or `session` (delivered into your next Claude Code session).

Standard catalog: `docs-sync`, `quality-review`, `security-review`, `soc2-readiness`,
`arch-review`, `perf-review`, `prod-readiness`, `pen-test`, `test-review`, `beta-readiness`,
`harness-refresh`, `ui-ux-review`, plus foreman's own `self-check` and `handover-refresh`. Every
loop is opt-in per project and one-click enable.

## The board

A local, auto-refreshing dashboard (`python -m collectors.web`, binds `127.0.0.1:8787`) and a
token-gated read-only mirror on Vercel. Eight sections:

| Section | What it answers |
| --- | --- |
| **Since you were away** | what changed while you were gone (runs, escalations, queued actions), new-since-last-visit marked |
| **Needs you** | red / overdue verdicts + escalations — the daily triage |
| **Queued** | operator actions waiting: run a loop off-cycle (▶ Run now, on-host), approve a push, apply a setting |
| **Loops** | every project's loops in one place — verdict + trend sparkline, schedule & next fire, where it runs, run / remove / add |
| **Loop library** | the catalog: enable for a project, edit or customize a built-in, add a new loop |
| **Repo status** | per-project git/GitHub vital signs (sync, dirty, PRs, CI, security alerts) + a fleet roll-up |
| **Spend & usage** | cost / tokens / sessions per project from Claude Code telemetry |
| **Drift / Discovered / API keys** | declared-vs-effective config divergence, unregistered repos, secrets used where |

## Non-negotiable invariants

1. **The index is a cache** — never store what can't be rebuilt from receipts, git, GitHub, or
   on-disk Claude Code state.
2. **The receipt is the contract** — one JSON shape, every tier, schema-validated.
3. **No secrets in receipts or the index** (receipts are committed to git).
4. **Never grep transcripts at query time** — tail by byte offset, cap the read (a 102 MB
   transcript can hang the reader).
5. **Declared is not effective** — activation claims come from the `claude -p` probe or are
   labeled predictions. This drives the **Drift** section.
6. **Foreman never does the work** — it schedules and reads; the cadence prompts do the work.

## Quickstart

```bash
python -m collectors.validate                 # registry + every cadence (the pre-commit gate)
sqlite3 :memory: < sql/schema.sql              # DDL parses
python -m pytest tests/                         # full suite (unit + end-to-end)

git worktree add state state                   # the state branch, as a worktree
export FOREMAN_STATE_DIR="$PWD/state"

python -m collectors.discover scan ~ --owner <you> --apply   # register local repos by git origin
python -m collectors.loops enable <project> <loop>           # opt a project into a loop
python -m collectors.collect --state-dir ./state --index ./index.db --host mbp   # snapshot git+GitHub
python -m collectors.web --port 8787           # open http://localhost:8787
```

Operator decisions (SPEC §18):

```bash
python -m collectors.decisions enqueue --project paysvc --kind note \
  --payload '{"text":"ship beta Friday"}'
python -m collectors.decisions board            # what's waiting, per project
python -m collectors.decisions gc               # expire past-due, prune old applied
```

## Layout

```
registry.yaml            projects, hosts, pins, per-project schedule/tier/window overrides
cadences/*.yaml          one loop per file: metrics + verdict rule + where it runs
prompts/*.md             the work each loop performs
schema/                  receipt, registry, cadence, decision contracts
collectors/
  validate.py            registry + cadence validation (pre-commit gate) + the Cron engine
  build_receipt.py       assembles a schema-valid receipt from the envelope + prompt partial
  scheduler.py           launchd agent, tiers, per-project autorun / frequency / window / tier
  loops.py               the loop catalog + library (enable / new / customize / disable)
  collect.py             run all collectors into the index (C1–C7)
  web.py                 the dashboard (stdlib http.server) + every mutating action
  decisions.py           operator decision queue: enqueue / drain / board / gc
  secrets.py             sops/age store, per-scope override, .env injection, key fingerprints
hooks/                   session_start (drain), session_end / stop (receipt emission)
sql/schema.sql           index DDL
state/                   the state branch (worktree): receipts/ and queue/ only, no source
```

## Status

**M1–M8 shipped** plus the §19 secrets store, and a full operator surface on top (dashboard,
launchd scheduler with per-project autorun / frequency presets / time-of-day window / per-loop
run-location override, loop catalog + library, assisted registration, telemetry-driven spend,
verdict trends, activity feed). **231 tests, ~84 % line coverage** (`python -m pytest tests/`).

The remaining work is host-bound operations, not code: stand up the OTLP telemetry receiver on
the always-on host, wire the cloud routine, provision `sops`/`age`, and set `FOREMAN_QUOTA_CMD`
so the quota guard populates automatically. See [`docs/HANDOVER.md`](docs/HANDOVER.md) for the
live working state.

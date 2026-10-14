# Foreman

**Know what your Claude Code fleet is *actually* running — and catch it drifting.** Foreman
probes the **effective** config on every host (not what's declared), commits a tamper-evident
**receipt** for every scheduled review, and shows the whole portfolio on one board where a
silent config change, a regressing loop, or a red verdict can't hide.

Most tools in this space *run* agents. Foreman does the opposite job nobody else does: it tells
you **what's real** — effective-vs-declared **config drift**, a git-committed verdict trail you
can reconstruct from scratch, and per-host activation you can trust. A supervisor you can
*audit*, not an agent swarm.

It is a control repo, a contract, and a small (rebuildable) index — **not** a daemon and not an
agent runtime. *Foreman schedules and reads; the cadence prompts do the work.*

**New:** push **notifications** (red → Slack/Discord/webhook with a deep link), receipt-history
**insights** (regressions, degrading trends, flapping, projections — not just the latest
verdict), and bounded **auto-remediation** (a red can dispatch a scoped fix loop whose PR waits
on your approval). One-command install: `scripts/install.sh`.

- **Positioning & who it's for:** [`docs/ONE-PAGER.md`](docs/ONE-PAGER.md)
- **How it compares:** [`docs/COMPARISON.md`](docs/COMPARISON.md) · tool-by-tool scan in [`docs/COMPETITIVE.md`](docs/COMPETITIVE.md)
- **Design:** [`docs/SPEC.md`](docs/SPEC.md) · **Build order:** [`BUILD.md`](BUILD.md) · **Conventions & invariants:** [`CLAUDE.md`](CLAUDE.md)

## Why

Coding agents made it cheap to *create and maintain* far more software per person. The bottleneck
moved from writing code to **supervising many autonomous streams of it** — is each project still
documented, secure, prod-ready, within budget, and *configured the way you think it is*? Doing
that by hand across 10–30 repos doesn't scale, and per-repo CI only fires on commits and only
runs deterministic checks.

Foreman's wedge is the two things nobody else gives you. First, **declared ≠ effective**: it runs
the live `claude -p` probe on every host and reports where the config actually in force has
**drifted** from what you declared — a shadowed `permissions.deny`, a silently-skipped skill, a
stale marketplace pin. Second, a **git-committed receipt** behind every verdict, so the whole
history is diffable, portable, and reconstructable — the index is just a cache. On top of that
it's a standing supervisor for the portfolio: open-ended review work becomes scheduled agentic
**loops**, each emitting an actionable verdict, escalating only what needs you.

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
token-gated read-only mirror on Vercel. Sections:

| Section | What it answers |
| --- | --- |
| **Since you were away** | what changed while you were gone (runs, escalations, queued actions), new-since-last-visit marked |
| **Needs you** | red / overdue verdicts + escalations — the daily triage |
| **Trends & insights** | what the receipt *history* says: regressions, degrading trends, flapping, chronic stucks, projected reds — not just the latest verdict |
| **Queued** | operator actions waiting: run a loop off-cycle (▶ Run now, on-host), approve a push, apply a setting |
| **Loops** | every project's loops in one place — verdict + trend sparkline, schedule & next fire, where it runs, run / remove / add |
| **Loop library** | the catalog: enable for a project, edit or customize a built-in, add a new loop |
| **Repo status** | per-project git/GitHub vital signs (sync, dirty, PRs, CI, security alerts) + a fleet roll-up |
| **Spend & usage** | cost / tokens / sessions per project from Claude Code telemetry |
| **Drift / Discovered / API keys** | declared-vs-effective config divergence, unregistered repos, secrets used where |

And it reaches *out*, not just in: **notifications** push a red verdict or a queued approval to
Slack / Discord / a webhook / a file with a deep link back to the board — so a supervisor you
don't have open still supervises.

## Three things beyond a status board

A board tells you what's wrong when you look. These make Foreman act like a supervisor:

- **Notifications — it finds you.** A new red verdict or a queued approval is pushed to Slack,
  Discord, a generic webhook, or a local file, with a one-line summary and a deep link into the
  board. No secret is committed — the channel comes from `FOREMAN_NOTIFY_WEBHOOK` (auto-detects
  Slack/Discord) in your env. `foreman notify test` sends a sample; `foreman notify scan` pushes
  what's new. Wired into every `collect`, deduped against a spool watermark so you're pinged
  once, not every tick.
- **Insights — it reads the whole history, not the tip.** Every verdict Foreman ever collected
  is in git; `foreman insights` mines that corpus for what a single verdict can't show —
  **regressions** (green→red), **degrading** trends, **flapping** (a flaky check, not a real
  problem), **chronic** stucks, and a **projected** days-to-red. It surfaces as the *Trends &
  insights* board section and in the notification headline. Nobody can copy this without first
  building the receipt contract — the insight is a property of the corpus, and the corpus is the
  moat.
- **Auto-remediation — it can fix, with your approval.** Opt a project into `auto_remediate` and
  mark a cadence `remediation.enabled`, and a red will **dispatch a scoped fix loop** (worktree-
  isolated, opens a PR). The invariant holds — *Foreman never does the work*; it dispatches a
  loop that does, and the PR waits on the existing `approve_push` human gate before anything is
  pushed. Capped at `max_attempts` per red streak, then it stops and files a note. Off by
  default; `foreman remediate plan` shows exactly what it would do.

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

## Install

One command puts the `foreman` CLI on PATH, scaffolds an instance (`registry.yaml`, the `state`
worktree, `spool/`, the index cache), and runs a preflight:

```bash
scripts/install.sh                    # or: FOREMAN_DIR=~/work/foreman scripts/install.sh
foreman doctor                         # preflight — shows exactly what's still to wire, with fixes
```

`foreman doctor` checks Python, git, `gh` auth, the `claude` binary (for the drift probe), the
state worktree, your notification channel, and the scheduler — printing a copy-pasteable fix for
anything not ready, and failing only on a hard problem (so it's safe in a setup script).

## Quickstart (manual)

```bash
python -m collectors.validate                 # registry + every cadence (the pre-commit gate)
sqlite3 :memory: < sql/schema.sql              # DDL parses
python -m pytest tests/                         # full suite (unit + end-to-end)

foreman init ~/foreman                         # scaffold the instance (registry + state + cache)
git worktree add ~/foreman/state state         # the state branch, as a worktree
export FOREMAN_DIR=~/foreman FOREMAN_STATE_DIR=~/foreman/state

foreman discover scan ~ --owner <you> --apply  # register local repos by git origin
foreman loops enable <project> <loop>          # opt a project into a loop
foreman collect --host mbp                      # snapshot git+GitHub into the index
foreman web --port 8787                         # open http://localhost:8787

export FOREMAN_NOTIFY_WEBHOOK=https://hooks.slack.com/services/...   # optional: ping me on reds
foreman notify test
```

> **Works with private repos.** The GitHub collector shells out to the [`gh`](https://cli.github.com)
> CLI, so it reads whatever your login can see — private repos included. Run `gh auth login` (or set
> `GH_TOKEN`/`GITHUB_TOKEN`) first. Without auth it still runs, degrading GitHub fields to defaults
> rather than erroring; git-side signals (branches, dirty tree, blobs) need no auth at all.

Operator decisions (SPEC §18):

```bash
python -m collectors.decisions enqueue --project paysvc --kind note \
  --payload '{"text":"ship beta Friday"}'
python -m collectors.decisions board            # what's waiting, per project
python -m collectors.decisions gc               # expire past-due, prune old applied
```

## Demo

A **self-contained synthetic demo** — a fictional fleet (receipts, index, and decision queue,
all made-up data) — lets you explore the whole board, differentiators included, with nothing
wired up:

```bash
python scripts/make_demo.py                          # writes examples/demo/
FOREMAN_DIR=examples/demo python -m collectors.web   # open http://localhost:8787
```

You get the full board with everything on show: the **drift banner** firing (a pin mismatch, a
shadowed permission, a skill the probe says would silently no-op), the **Trends & insights**
panel (regressions, chronic reds, flapping, a projected red), green/amber/red verdicts with
sparklines, spend & usage, and a staged **red → auto-fix → approve** flow in Queued.

> If you cloned the public mirror, the repo root is already a pre-built demo — just
> `python -m collectors.web`. The Quickstart's `discover` / `collect` steps replace the demo
> with **your** projects. Demo timestamps are generation-relative, so regenerate to refresh.

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
  notify.py              outbound push (Slack/Discord/webhook/file) for reds + queued approvals
  insights.py            receipt-history intelligence: regressions, trends, flapping, projections
  remediate.py           bounded auto-remediation: red → dispatch a fix loop (approval-gated)
  doctor.py              preflight: python / git / gh / claude / state / notify / scheduler
  secrets.py             sops/age store, per-scope override, .env injection, key fingerprints
hooks/                   session_start (drain), session_end / stop (receipt emission)
sql/schema.sql           index DDL
state/                   the state branch (worktree): receipts/ and queue/ only, no source
```

## Status

**M1–M8 shipped** plus the §19 secrets store and a full operator surface on top (dashboard,
launchd scheduler with per-project autorun / frequency presets / time-of-day window / per-loop
run-location override, loop catalog + library, assisted registration, telemetry-driven spend,
verdict trends, activity feed), and now the three supervisor capabilities above —
**notifications**, receipt-history **insights**, and bounded **auto-remediation** — plus a
one-command installer and `foreman doctor` preflight. **350+ tests** (`python -m pytest tests/`).

The remaining work is host-bound operations, not code: stand up the OTLP telemetry receiver on
the always-on host, wire the cloud routine, provision `sops`/`age`, and set `FOREMAN_QUOTA_CMD`
so the quota guard populates automatically. See [`docs/HANDOVER.md`](docs/HANDOVER.md) for the
live working state.

# Foreman — one-pager

## The assurance control plane for agent-built software

Coding agents made it cheap to **create and maintain far more software per person**. The
bottleneck moved from *writing code* to **supervising many autonomous streams of it**. One
operator now runs 10–30 agent-maintained projects — and nothing exists to keep them all healthy
without babysitting each one.

**Foreman is the supervisor for that portfolio.** It schedules recurring AI reviews across every
project, reduces each run to a durable **green / amber / red verdict**, and shows the whole fleet
on one board — with a git-committed receipt trail behind every verdict. It never does the work
itself: it *schedules and reads*; the review prompts do the work.

## What it does

- **Scheduled agentic reviews (loops).** docs-sync, security-review, soc2-readiness, arch-review,
  perf-review, prod-readiness, pen-test, test-review, UI/UX, quality — recurring cadences, opt-in
  per project, each emitting an actionable verdict. Not deterministic CI checks — *open-ended
  judgment*, on a schedule, decoupled from commits.
- **One board.** "Since you were away," "Needs you" triage, a per-project Loops panel (verdict +
  trend, next fire, where it runs, run/remove/add), repo vital-signs, and spend by project.
- **Escalation that closes the loop.** Amber ages to red; a red opens a GitHub issue; a later
  green closes it with a link to the clearing receipt.
- **A tamper-evident audit trail.** Every run writes the same schema-validated JSON receipt,
  committed to git — a versioned record of *what was reviewed, when, and every verdict*.

## Why it's different (the moats)

- **Receipt-as-contract, index-as-cache.** Every verdict is a git-committed JSON receipt; the
  database is a throwaway cache, fully rebuildable. Inspectable, versioned, portable — not a
  stateful black box.
- **Declared ≠ effective.** Foreman doesn't trust config; it *probes* what's actually in effect
  (`claude -p`) and reports **drift** — pins, shadowed permissions, silently-skipped skills.
  No comparable tool does this.
- **Portfolio + time, metered.** Cloud / local / session run-tiers, per-run budgets, a quota
  guard, and spend-by-project. Agent runs treated as a metered resource across a fleet.
- **A supervisor, not a swarm.** "Foreman never does the work" keeps it composable and safe — it
  orchestrates *when* agents run and reads *what they concluded*.

## Who it's for

The operator — solo builder, studio, or platform team — running **many** agent-maintained
projects who needs portfolio-level assurance: *is every project still documented, secure,
prod-ready, on-budget — and can I prove it?*

## The wedge

**Continuous assurance for AI-built software.** The security / soc2 / pen-test loops plus the
git-committed receipt history are, together, a tamper-evident record that every project was
reviewed on a schedule, with every verdict and its clearing. That's not a nice-to-have — it's
audit evidence with a buyer and a budget.

## Honest limits

Deeply wired to Claude Code today (the drift probe is ecosystem-specific — and the moat).
Verdicts are LLM judgments (mitigated by an independent verifier pass). Currently a
single-operator control plane — local dashboard + a token-gated read-only mirror — not yet
multi-tenant SaaS. Cloud-tier ops are partly host-bound and not fully wired.

## Status

M1–M8 shipped + secrets store + full operator surface. **231 tests, ~84 % coverage.** Live as a
local dashboard with a hosted read-only board. See [`COMPARISON.md`](COMPARISON.md) for how it
sits in the field and [`../README.md`](../README.md) to run it.

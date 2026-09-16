# Competitive assessment

A point-in-time snapshot (2026-09-06) of where Foreman sits among similar open-source tools.
The "AI-agent ops / scheduled-review-fleet" category exploded in 2026, so this will date fast
— re-run the scan before making strategic calls.

## Closest comparables

| Tool | What it is | Overlap with Foreman |
| --- | --- | --- |
| **5dive** | Self-hosted "company of AI coding agents": cron + heartbeat scheduling, multi-agent, babysit + "needs-you" triage dashboard | Closest overall — cron + a needs-you board ≈ Foreman's scheduler + brief |
| **MartinLoop** | Execution-control layer around Claude Code: cost/token budgets, iteration caps, stop conditions, independent verifier passes, signed run receipts | Directly overlaps the receipt contract + quota guard |
| **AgentsRoom** | Cron-style scheduler for coding agents; per-project recurring runs | Foreman's cadences/loops concept |
| **Bernstein** | Deterministic Python orchestrator; worktree isolation; verify with tests/lint; same-inputs→same-outputs | Foreman's "Python schedules, not the LLM; deterministic" philosophy |
| **agent-fleet-o / AWS cli-agent-orchestrator / Multiclaude / construct** | Supervisor→worker fleets, DAG workflows, tmux/worktree/EKS parallelism | These *run* agents; Foreman deliberately doesn't |
| **Claude Code Desktop Scheduled Tasks** (Mar 2026) | Built-in scheduler in the product | The substrate Foreman's cloud/local tiers ride on |

## Where Foreman leads / holds up

- **Receipt contract + "index is a cache."** One git-committed JSON verdict per run; the index
  is fully rebuildable from receipts/git/GitHub. Cleaner, more auditable than most (MartinLoop
  has "signed run receipts" but not the rebuildable-index stance).
- **"Declared is not effective."** Settings-layer merge + live `claude -p` probe reconciliation,
  config-drift detection, per-host activation resolution. No comparable feature found in any
  competitor — this is the genuine moat.
- **Integrated secrets** (sops, per-project override, `.env` discovery, collision-proof key
  fingerprints) — atypical for the category.
- **One consolidated cross-project board** with the escalation→GitHub-issue lifecycle and a
  standard-loop catalog (docs/security/perf/arch/pen-test/prod-readiness) as one-click.

## Where Foreman is behind

- **Maturity/packaging.** 5dive/AgentsRoom ship one-command spin-up and communities; Foreman is
  a personal control repo — no installer, no ecosystem.
- **Execution engine.** Bernstein/agent-fleet-o do worktree parallelism, DAG workflows, and
  human-in-the-loop approvals; Foreman "never does the work," so its launch mode is basic and
  it leans on external sessions/scheduler.
- **Cost/execution control.** MartinLoop's budgets, iteration caps, stop conditions, and
  verifier passes are more mature than Foreman's single quota guard.
- **UI polish & scale.** 5dive ships a real triage dashboard; Cloudflare's system did ~131k
  review runs/month. Foreman's dashboard is a functional stdlib server, single-operator.

## Verdict

A credible, unusually principled entrant in a now-crowded field. Foreman won't win on "run the
most agents fastest" (fleet/DAG tools own that) or polish (5dive/MartinLoop). Its defensible
angle is **correctness & auditability**: the git-native receipt contract, the effective-config
drift probe, and multi-tier per-host activation — "a supervisor you can trust and reconstruct,"
not "an agent swarm." Sharpen the wedge by leaning into config-drift/effective-activation (nobody
else has it) while adopting the strongest table-stakes ideas from the field (see BUILD.md M8).

## Sources

- https://agentsroom.dev/features/scheduled-tasks — AgentsRoom scheduled tasks (also surfaced
  5dive, MartinLoop, Claude Code Desktop scheduled tasks)
- https://stackshare.io/bernstein-declarative-agent-orchestration — Bernstein
- https://github.com/escapeboy/agent-fleet-o — agent-fleet-o
- https://github.com/awslabs/cli-agent-orchestrator — AWS cli-agent-orchestrator
- https://github.com/bradagi/awesome-cli-coding-agents — directory of CLI coding agents
- https://www.augmentcode.com/tools/open-source-agent-orchestrators — 9 open-source orchestrators
- https://blog.cloudflare.com/ai-code-review/ — Cloudflare AI code review at scale

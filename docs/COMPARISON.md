# Foreman — comparison matrix

Where Foreman sits versus the *categories* it's often confused with. For a live, tool-by-tool
scan of the fast-moving "AI-agent ops" field (5dive, MartinLoop, AgentsRoom, Bernstein, …), see
[`COMPETITIVE.md`](COMPETITIVE.md).

## Category matrix

Foreman is none of these — it occupies the gap between them.

| Dimension | **Foreman** | CI/CD (GH Actions) | Code-quality SaaS (Sonar/Snyk) | Coding agents (Claude Code, Cursor, Devin) | Agent-swarm frameworks (claude-flow, CrewAI) | Observability (Datadog) |
| --- | --- | --- | --- | --- | --- | --- |
| **Unit of concern** | a *portfolio* of projects, over time | one repo, one commit | one repo | one task / session | one task (fanned out) | running services |
| **Trigger** | schedule / cadence | commit / PR | commit / scan | human prompt | human prompt | always-on |
| **What the "check" is** | open-ended AI review | deterministic script | fixed rule set | the work itself | the work itself | metrics/traces |
| **Does the work?** | **No — schedules & reads** | runs scripts | analyzes | **yes** | **yes** | no |
| **Output** | green/amber/red **verdict** + next action | pass/fail | findings list | a diff/PR | a result | dashboards/alerts |
| **State model** | **receipts in git; index is a cache** | logs (ephemeral) | vendor DB | session/none | session/none | vendor TSDB |
| **Declared-vs-effective config drift** | **yes (probe)** | no | no | no | no | config-only |
| **Portfolio / fleet view** | **yes** | per-repo | per-repo | no | no | yes (services) |
| **Audit trail (versioned verdicts)** | **yes — git-committed** | partial (run logs) | vendor-held | no | no | retention-limited |
| **Cost/tier aware** | **yes (tiers, budgets, spend)** | minutes billing | n/a | per-call | some | usage billing |

## Foreman's capability set

| Capability | Foreman |
| --- | --- |
| Scheduled recurring reviews per project (loops) | ✅ catalog of 14, one-click enable, per-project frequency |
| Verdict contract (green/amber/red) with amber→red aging | ✅ |
| Git-committed, schema-validated receipt per run | ✅ (invariant: receipt is the contract) |
| Rebuildable index (delete-and-rebuild safe) | ✅ (invariant: index is a cache) |
| Declared-vs-effective config **drift** probe | ✅ **(no known competitor)** |
| Escalation → GitHub issue → auto-close on green | ✅ |
| Per-run budgets + quota guard + spend-by-project | ✅ |
| Run-location tiers (cloud / local / session) | ✅ |
| Integrated secrets (sops/age, per-scope override, `.env` inject) | ✅ (atypical for the category) |
| One consolidated cross-project board | ✅ 8 sections + read-only hosted mirror |
| "Since you were away" activity + verdict trend sparklines | ✅ |
| Runs the agents / DAG execution engine | ⛔ by design — *never does the work* |
| Multi-tenant / RBAC / team SaaS | 🚧 single-operator today |

## The one-sentence positioning

The fleet/DAG tools win "run the most agents fastest"; the SaaS tools win polish. **Foreman's
defensible ground is correctness & auditability** — the git-native receipt contract, the
effective-config drift probe, and per-host multi-tier activation. *A supervisor you can trust and
reconstruct, not an agent swarm.*

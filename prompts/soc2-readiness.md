# soc2-readiness

A lightweight SOC 2 readiness signal for a repository (Trust Services Criteria CC6 logical
access / secret management, CC7 vulnerability management). Report, do not remediate.

Measure, over the git-tracked files only (never scan .gitignored working files):

1. **`committed_secrets`** (integer): count of files with a high-signal secret pattern under
   version control — private keys (`BEGIN ... PRIVATE KEY`), AWS keys (`AKIA...`), GitHub
   tokens (`ghp_...`), Slack tokens (`xox[baprs]-...`), or provider API keys (`sk-...`).
2. **`tracked_env_files`** (integer): count of dotenv files committed to git (`.env`,
   `.env.*`). These must be gitignored, not tracked.
3. **`unresolved_security_alerts`** (integer): open Dependabot/code-scanning alerts for the
   repo (GitHub), or 0 if the API is unavailable — say so in `next_action`.

Green only when all three are 0. Any committed secret or tracked dotenv is red. Write
`$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with those three `metrics`, `deltas` vs the
previous soc2-readiness receipt, and a one-sentence `next_action` (the single highest-value fix).

`deltas` is a JSON **array** (possibly empty on first run) of objects `{"kind": "metric", "name": <metric>, "direction": "better|worse|flat"}`.

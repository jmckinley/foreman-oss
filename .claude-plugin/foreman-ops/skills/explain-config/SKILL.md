---
name: explain-config
description: Explain how a configuration key resolves for a project — which layer won and every layer it shadowed, plus whether the live probe disagreed with the file prediction. Use when the operator asks why a setting has its value, which settings file wins, why a permission/model/MCP/skill is active, or "explain-config <key> [project]". Reads the config snapshot from the index.
---

# /explain-config

Name the winning layer and every shadowed layer for a configuration key, from the config
snapshot in the index (SPEC.md section 10). Answers "why does this key have this value?"

## Usage

`/explain-config <key> [project]` — e.g. `/explain-config permissions.deny paysvc`.

Keys are dot-paths into the settings tree: `permissions.deny`, `permissions.allow`, `model`,
`enableAllProjectMcpServers`, etc.

## How to run it

```bash
python -m collectors.config_resolve explain <key> <project> --index "$FOREMAN_INDEX"
```

Present the output as-is. It reports:

- **effective** — the resolved value. For list keys (permissions.allow/deny, the MCP
  allow-lists) this is the concatenation across layers in precedence order, which is exactly
  why a lower-layer `permissions.deny` still applies over a higher-layer allow.
- **winning layer** — one of `managed`, `cli`, `local`, `project`, `user`, `merged`,
  `env:hard`, `env:soft`, or `probe`. Layer precedence, highest first: managed > command-line
  > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`.
- **shadowed** — every lower layer that also set the key and lost.
- A **probe** note when the live `claude -p` measurement disagreed with the file prediction;
  in that case the probe wins and the key is drift (raised amber in the brief).

## Notes

- If there is no snapshot yet for the project, run the collectors first
  (`python -m collectors.config_resolve resolve <project> --project-dir <path>`), then retry.
- Declared is not effective (invariant 5): this skill reports the reconciled answer, and
  flags where the file prediction and the probe measurement diverged rather than guessing.

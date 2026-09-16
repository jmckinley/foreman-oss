---
name: promote
description: Promote a skill into the foreman-ops marketplace plugin, bump the version and pins, and verify it installed per project. Use when the operator wants to distribute a user skill to all projects, e.g. "promote the graphify skill" or "move my new skill into the plugin". Verifies installation via the live probe rather than assuming it propagated.
---

# /promote

`/promote <skill>` moves a user skill into the `foreman-ops` plugin, bumps the plugin +
marketplace version and the registry pin together, then verifies installation per project via
the C6 probe (SPEC.md section 11).

## Run it

```bash
python -m collectors.promote <skill> --foreman-dir "$FOREMAN_DIR" \
  --source-dir ~/.claude/skills
```

Report the JSON result:

- `version` — the new patch version applied to `plugin.json`, `marketplace.json`, and the
  `defaults.marketplace_pin` in `registry.yaml`.
- `verified` — per project, `{installed, invocable}` from the live probe.
- `propagated` — true only if every project reports the skill invocable.

## Why verify, don't assume

There is an open report that `extraKnownMarketplaces` + `enabledPlugins` in project settings
does **not** always trigger the documented install prompt, leaving a skill unloaded despite a
populated cache. So `/promote` measures with the probe rather than trusting propagation. If
`propagated` is false, name the projects where the skill is not yet invocable and tell the
operator to accept the install prompt there (or re-sync the marketplace).

## Notes

- After promoting, commit `registry.yaml`, `plugin.json`, `marketplace.json`, and the moved
  skill directory. The version bump is what the harness-refresh cadence measures as pin drift
  until each host updates.

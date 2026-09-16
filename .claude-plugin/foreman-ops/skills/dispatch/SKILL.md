---
name: dispatch
description: Fire a Foreman cadence off-cycle for a project, on the correct tier. Use when the operator wants to run a cadence now rather than wait for its schedule, e.g. "dispatch acmeapi docs-sync" or "run quality-review on sentrygw now". Honours the quota guard (defers non-red cadences below 15% headroom) and enqueues a decision so it works even if the project has no live session.
---

# /dispatch

`/dispatch <project> <cadence>` fires a cadence off-cycle (SPEC.md sections 11, 16.3, 18).

It is a decision producer: rather than launching work directly, it enqueues a
`dispatch_cadence` decision that the project's next session (or the cloud routine) picks up,
so it works against a project that is not currently running. Before enqueuing it checks the
quota guard.

## Run it

```bash
python -m collectors.dispatch <project> <cadence> \
  --state-dir "$FOREMAN_STATE_DIR" --foreman-dir "$FOREMAN_DIR" --index "$FOREMAN_INDEX"
```

Report the JSON result:

- `{"dispatched": "<decision-id>", ...}` — queued; it will run at the next session/window.
- `{"deferred": true, "reason": "quota headroom N% < 15% ..."}` — **not** queued because usage
  is low and the cadence is not currently red. Say so; suggest retrying after the window
  resets, or note that a red cadence would not have been deferred.

## Notes

- The cadence must apply to the project (be in its `registry.cadences`), or dispatch errors.
- Duplicate protection at run time is the §14 lock: two runs for the same project+cadence
  produce one run and one `status: locked` receipt.

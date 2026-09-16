---
name: close
description: Resolve a Foreman escalation with a reason, closing its GitHub issue. Use when the operator wants to close, resolve, dismiss, or acknowledge an escalation out of band (not by a green run), e.g. "close escalation 12345 fixed in #204" or "resolve the sentrygw beta-readiness escalation". Escalations otherwise resolve only through a green run.
---

# /close

`/close <escalation-id> <reason>` resolves an escalation and closes its GitHub issue
(SPEC.md sections 11, 13). Escalations otherwise clear only through a green run; this is the
manual path when the operator has handled it another way.

## Run it

Find the id in the brief's NEEDS YOU section or the index, then:

```bash
python -m collectors.escalations close <escalation-id> "<reason>" \
  --index "$FOREMAN_INDEX" --foreman-dir "$FOREMAN_DIR"
```

Report the outcome:

- resolved → the escalation is marked resolved with the reason, and if it had a GitHub issue
  that issue is closed with a "Closed via /close" comment.
- already resolved / unknown id → no-op; say so.

## Notes

- A `/close` does not stop a future red: if the underlying condition recurs, a new episode
  opens a fresh escalation and issue.

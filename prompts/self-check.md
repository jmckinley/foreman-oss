# self-check

Foreman supervising itself. Verify the repo is healthy and write the receipt partial.

1. Run the test suite; count failing tests -> `tests_failed`.
2. Run `python -m collectors.validate`; `validate_ok` = 1 if it exits 0, else 0.
3. Count uncommitted tracked changes (`git status --porcelain`) -> `uncommitted_files`.

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with `metrics` (exactly those three keys),
`deltas` (versus the previous self-check receipt for foreman), and a one-sentence `next_action`.

`deltas` is a JSON **array** (possibly empty on first run) of objects `{"kind": "metric", "name": <metric>, "direction": "better|worse|flat"}`.

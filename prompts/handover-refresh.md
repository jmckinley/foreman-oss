# handover-refresh

Foreman keeping its own handover doc and memory current, so the next session (human or agent)
starts from the truth instead of a stale snapshot. You edit two things and then report how stale
they were *before* you fixed them.

## What to read (the current truth)

1. `git log --oneline -40` and `git log -1 --format=%cd docs/HANDOVER.md` — what's changed since
   the handover was last touched.
2. The test suite size: `python -m pytest tests/ -q 2>&1 | tail -1` (count), and optionally
   `python -m coverage run --source=collectors -m pytest tests/ -q && python -m coverage report`
   for the overall %.
3. `docs/HANDOVER.md` itself, section by section, and `BUILD.md` for milestone state.
4. Your project auto-memory: `MEMORY.md` (the index) plus the `memory/*.md` files Claude Code
   loads each session (on this host:
   `~/.claude/projects/-Users-johnmckinley-foreman/memory/`).

## What to update

- **`docs/HANDOVER.md`** — bring it to current reality: the "Last updated" line (today + a
  one-line summary), milestone/feature state, the dashboard section list, the test count +
  coverage, and the "Remaining / honest gaps". Keep it tight; prune anything no longer true.
- **Memory** — `foreman-build-status.md` (test count, shipped features, what remains) and the
  `MEMORY.md` index line for it; add/fix other entries only if they're now wrong. Don't duplicate
  what the repo already records (code, git history); memory is for the non-obvious.

Make the smallest honest edits. Do **not** invent progress — if something is unwired, say so.

## Metrics to measure (before your edits) and write

- **`handover_sections_stale`** (integer): how many `docs/HANDOVER.md` sections materially
  disagreed with current reality when you started (wrong test count, missing/renamed dashboard
  sections, a milestone or gap that's no longer accurate).
- **`memory_entries_stale`** (integer): how many memory entries (incl. the build-status note and
  the `MEMORY.md` index) were out of date.
- **`days_since_handover_update`** (integer): whole days between the handover's previous
  "Last updated" date and today.

Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with `metrics` (exactly those three keys),
`deltas` (a JSON **array**, possibly empty, of `{"kind": "metric", "name": <metric>,
"direction": "better|worse|flat"}` versus the previous handover-refresh receipt for foreman), and
a one-sentence `next_action` (e.g. "Committed the refreshed handover + build-status memory.").

Green means the doc + memory were already current (nothing stale). Leave the working tree with the
edits made; the operator commits them (foreman works directly on `main`).

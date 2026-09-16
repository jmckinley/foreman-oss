# Foreman

Supervisor for multiple concurrent Claude Code projects. Schedules recurring cadences,
collects a verdict from every run, and resolves what is actually activated per project and
host.

Read `docs/SPEC.md` before changing anything structural. Read `BUILD.md` for what to build
next and what "done" means for each milestone.

## Non-negotiable invariants

1. **The index is a cache.** Deleting `~/foreman/index.db` must cost only rebuild time.
   Never store anything in SQLite that cannot be reconstructed from receipts, git, GitHub,
   or on-disk Claude Code state.
2. **The receipt is the contract.** Every run on every tier writes the same JSON shape,
   validated against `schema/receipt.schema.json`. A run with no receipt is red by absence.
3. **No secrets in receipts or the index.** Receipts are committed to git.
4. **Never grep transcripts at query time.** Tail by byte offset, cap the read at 25 MB per
   pass. A 102 MB transcript can hang Claude Code.
5. **Declared is not effective.** Any statement about what is activated must come from the
   `claude -p` probe or be labeled as a prediction.
6. **Foreman never does the work.** It schedules and reads. Cadence prompts do the work.

## Conventions

- Python 3.11, standard library plus `pyyaml`, `jsonschema`, `httpx`. No framework.
- Collectors are idempotent and resumable. Every collector can be re-run over the same
  window without duplicating rows.
- All timestamps stored as ISO 8601 UTC strings. Cron expressions are interpreted in local
  time by Claude Code, so convert at write time and record both.
- `run_id` is a ULID.
- Cron minute fields avoid `:00` and `:30` to dodge one-shot jitter.
- Shell scripts are `bash`, `set -euo pipefail`, POSIX-portable across macOS and Linux since
  they run on both `mbp` and `mini`/`vps`.

## Validation before commit

```bash
python -m collectors.validate            # registry + all cadences against schemas
sqlite3 :memory: < sql/schema.sql        # DDL parses
jsonschema -i state/receipts/**/*.json schema/receipt.schema.json
python -m pytest tests/                   # full app suite incl. end-to-end integration
```

## Things that are known-uncertain

These are flagged in SPEC.md section 17. Do not code around them as if settled; probe and
record disagreement instead.

- Permissions precedence: a lower-layer deny may defeat a higher-layer allow.
- Memory precedence between `CLAUDE.md` and `CLAUDE.local.md` is documented inconsistently.
- Project-level `extraKnownMarketplaces` may not trigger plugin install.

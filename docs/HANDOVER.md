# Handover

Working state of the Foreman build. Read alongside `BUILD.md` (milestones + acceptance) and
`docs/SPEC.md` (design). Last updated 2026-09-14 (dashboard folds to a scannable digest —
foldable sections + orientation legend + open-on-signal + collapsed peeks; "Recently worked on"
per-project prompt timeline with search backed by an hourly local prompt collector + secret/PII
redaction; "every N days" scheduler; intuitive-ux loop; 303 tests).

## Repo

- GitHub: `acme-demo/foreman` (**private**). Two branches:
  - `main` — control repo (schemas, collectors, cadences, plugin, web dashboard). Commit work
    directly here (no feature branches).
  - `state` — orphan branch, receipts + decision queue only, never merged into `main`.
- Python 3.11, stdlib + `pyyaml`, `jsonschema`, `httpx`. No web framework — the dashboard is
  `collectors/web.py` on `http.server`.

## Milestones (all shipped)

| Milestone | Notes |
| --- | --- |
| M1 — contract end to end | schemas, `validate.py`, `docs-sync` cadence+prompt, hooks, `build_receipt.py`, `state` branch |
| M2 — `/brief` from git | `foreman-ops` plugin, `brief.py`, staleness + amber aging |
| M3 — index, C4/C5, locking | `db.py`, `ingest.py`, `lock.py`, `git_state.py`, `github_state.py`, `collect.py` |
| M4 — config resolver, DRIFT | `config_resolve.py` (C6), `/explain-config`, brief DRIFT |
| M5 — telemetry + transcripts | `telemetry.py` (C2), `transcripts.py` (C1) |
| M6 — steady state | cadences, `escalations.py`, `dispatch.py`, `promote.py`, `quota.py`, `retention.py` |
| M7 — async operator decisions | `decisions.py`, `hooks/session_start.sh`, SPEC §18 |
| Secrets (§19) | `secrets.py`, age/sops store, env injection, per-scope override, fingerprints |

**M8 (competitor-inspired) progress:**

- **M8.1 run budgets + stop** — cadence `budget` (tokens/cost/iterations/wall-clock);
  `collectors/budget.py` reaps in-flight launches per tick, writes `status: stopped` receipts
  (verdict amber), releases the lock. Distinct from `timeout` and the quota guard.
- **M8.5 backfill/catch-up** — `Cron.previous_fire`: after downtime the scheduler fires the
  most recent missed occurrence only, not every one (fixes the old 2-day `fires_between` cap
  that silently skipped weekly/monthly cadences).
- **M8.6 deeper drift (moat)** — `reconcile_components` extends the probe reconciliation past
  model/permission_mode to skills (silent-no-op catch), MCP, plugins; `component.disagrees_
  with_probe` persists it; **DRIFT now leads the brief**.
- **M8.7 fleet config diff** — `config_resolve fleet-diff <project>`: cross-host divergence of
  the resolved config (keys + component presence/invocability).
- **M8.2 verifier pass** — cadence `verify: {enabled}`; a metric-driven red the verifier can't
  reproduce is downgraded to amber with a `verification` block (`FOREMAN_VERIFY_CMD` or `claude -p`).
- **M8.3 one-command install** — `foreman` console entry (`collectors/cli.py`) + `foreman init`
  (`collectors/init.py`) scaffolds an instance (dirs, schema, registry, index, first project,
  optional services); `pyproject.toml` ships the `foreman` script (editable install works; a
  relocatable wheel needs the root data dirs relocated — noted in pyproject).
- **Deploy/ops (partial)** — `telemetry install` runs the OTLP receiver as a launchd/systemd
  service. Host-bound remainder unwired: run it on mini, push `config/telemetry.env`, the
  cloud routine, sops/age provisioning.
- **Dashboard** — every board section now has a descriptive subtitle; `db.connect` self-heals
  additive columns so read paths don't crash on an older-schema index.

## Operator surface (built after the milestones)

- **Web dashboard** (`collectors/web.py`, `http.server`, binds `127.0.0.1:8787`). Header carries a
  wordmark + tagline + quota/gh chips, then an **orientation legend** (`_legend_strip` — dismissible
  "How to read this board": the green/amber/red colour key + a plain-language glossary; open by
  default, remembered per browser). **Folds to a scannable digest**: every top-level section is a
  collapsible `<details class="sec">` produced by one post-processor, `_foldablize(html, secsum,
  open_ids)`, which rewrites `<section data-sec="id">…</section>` → summary (the `<h2>` + count
  badge) + body. Each section **opens on signal** (in `open_ids`: Needs you if a to-do, Queued if
  waiting, Repo status if red/amber, Discovered if unregistered, Loops if a red loop — else folded)
  and carries a **collapsed peek** (`secsum[id]`, shown only while collapsed: "3 issue · 11 attn",
  "next run: self-check in 20h", "✓ in sync", …) so the folded board is itself an at-a-glance
  dashboard. A saved per-browser fold choice (`f-sec:<id>`) overrides the default.

  Section order (local): **Recently worked on** (local-only, below) · **Since you were away**
  (recent-activity feed, height-capped ~8 rows + scroll) · **Needs you** · **Queued** · **Loops**
  (the whole per-project surface: per-project accordion with auto-run/queue + time-of-day window,
  a row per loop — last run, verdict+sparkline, **schedule**, **where it runs**, **▶ run**/**✕
  remove**; a "+ Add a loop" form; summary line leads with **"next run: <loop> · in Xh"**) ·
  **Loop library** (built-in/library/custom, enabled-for, safety chips, ✎ edit/customize, create) ·
  **Discovered** (unregistered repos + scan/register; moved up into the "manage" zone) · **Repo
  status** · **Spend & usage** · **Drift** · **API keys & secrets** (declared keys, `✎ .env` edit,
  with the shared-value alert folded in — the old standalone "Shared keys" section was merged here,
  11→10 sections).

  - **Repo status** — fleet roll-up then per-project accordions, each **leading with a "Suggested
    next" block** (`repo_health.suggestions(g, h)`): raw git/GitHub counts synthesized into a
    prioritized (red→amber→info) action list, folding related signals into one (security+dependabot
    → "Patch dependencies"; dirty+stale/agent branches+orphan worktrees → "Tidy the workspace"). Top
    action shows in the collapsed row (`➜ Fix CI`); stat tiles (`_repo_stats`) + per-stat chips
    (`repo_health.signals`) are secondary. Thresholds are named constants in `repo_health.py`
    (`DIRTY_AMBER=50`/`STALE_BRANCH_AMBER=3`/`AHEAD_AMBER=10`/`BLOB_RED_MB=50`/`OLD_PR_AMBER_DAYS=7`).
  - **Recently worked on** (`_recent_section`) — **LOCAL ONLY** (skipped in `read_only`): one row
    per project of the last human prompt you typed to Claude Code, each expandable to that project's
    prompt **timeline**; plus a **search** box over your whole prompt history (`?pq=` GET param, so
    it survives the meta-refresh) and a **"this week"** per-project tally. Sourced from the hourly
    prompt store (below); falls back to a live bounded-tail read when the store is empty. Prompt
    text is secret/PII-redacted and never leaves the machine (see Prompt history).

  Each `_h2` header carries an accent colour + one-line purpose subtitle. Mutating POSTs route
  through **`web.apply_action(action, form, *, foreman_dir, state_dir, index_path)`** (a
  module-level fn the request handler delegates to — unit-testable without a server); failures
  show a red banner via `/?err=`. Native `<select>`s are styled; per-repo accordions remember
  open/closed in `localStorage`. "Tier" is surfaced as **"where it runs"** (this host / cloud /
  next session / default) — the schema field is still `tier`.
- **Prompt history** (`collectors/prompts.py` + `collectors/recent.py` + `collectors/redact.py`):
  an **hourly launchd job** (`python -m collectors.prompts install`, label `com.foreman.prompts`;
  or `… collect`) tails each project's Claude Code transcripts **forward by byte offset** (resumable,
  ≤25 MB/pass — invariant 4) and appends your human prompts to a **local, out-of-repo store** at
  `~/.foreman/prompts.db` (`FOREMAN_PROMPT_STORE`). **Never** the committed index or the hosted
  board — prompt text stays on the machine (invariant 3); the store is a cache, rebuildable. It
  skips foreman's own headless prompts (cadence runs, the `Report as JSON only …` drift probe).
  `redact.py` masks before anything is stored/shown: **gitleaks-aligned** vendor secret patterns
  (AWS/GCP/GitHub/GitLab/Slack/Stripe/npm/JWT/private-key/OpenAI/Anthropic) + a **Shannon-entropy**
  fallback for unknown high-entropy tokens (tuned not to mask prose/paths). **Microsoft Presidio is
  OPTIONAL**: `redact.full()` adds PERSON/PHONE/SSN/… masking if `presidio-analyzer` is installed,
  else degrades to the regex path; the collector uses `full()`, the live roster the fast `secrets()`.
  Transcripts are located by `encode_cwd(worktree)` under `FOREMAN_TRANSCRIPTS_ROOT`
  (default `~/.claude/projects`).
- **Scheduler** (`collectors/scheduler.py`): launchd agent, `tick` in auto/dispatch/launch modes,
  per-project `autorun`, and **per-project frequency presets** — the named `weekdays`/`weekly`/
  `monthly` (cron) plus **`every-<N>d`** intervals ("every N days", default 1). Cron can't express
  "every N days" (day-of-month `*/N` resets each month), so an interval is scheduled off a fixed
  anchor and carried as an `@every Nd H:M` token; `previous_fire`/`next_fire` dispatch on
  cron-vs-interval so `due` and the UI handle both. `set_loop_schedule` accepts `every-<N>d`; the
  Loops UI has an "every [N] days" number input + the named dropdown. Presets override a loop's
  cadence schedule, gated on auto-run (a preset fires unattended). `effective_schedule` /
  `preset_cron` / `interval_days`. Also:
  **per-project run-location override** (`project.tiers: {loop: cloud|local|session}`) via
  `effective_tier` / `set_loop_tier` — lets one project run a shared cloud cadence on its local
  scheduler without flipping the cadence for the fleet; and **time-of-day window** via
  `autorun_window` / `set_autorun_window` (per project or `defaults`), plus `Cron.next_fire`
  (powers the "next fire" column). Registry editors share one comment-preserving
  `_set_project_map_entry` helper.
- **Loops** (`collectors/loops.py`): a catalog (built-in + standard) plus a **library** of custom
  templates (`<foreman>/loops/*.yaml`). CLI: `list`/`enable`/`new`/`create`/`schedule`/`status`.
  `disable(project, loop)` unwires a loop (drops it from `cadences`+`applies_to`, clears its
  schedule/tier overrides; keeps the cadence file). `customize(name)` copies a built-in's spec
  into `loops/<name>.yaml` so it's editable — the library copy then wins (`_spec_for` prefers
  library over `CATALOG`; `catalog()` marks it "built-in (edited)"). Dashboard "Loop library"
  enables/adds/edits/customizes loops (edit/customize open the file in the operator's editor).
- **Assisted registration** (`collectors/discover.py`): `discover scan <base> [--owner] [--apply]`
  registers local git repos matched to their GitHub project by `origin` remote; `discover register
  <path>` for one. Dashboard Discovered section has a scan form + per-row register. Slugs follow the
  repo name and are sanitized to `^[a-z0-9-]+$`.

## Hosted read-only board (Vercel)

`api/index.py` is a WSGI read-only view of the board for Vercel (auto-detected; renders from the
committed `registry.yaml` + `index.db`, `state/` is gitignored/absent there, all action controls
hidden via `web.render(read_only=True)`). It is **token-gated** on `FOREMAN_DASH_TOKEN` (Vercel
project env), fail-closed when unset — Hobby-plan Vercel Authentication does not reliably protect
production. Access with `?token=…` (sets a 30-day cookie), Bearer header, or the `fdash` cookie.
`.vercelignore` keeps receipts/secrets/spool off Vercel. **Shared-key detection**: `foreman
keyscan` compares declared-key *values* across projects locally and writes `data/keyshare.json`
(group membership only — no value/fingerprint/hash), which the board renders as a "shared" badge.

**Hosted verdict fallback:** the `state` branch (receipts) is *not* deployed to Vercel, so a loop
with no committed receipt would render "stale". `web._gather` now falls back to the `run` table in
the committed `index.db` (synthesizing verdict history from `run` rows where no receipt group
exists) so the hosted board shows real green/amber/red instead of all-stale.

**Deploy is git-integrated on `main` and confirmed working** (all recent deploys `● Ready`, verify
with `vercel ls foreman`). Vercel links a commit author via the connected **GitHub** account, so
the repo git author is set (repo-local) to `John@greatfallsventures.com` — the email verified on
GitHub `acme-demo` — and pushes auto-deploy. This is the fix for the old block: commits authored
by an unrecognized email (e.g. `johnamckinley@gmail.com`) get rejected; adding that email to the
*Vercel* account does not help — it must be a verified **GitHub** email. If a deploy ever stalls,
`vercel --prod` from the repo force-deploys HEAD. **Refresh the board** with
`scripts/refresh-hosted.sh` (recollects + keyscans, then commits + pushes → auto-deploy).

## The live instance

The dashboard runs on **the repo itself** (`FOREMAN_DIR=/srv/acme-demo/foreman`), serving the
committed `registry.yaml` (16 projects — the 3 originals + 13 registered from `~`). State is
`./state`, index is `./index.db`.

- **Restart after every app commit** — `http.server` does NOT hot-reload. Use
  `scripts/restart-dashboard.sh` (kills :8787, relaunches detached with the right env). See the
  `restart-app-after-commit` memory.
- The index is a cache; repopulate with the collectors (below). `foreman-test/` is an older,
  now-unused scratch instance.

## Running things

```bash
python -m collectors.validate                                   # gate (registry + cadences)
python -m collectors.collect --state-dir ./state --index ./index.db --host mbp   # git+github snapshot
python -m collectors.collect ... --probe                        # add the (slow) claude -p config probe
python -m collectors.web --port 8787                            # dashboard (FOREMAN_DIR/STATE/INDEX from env)
python -m collectors.loops enable <project> <loop>              # + schedule/new/create/status
python -m collectors.discover scan ~ --owner acme-demo --apply  # register local repos
python -m collectors.prompts collect                            # scan transcripts -> ~/.foreman/prompts.db
python -m collectors.prompts install                            # hourly launchd job for the above
```

`FOREMAN_STATE_DIR`, `FOREMAN_DIR`, `FOREMAN_INDEX`, `FOREMAN_HOST` are read from the environment.
`FOREMAN_PROMPT_STORE` (default `~/.foreman/prompts.db`) and `FOREMAN_TRANSCRIPTS_ROOT` (default
`~/.claude/projects`) locate the local prompt history; the test suite points both at temp dirs.

**`collect` performance:** the per-project `claude -p` probe is **off by default** (it spawns a
headless Claude per project — minutes + token spend); opt in with `--probe`. `git_state`'s
largest-blob scan is capped at 12s (`--unordered`), so a big-history repo can't stall the run. A
full 16-project collect is ~1 min.

## Tests

`tests/` is a committed pytest suite — **303 tests, ~86% line coverage** (`python -m pytest tests/`,
part of the pre-commit ritual in CLAUDE.md; `python -m coverage run --source=collectors -m pytest
tests/ && coverage report` for the breakdown). Modules include: `test_contract` +
`test_build_receipt` (validate + receipt build/verdict/verifier, in-process — note the shell
`session_end.sh` path is a subprocess coverage.py can't attribute), `test_index` (C1–C7 + lock),
`test_supervisor` (brief/loops/scheduler/registration/web-render), `test_scheduler` (cron +
tier/window/schedule setters), `test_web` + `test_web_actions` (render + every mutating POST via
`apply_action`), `test_decisions`, `test_secrets`, `test_escalations`, `test_telemetry`,
`test_discover`, `test_collect`, `test_scheduler_surfaces` (keyscan/dispatch/runstate),
`test_integration` (end-to-end through `collect.run_all`), and (2026-09-14) `test_recent` +
`test_prompts` (transcript tailing, per-project roster/timeline, search + week counts, offset
resume) and `test_redact` (gitleaks-aligned patterns, entropy, no over-masking of prose/paths).
`conftest.py` holds the fixtures (`foreman_dir`, `state_dir`, `make_receipt`, `git_repo`,
`fake_github`, …) and an autouse fixture that points `FOREMAN_TRANSCRIPTS_ROOT` +
`FOREMAN_PROMPT_STORE` at temp dirs so the suite never reads the real `~/.claude` or `~/.foreman`.
Coverage went 73%→84% / 121→211 tests (2026-09-09), 276 (2026-09-10), then **303 / ~86%**
(2026-09-14: the folding IA / legend / peeks / open-on-signal, the "Recently worked on"
roster+timeline+search, the hourly prompt collector + redaction, and the "every N days" scheduler).
The low remainder is host-deploy utilities (`promote`/`init`/`retention`) and blocking service loops
(`telemetry.serve`).

## Reviews (this session)

Two adversarial UI/UX reviews (dashboard + full-app) ran via the Workflow tool; ~65 verified
findings. Landed fixes: legible action failures (red banner), Schedule cell double-escape, the
frequency-preset CLI + docs, register-a-project docs, `/brief` orphan-project fold, confirm/tooltip
wording. Remaining lower-severity findings are catalogued in the review outputs, not yet all done.

## Docs & deck

- `README.md`, `docs/ONE-PAGER.md`, `docs/COMPARISON.md`, `docs/DECK.md` (Marp source) are the
  narrative/positioning set. `scripts/build_docs_pdf.py` renders the markdown docs to PDF
  (markdown→HTML→Chrome `--print-to-pdf`, sentrygw's approach); `scripts/build_deck.py` builds
  `docs/Foreman_Deck.{pdf,pptx}` (python-pptx + Chrome PDF, `SLIDES` list) embedding the
  `docs/assets/deck-0{1..4}.png` dashboard screenshots.
- **Screenshots** are captured from the live local board via Playwright at 1180×1500 (clear the
  page's auto-refresh timers first, then expand the target accordion — e.g. paysvc in the
  `data-acc="repo"` namespace to show the suggestions block — and scroll it to top). Rerun
  `build_deck.py` after re-shooting. Caveat: they show the **real** portfolio; reshoot against a
  demo before any external sharing.

## Remaining / honest gaps

- **Deploy/ops (host-bound):** the receiver is installable (`telemetry install`) but not yet
  running on `mini`; `config/telemetry.env` not yet pushed to hosts; cloud routine (M1 task 5)
  still unwired; provision `sops`+`age`; real `FOREMAN_QUOTA_CMD`. These need SSH/creds, not code.
- **Config drift on the live board is thin** until `collect --probe` runs (probe is opt-in for
  speed). Component drift (M8.6) also only populates once the probe runs on each host.
- **M8 complete.** Done: run budgets/stop, backfill, deeper drift, fleet diff, verifier pass,
  one-command install, receiver-as-service, worktree-isolated launch, and the **approval gate**
  (a `writes.open_pr` run enqueues a manual-only `approve_push` decision; Approve in the
  brief/dashboard runs `git push` + `gh pr create`, Reject dismisses). The real push path needs
  a live remote + `gh` auth on the host — the mechanism is built and unit-tested with the remote
  stubbed; wire `push_branch` against a real repo to exercise end-to-end.
- **DRIFT leads the CLI brief** (M8.6); on the dashboard it now carries an explainer + green empty
  state but still sits mid-page (Queued is the promoted-to-#2 actionable section). Fine as-is.
- Dashboard writes (`registry.yaml`, new cadence/prompt files, `loops/*.yaml` customizations) are
  **not auto-committed** by the app — but the **post-commit hook** (`scripts/install-hooks.sh`)
  pushes to cloud + restarts local on every commit you make (see `foreman-sync-local-cloud` memory).
- `git_state.force_pushes_7d` is best-effort from the local reflog only.
- **Prompt-history PII masking is secrets-only** until `presidio-analyzer` is `pip install`ed:
  `redact.full()` degrades to the gitleaks-aligned regex + entropy path, so names/addresses/phones
  in prompts are NOT masked (only credentials/emails/high-entropy tokens). Install Presidio to close
  that. The hourly collector (`com.foreman.prompts`) is installed on **mbp** only.
- **`intuitive-ux` loop is built but not yet run** — it's a report-only first-use UX review of the
  dashboard; execute it to get ranked findings and act on them.

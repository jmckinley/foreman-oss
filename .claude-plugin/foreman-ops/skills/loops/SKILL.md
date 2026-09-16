---
name: loops
description: List Foreman's standard review loops and one-click enable one for a project. Use when the operator wants to add a recurring review (documentation, code/arch, security, beta-readiness, performance/scalability, production-readiness, or test-harness) to a project, asks "what loops are available", "enable security review on X", or "turn on the perf loop". Materializes the cadence + prompt and wires the registry.
---

# /loops

Foreman ships a catalog of standard review loops (SPEC.md section 6, `collectors/loops.py`).
`/loops` lists them and enables one for a project in a single step.

## Prerequisite: the project must be registered

Every `loops` command takes a `<project>` that must already exist in `registry.yaml` under
`projects:` (otherwise you get `error: unknown project '<slug>'`). Foreman does not auto-add
projects. Register them from where they live on disk:

```bash
python -m collectors.discover scan ~ --apply           # register every git repo under ~,
python -m collectors.discover scan ~ --owner acme-demo --apply   # ...or just one GitHub owner
python -m collectors.discover register <path>          # register a single directory
```

Each dir is matched to its GitHub project by its `origin` remote (so the slug follows the repo,
not the folder name). Then run `python -m collectors.validate` and commit `registry.yaml`.

## The catalog

Built-in: `docs-sync`, `quality-review`, `beta-readiness`, `harness-refresh`.
Standard (materialized on demand): `arch-review`, `security-review`, `perf-review`,
`prod-readiness`, `test-review`, `pen-test`.
Library: your own reusable templates in `$FOREMAN_DIR/loops/*.yaml` (see "Build your own").

## Run it

List the catalog and which projects have each enabled:

```bash
python -m collectors.loops list --foreman-dir "$FOREMAN_DIR"
```

One-click enable a loop for a project:

```bash
python -m collectors.loops enable <project> <loop> --foreman-dir "$FOREMAN_DIR"
```

Show each enabled loop's last-run time and verdict for a project:

```bash
python -m collectors.loops status <project> --foreman-dir "$FOREMAN_DIR" --index "$FOREMAN_INDEX"
```

Enabling a standard loop writes `cadences/<loop>.yaml` + `prompts/<loop>.md` (scoped to the
project) and adds the loop to the project's `registry.cadences`. Enabling a loop that already
exists just adds the project to its `applies_to`. The result is an ordinary cadence.

## Set how often a loop runs (per project)

By default a loop uses its cadence's own schedule, shared by every project it applies to.
Override the frequency for one project with a preset:

```bash
python -m collectors.loops schedule <project> <loop> <daily|weekdays|weekly|monthly> \
  --foreman-dir "$FOREMAN_DIR"
python -m collectors.loops schedule <project> <loop> default --foreman-dir "$FOREMAN_DIR"  # clear
```

A preset **fires the loop unattended**, so it only applies when the project is on **auto-run**
(`registry.autorun: true`, toggled in the dashboard's Automation section). Setting a preset on
a queue-mode project errors (`enable auto-run before scheduling a loop preset`); a project
flipped back to queue ignores its presets and reverts to the cadence default. Presets map to
canonical, jitter-safe crons. The dashboard's **Schedule** section is the point-and-click
equivalent.

## Build your own loops (a tailored library)

Scaffold a reusable template into `$FOREMAN_DIR/loops/<name>.yaml`, then edit its
metrics/verdict/measure before enabling it anywhere:

```bash
python -m collectors.loops new <name> --metrics "a,b,c" --title "..." --foreman-dir "$FOREMAN_DIR"
python -m collectors.loops create <name> <project> --metrics "a,b" --foreman-dir "$FOREMAN_DIR"  # new + enable
```

Library loops enable exactly like built-ins and show as `kind: library` in `loops list`.

## After enabling

- Run `python -m collectors.validate` to confirm the new cadence is well-formed (it is by
  construction, but validate is the gate).
- Commit `registry.yaml`, the new `cadences/<loop>.yaml`, and `prompts/<loop>.md`.
- The loop then runs on its schedule (or via `/dispatch <project> <loop>` off-cycle). Its
  first red opens a GitHub issue; a green run closes it.

## Notes

- The generated prompt names exactly what to measure for that review; tune it per project if
  the defaults don't fit. The metric keys must keep matching the cadence `metrics` list.

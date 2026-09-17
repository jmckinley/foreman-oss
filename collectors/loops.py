"""Standard loops -- a catalog of ready-made cadences, one-click enabled per project.

The recurring reviews every project wants (docs, code/arch, security, beta, performance,
production readiness) are defined once here as a catalog. ``enable`` materialises a loop for a
project: it writes the cadence + prompt (if not already present) and wires the project into
``registry.yaml`` and the cadence's ``applies_to``. The result is an ordinary cadence that
validates and runs like any other -- the catalog is just the one-click on-ramp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

# Each entry generates a valid cloud cadence + prompt. metrics/verdict are the contract;
# the prompt tells the run what to measure. Schedules avoid :00/:30 and are weekly.
CATALOG: dict[str, dict] = {
    "docs-sync": {"title": "Documentation and user guide drift", "builtin": True},
    "quality-review": {"title": "Code quality and review debt", "builtin": True},
    "beta-readiness": {"title": "Beta readiness blockers", "builtin": True},
    "harness-refresh": {"title": "Claude Code harness drift", "builtin": True},
    "arch-review": {
        "title": "Architecture and boundaries review", "schedule": "43 8 * * 2",
        "metrics": ["boundary_violations", "cyclic_deps", "files_over_500loc"],
        "green": "boundary_violations == 0 and cyclic_deps == 0 and files_over_500loc < 5",
        "amber": "boundary_violations < 5",
        "measure": ["**`boundary_violations`** (integer): imports that cross a bounded-context "
                    "boundary the wrong way.",
                    "**`cyclic_deps`** (integer): import cycles between modules or packages.",
                    "**`files_over_500loc`** (integer): source files over the 500-line ceiling."],
        "example_action": "Break the cycle between billing and orders; extract a shared port"},
    "security-review": {
        "title": "Security review", "schedule": "31 7 * * 4",
        "metrics": ["high_severity_findings", "secrets_exposed", "deps_with_known_cves"],
        "green": "high_severity_findings == 0 and secrets_exposed == 0 and deps_with_known_cves == 0",
        "amber": "high_severity_findings < 3",
        "measure": ["**`high_severity_findings`** (integer): open high/critical security findings.",
                    "**`secrets_exposed`** (integer): secrets found in code, history, or config.",
                    "**`deps_with_known_cves`** (integer): dependencies with known CVEs."],
        "example_action": "Rotate the leaked token in commit a1b2c3 and purge it from history"},
    "soc2-readiness": {
        "title": "SOC 2 readiness", "schedule": "33 9 * * 3",
        "metrics": ["committed_secrets", "tracked_env_files", "unresolved_security_alerts"],
        "green": "committed_secrets == 0 and tracked_env_files == 0 and unresolved_security_alerts == 0",
        "amber": "committed_secrets == 0 and tracked_env_files == 0",
        "measure": ["**`committed_secrets`** (integer): git-tracked files matching a high-signal "
                    "secret pattern (private keys, AWS/GitHub/Slack tokens, provider API keys).",
                    "**`tracked_env_files`** (integer): dotenv files committed to git (.env, "
                    ".env.*); these must be gitignored, not tracked.",
                    "**`unresolved_security_alerts`** (integer): open Dependabot/code-scanning "
                    "alerts for the repo, or 0 if the API is unavailable (say so in next_action)."],
        "example_action": "Enable Dependabot alerts and remove any committed secret or .env file"},
    "ui-ux-review": {
        "title": "UI/UX review", "schedule": "27 9 * * 4",
        "metrics": ["high_severity_findings", "intuitiveness_score", "visual_polish_score"],
        "green": "high_severity_findings == 0 and intuitiveness_score >= 80 and visual_polish_score >= 80",
        "amber": "high_severity_findings < 3",
        "measure": ["**`high_severity_findings`** (integer): usability issues that block or badly "
                    "degrade a first-time user — unclear affordances (e.g. a control you must "
                    "hover to understand), dead-end/empty states, confusing controls, flat or "
                    "unreadable information hierarchy, unlabelled iconography.",
                    "**`intuitiveness_score`** (number 0-100): how well the UI explains itself "
                    "WITHOUT tooltips or docs — can a first-time user act correctly on sight.",
                    "**`visual_polish_score`** (number 0-100): visual quality — layout, spacing, "
                    "typography, colour, consistency, modern/professional feel."],
        "example_action": "Replace the bare hover-only '-' cell with a labelled, self-explanatory "
                          "empty state"},
    "intuitive-ux": {
        "title": "First-use intuitiveness review", "schedule": "41 10 * * 1",
        "metrics": ["task_completion_failures", "friction_points", "intuitiveness_score"],
        "green": "task_completion_failures == 0 and friction_points < 3 and intuitiveness_score >= 85",
        "amber": "task_completion_failures < 2 and friction_points < 8",
        "measure": ["**`task_completion_failures`** (integer): core jobs-to-be-done a brand-new "
                    "operator CANNOT complete unaided, or completes wrongly — catch up on what "
                    "changed, triage what needs them, run/approve a queued item, enable & schedule "
                    "a loop, read a loop's verdict/next-fire/where-it-runs, act on repo health, "
                    "manage a secret. The primary signal.",
                    "**`friction_points`** (integer): distinct points of confusion or friction "
                    "across the walkthrough — a guessed label, a hidden action, an ambiguous "
                    "state, an unexplained internal term (\"tier\", \"cadence\").",
                    "**`intuitiveness_score`** (number 0-100): how much of the UI a first-time "
                    "user understands and acts on correctly ON SIGHT — no tooltip, doc, or "
                    "trial-and-error."],
        "example_action": "Label the queued dispatch's ▶ button 'Run now' and add a one-line "
                          "'what this does' caption so a newcomer acts without guessing"},
    "perf-review": {
        "title": "Performance and scalability review", "schedule": "19 9 * * 3",
        "metrics": ["hot_paths_unbounded", "p95_regressions", "load_headroom_pct"],
        "green": "hot_paths_unbounded == 0 and load_headroom_pct >= 40 and p95_regressions == 0",
        "amber": "hot_paths_unbounded < 3",
        "measure": ["**`hot_paths_unbounded`** (integer): hot paths with unbounded work "
                    "(N+1 queries, unpaged scans, unbounded fan-out).",
                    "**`p95_regressions`** (integer): endpoints whose p95 latency regressed "
                    "beyond the budget since the last run.",
                    "**`load_headroom_pct`** (number): estimated headroom to the next scale "
                    "limit, 0-100."],
        "example_action": "Page the /search scan and add an index; it is the top unbounded path"},
    "prod-readiness": {
        "title": "Production readiness review", "schedule": "37 10 * * 5",
        "metrics": ["slo_gaps", "runbooks_missing", "unmonitored_services"],
        "green": "slo_gaps == 0 and unmonitored_services == 0 and runbooks_missing < 3",
        "amber": "slo_gaps < 3",
        "measure": ["**`slo_gaps`** (integer): user-facing flows with no defined SLO or alert.",
                    "**`runbooks_missing`** (integer): on-call scenarios without a runbook.",
                    "**`unmonitored_services`** (integer): deployed services with no health "
                    "signal in the dashboard."],
        "example_action": "Add an SLO + alert for the checkout flow before the launch gate"},
    "pen-test": {
        "title": "Penetration test (authorized, own project)", "schedule": "47 12 * * 5",
        "metrics": ["exploitable_findings", "unauthenticated_exposures", "attack_surface_uncovered"],
        "green": "exploitable_findings == 0 and unauthenticated_exposures == 0",
        "amber": "exploitable_findings < 2",
        "measure": ["**`exploitable_findings`** (integer): confirmed exploitable issues in THIS "
                    "project (auth bypass, injection, SSRF, IDOR, deserialization, etc.), each "
                    "verified non-destructively.",
                    "**`unauthenticated_exposures`** (integer): endpoints, buckets, or services "
                    "reachable without the authentication they should require.",
                    "**`attack_surface_uncovered`** (integer): entry points (routes, params, "
                    "integrations) not yet exercised by this assessment."],
        "example_action": "Fix the IDOR on /api/orders/:id that leaks other tenants' orders",
        "authorized": True},
    "test-review": {
        "title": "Test harness, e2e and test-data review", "schedule": "29 11 * * 1",
        "metrics": ["e2e_flows_uncovered", "coverage_gaps", "flaky_or_stale_fixtures"],
        "green": "e2e_flows_uncovered == 0 and flaky_or_stale_fixtures == 0",
        "amber": "coverage_gaps < 10",
        "measure": ["**`e2e_flows_uncovered`** (integer): critical user/end-to-end flows with "
                    "no e2e test exercising them.",
                    "**`coverage_gaps`** (integer): public modules or functions with no unit "
                    "coverage.",
                    "**`flaky_or_stale_fixtures`** (integer): tests that failed intermittently "
                    "recently, plus test data / simulators / fakes that no longer reflect "
                    "production behaviour and need refreshing."],
        "example_action": "Add an e2e test for checkout and refresh the payment simulator "
                          "fixtures to match the new gateway"},
}


def _cadence_yaml(name: str, spec: dict, project: str) -> str:
    metrics = "\n".join(f"  - {m}" for m in spec["metrics"])
    return (f"slug: {name}\n"
            f"title: {json.dumps(spec['title'])}\n"
            f"tier: cloud\n"
            f"schedule: \"{spec['schedule']}\"\n"
            f"timeout_minutes: 45\n"
            f"concurrency: one-per-project\n"
            f"lock_key: \"{name}:{{project}}\"\n\n"
            f"applies_to: [{project}]\n\n"
            f"connectors: [github]\n\n"
            f"prompt_ref: prompts/{name}.md\n\n"
            f"metrics:\n{metrics}\n\n"
            f"verdict:\n"
            f"  green: \"{spec['green']}\"\n"
            f"  amber: \"{spec['amber']}\"\n"
            f"  red: otherwise\n\n"
            f"escalate_when: red\n"
            f"amber_ages_to_red_after_days: 14\n\n"
            f"writes:\n"
            f"  branch: foreman/{name}\n"
            f"  open_pr: false\n"
            f"  max_files_changed: 1\n")


def _prompt_md(name: str, spec: dict) -> str:
    measure = "\n".join(f"- {m}" for m in spec["measure"])
    keys = ", ".join(spec["metrics"])
    scope = ""
    if spec.get("authorized"):
        scope = ("## Authorized scope\n\n"
                 "This is an **authorized security assessment of this project only** -- the "
                 "operator owns the repo Foreman handed you. Stay in scope: assess and report, "
                 "do not exploit destructively, do not run denial-of-service, do not exfiltrate "
                 "data, and touch nothing outside this project. Verify findings the minimal "
                 "non-destructive way. If a finding cannot be checked safely, report it as "
                 "suspected rather than proving it.\n\n")
    return (f"# Cadence: {name}\n\n"
            f"You are running the **{name}** cadence ({spec['title']}) for one project under "
            f"Foreman. Measure, decide one next action, and report a verdict. Foreman schedules "
            f"and reads; **you do the work** (SPEC.md invariant 6). The `SessionEnd` hook writes "
            f"the receipt envelope; your only reporting duty is the partial in the final step.\n\n"
            f"{scope}"
            f"## Environment\n\n"
            f"Set by the dispatcher. Never hardcode an absolute local path: this runs on the "
            f"**cloud** tier with a fresh clone.\n\n"
            f"- `FOREMAN_RUN_ID`, `FOREMAN_PROJECT`, `FOREMAN_SPOOL`, `FOREMAN_STATE_DIR`.\n\n"
            f"## Step 1 - Measure\n\n"
            f"Compute exactly these metrics; the keys must match the cadence `metrics` list "
            f"(`{keys}`):\n\n{measure}\n\n"
            f"Measure only; `writes.open_pr` is false, so open nothing.\n\n"
            f"## Step 2 - Deltas\n\n"
            f"Find the latest prior receipt under "
            f"`$FOREMAN_STATE_DIR/receipts/$FOREMAN_PROJECT/{name}/` (lexically last) and emit "
            f"one delta per changed metric: "
            f"`{{ \"kind\": \"metric\", \"name\": ..., \"from\": ..., \"to\": ..., \"direction\": ... }}`. "
            f"Lower is better for count metrics; higher is better for headroom/percentage "
            f"metrics. Omit unchanged. No prior receipt -> `[]`.\n\n"
            f"## Step 3 - Next action\n\n"
            f"One imperative sentence naming the single highest-value step (e.g. "
            f"`{spec['example_action']}`).\n\n"
            f"## Step 4 - Write the partial (last thing you do)\n\n"
            f"Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly `metrics`, "
            f"`deltas`, `next_action`. No verdict or status - the hook derives the verdict. If "
            f"you cannot compute the metrics, write no partial: the missing partial is recorded "
            f"as a failed run, not a green.\n")


# ------------------------------------------------------------------- registry edits

def _add_to_project_cadences(text: str, project: str, loop: str) -> str:
    """Add ``loop`` to a project's inline ``cadences: [...]`` list, comment-preserving."""
    lines = text.splitlines(keepends=True)
    in_block = False
    indent = 0
    for i, ln in enumerate(lines):
        body = ln.lstrip().lstrip("-").strip()
        if body.startswith("slug:"):
            # slug value may carry a trailing "# comment" -- strip it before comparing.
            in_block = body.split("slug:", 1)[1].split("#")[0].strip() == project
            indent = len(ln) - len(ln.lstrip(" "))
            continue
        if in_block and ln.lstrip().startswith("cadences:"):
            m = re.match(r"(\s*cadences:\s*\[)([^\]]*)(\].*)", ln)
            if not m:
                return text
            items = [x.strip() for x in m.group(2).split(",") if x.strip()]
            if loop in items:
                return text
            items.append(loop)
            lines[i] = f"{m.group(1)}{', '.join(items)}{m.group(3)}\n"
            return "".join(lines)
        if in_block and ln.strip() and (len(ln) - len(ln.lstrip(" "))) <= indent and ln.lstrip().startswith("-"):
            break  # next project
    return text


def _add_to_applies_to(text: str, project: str) -> str:
    m = re.search(r"(applies_to:\s*\[)([^\]]*)(\])", text)
    if not m:
        return text
    items = [x.strip() for x in m.group(2).split(",") if x.strip()]
    if project in items:
        return text
    items.append(project)
    return text[:m.start()] + f"{m.group(1)}{', '.join(items)}{m.group(3)}" + text[m.end():]


def _remove_from_project_cadences(text: str, project: str, loop: str) -> str:
    """Drop ``loop`` from a project's inline ``cadences: [...]`` list, comment-preserving."""
    lines = text.splitlines(keepends=True)
    in_block = False
    indent = 0
    for i, ln in enumerate(lines):
        body = ln.lstrip().lstrip("-").strip()
        if body.startswith("slug:"):
            in_block = body.split("slug:", 1)[1].split("#")[0].strip() == project
            indent = len(ln) - len(ln.lstrip(" "))
            continue
        if in_block and ln.lstrip().startswith("cadences:"):
            m = re.match(r"(\s*cadences:\s*\[)([^\]]*)(\].*)", ln)
            if not m:
                return text
            items = [x.strip() for x in m.group(2).split(",") if x.strip()]
            if loop not in items:
                return text
            items = [x for x in items if x != loop]
            lines[i] = f"{m.group(1)}{', '.join(items)}{m.group(3)}\n"
            return "".join(lines)
        if in_block and ln.strip() and (len(ln) - len(ln.lstrip(" "))) <= indent and ln.lstrip().startswith("-"):
            break
    return text


def _remove_from_applies_to(text: str, project: str) -> str:
    m = re.search(r"(applies_to:\s*\[)([^\]]*)(\])", text)
    if not m:
        return text
    items = [x.strip() for x in m.group(2).split(",") if x.strip()]
    if project not in items:
        return text
    items = [x for x in items if x != project]
    return text[:m.start()] + f"{m.group(1)}{', '.join(items)}{m.group(3)}" + text[m.end():]


# --------------------------------------------------------------------- operations

def catalog(foreman_dir) -> list[dict]:
    foreman_dir = Path(foreman_dir)
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    enabled_by: dict = {}
    for p in reg.get("projects", []):
        for c in p.get("cadences", []):
            enabled_by.setdefault(c, []).append(p["slug"])
    lib = library_specs(foreman_dir)
    out = []
    for name, spec in CATALOG.items():
        # a library file of the same name is an edit of this built-in -> one row, marked.
        kind = "built-in (edited)" if name in lib else "built-in"
        title = lib[name].get("title", spec["title"]) if name in lib else spec["title"]
        out.append({"loop": name, "title": title, "kind": kind,
                    "enabled_for": sorted(enabled_by.get(name, []))})
    for name, spec in lib.items():
        if name in CATALOG:
            continue  # already listed as a customised built-in above
        out.append({"loop": name, "title": spec.get("title", name), "kind": "library",
                    "enabled_for": sorted(enabled_by.get(name, []))})
    # Hand-authored cadences with no template (not built-in, not in the library).
    cad_dir = foreman_dir / "cadences"
    for cad in sorted(cad_dir.glob("*.yaml")) if cad_dir.is_dir() else []:
        if cad.stem in CATALOG or cad.stem in lib:
            continue
        try:
            title = (yaml.safe_load(cad.read_text()) or {}).get("title", cad.stem)
        except (OSError, yaml.YAMLError):
            title = cad.stem
        out.append({"loop": cad.stem, "title": title, "kind": "custom",
                    "enabled_for": sorted(enabled_by.get(cad.stem, []))})
    return out


LIBRARY_SUBDIR = "loops"


def library_specs(foreman_dir: Path) -> dict[str, dict]:
    """Custom loop templates the operator has built: <foreman>/loops/*.yaml. The tailored
    library — reusable across projects, git-tracked, distinct from the built-in CATALOG."""
    lib = Path(foreman_dir) / LIBRARY_SUBDIR
    out: dict[str, dict] = {}
    if not lib.is_dir():
        return out
    for f in sorted(lib.glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(spec, dict):
            out[f.stem] = spec
    return out


def _spec_for(foreman_dir: Path, name: str) -> dict | None:
    """A loop's template. A library file wins over the built-in of the same name, so a
    *customised* built-in (its spec copied into loops/<name>.yaml and edited) takes precedence."""
    return library_specs(foreman_dir).get(name) or CATALOG.get(name)


def _prompt_scaffold(name: str, metrics: list[str], body: str) -> str:
    """Wrap a user's plain-language review instructions with the reporting contract, so a
    hand-authored loop is immediately runnable."""
    body = (body or "").strip() or ("Describe what this loop reviews: what to inspect, what "
                                    "counts as a problem, and how to compute each metric below.")
    keys = ", ".join(f"`{m}`" for m in metrics)
    metric_json = ", ".join(f'"{m}": <number>' for m in metrics)
    return (f"# {name}\n\n{body}\n\n"
            f"## Report — do this last\n\n"
            f"Write `$FOREMAN_SPOOL/$FOREMAN_RUN_ID.partial.json` with exactly:\n\n"
            f"- **metrics** — the numbers you measured; keys must match the loop's metrics "
            f"({keys}): `{{ {metric_json} }}`\n"
            f"- **next_action** — one imperative sentence: the single highest-value next step.\n"
            f"- **deltas** — `[]`, or one `{{\"kind\":\"metric\",\"name\":...,"
            f"\"direction\":\"better|worse|flat\"}}` per changed metric vs the previous receipt.\n\n"
            f"The SessionEnd hook derives the verdict from this partial; writing no partial is a "
            f"failed (red) run.\n")


_REPORT_MARKER = "## Report — do this last"


def _prompt_body(raw: str) -> str:
    """Reverse ``_prompt_scaffold``: recover the editable instructions from a prompt file, so the
    edit form shows what the author wrote — not the machine-generated reporting contract, which
    ``author`` regenerates from the current metrics on save (avoids a doubled contract)."""
    if not raw:
        return ""
    body = raw.split(_REPORT_MARKER, 1)[0]      # drop the reporting contract (regenerated on save)
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):     # drop the scaffold's "# <name>" title line
        lines = lines[1:]
    return "\n".join(lines).strip()


def author(foreman_dir, name: str, *, title: str = "", metrics=None, green: str = "",
           amber: str = "", prompt: str = "") -> dict:
    """Create or update a loop entirely from the dashboard — no external editor. Writes
    ``loops/<name>.yaml`` (spec: metrics + verdict + title) and ``prompts/<name>.md`` (the review
    instructions). Editing a built-in this way lands a library override (like Customize + edit)."""
    foreman_dir = Path(foreman_dir)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise ValueError(f"loop name {name!r} must be lowercase kebab-case, e.g. my-review")
    metrics = [m.strip() for m in (metrics or []) if m.strip()]
    if not metrics:
        raise ValueError("give at least one metric — the number(s) your review reports")
    green = (green or "").strip() or f"{metrics[0]} == 0"
    amber = (amber or "").strip() or f"{metrics[0]} < 5"
    prev = library_specs(foreman_dir).get(name) or CATALOG.get(name) or {}
    spec = {"title": (title or "").strip() or prev.get("title") or f"Custom loop: {name}",
            "schedule": prev.get("schedule", "33 9 * * 3"),
            "metrics": metrics, "green": green, "amber": amber,
            "measure": prev.get("measure") or [f"**`{m}`** — what this measures." for m in metrics],
            "example_action": prev.get("example_action", "the single highest-value next step")}
    lib = foreman_dir / LIBRARY_SUBDIR
    lib.mkdir(exist_ok=True)
    (lib / f"{name}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    (foreman_dir / "prompts").mkdir(exist_ok=True)
    (foreman_dir / "prompts" / f"{name}.md").write_text(_prompt_scaffold(name, metrics, prompt))
    return {"loop": name, "spec": str(lib / f"{name}.yaml"),
            "prompt": str(foreman_dir / "prompts" / f"{name}.md")}


def editable(foreman_dir, name: str) -> dict:
    """The current authorable state of a loop (title / metrics / verdict / prompt), for pre-filling
    the edit form. Reads the committed cadence's verdict when there is one."""
    foreman_dir = Path(foreman_dir)
    spec = dict(_spec_for(foreman_dir, name) or {})
    green, amber = spec.get("green", ""), spec.get("amber", "")
    cad = foreman_dir / "cadences" / f"{name}.yaml"
    if cad.is_file():
        try:
            c = yaml.safe_load(cad.read_text()) or {}
            v = c.get("verdict") or {}
            green, amber = v.get("green", green), v.get("amber", amber)
            spec.setdefault("title", c.get("title", ""))
            spec.setdefault("metrics", c.get("metrics", []))
        except (OSError, yaml.YAMLError):
            pass
    prompt_path = foreman_dir / "prompts" / f"{name}.md"
    return {"name": name, "title": spec.get("title", ""), "metrics": spec.get("metrics", []),
            "green": green, "amber": amber,
            "prompt": _prompt_body(prompt_path.read_text()) if prompt_path.is_file() else ""}


def customize(foreman_dir, name: str) -> dict:
    """Make a built-in loop editable: copy its catalog spec into loops/<name>.yaml, where it
    overrides the built-in for future materialisation. Idempotent — if the library file already
    exists it's left as-is (so an edit isn't clobbered). Only meaningful for a built-in that
    carries a full spec (metrics/verdict); bare built-ins are defined by their committed cadence
    file, which is edited directly."""
    foreman_dir = Path(foreman_dir)
    spec = CATALOG.get(name)
    if spec is None:
        raise ValueError(f"{name!r} is not a built-in loop")
    if "metrics" not in spec:
        raise ValueError(f"{name!r} has no editable template — edit its cadence file instead")
    lib = foreman_dir / LIBRARY_SUBDIR
    lib.mkdir(exist_ok=True)
    path = lib / f"{name}.yaml"
    if not path.is_file():
        body = {k: v for k, v in spec.items() if k != "builtin"}
        path.write_text(yaml.safe_dump(body, sort_keys=False))
    return {"loop": name, "template": str(path), "created": True}


def new(foreman_dir, name: str, *, metrics: list[str] | None = None,
        schedule: str = "33 9 * * 3", title: str | None = None) -> dict:
    """Add a reusable loop template to the library (not yet enabled anywhere). Edit the
    generated file to define the metrics/verdict, then `enable` it for any project."""
    foreman_dir = Path(foreman_dir)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"loop name {name!r} must be lowercase kebab-case")
    if name in CATALOG or name in library_specs(foreman_dir):
        raise ValueError(f"{name!r} already exists (built-in or in the library)")
    metrics = metrics or ["finding_count", "coverage_pct", "notes"]
    spec = {"title": title or f"Custom loop: {name}", "schedule": schedule, "metrics": metrics,
            "green": f"{metrics[0]} == 0", "amber": f"{metrics[0]} < 5",
            "measure": [f"**`{m}`** (integer/number): TODO describe what to measure." for m in metrics],
            "example_action": "TODO the single highest-value next step"}
    lib = foreman_dir / LIBRARY_SUBDIR
    lib.mkdir(exist_ok=True)
    path = lib / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False))
    return {"loop": name, "template": str(path),
            "next": f"edit {path} (metrics, verdict, measure), then loops enable <project> {name}"}


def create(foreman_dir, name: str, project: str, **kw) -> dict:
    """Add a library template and enable it for a project in one step."""
    t = new(foreman_dir, name, **kw)
    return {**t, **enable(foreman_dir, project, name)}


def recent_verdicts(conn, limit: int = 12) -> dict:
    """{(project, cadence): [verdict, …]} oldest→newest, last ``limit`` real runs each — for a
    verdict-trend sparkline. Receipt-derived (run table), so nothing new is stored."""
    out: dict = {}
    for r in conn.execute(
            "SELECT project, cadence, verdict, status FROM run "
            "WHERE status NOT IN ('locked','skipped') AND ended IS NOT NULL ORDER BY ended"):
        # a failed/timeout run with no verdict reads as red on the trend
        v = r["verdict"] or ("red" if r["status"] in ("failed", "timeout") else r["status"])
        out.setdefault((r["project"], r["cadence"]), []).append(v)
    return {k: v[-limit:] for k, v in out.items()}


def last_runs(conn) -> dict:
    """Latest real run per (project, cadence) from the index: {(project, cadence): row}.

    Derived from the run table (which mirrors receipts), so nothing new is stored -- the
    index stays a cache. Locked/skipped coordination runs are excluded.
    """
    out: dict = {}
    for r in conn.execute(
            "SELECT project, cadence, ended, verdict, status FROM run "
            "WHERE status NOT IN ('locked','skipped') AND ended IS NOT NULL ORDER BY ended"):
        out[(r["project"], r["cadence"])] = {"ended": r["ended"], "verdict": r["verdict"],
                                             "status": r["status"]}
    return out


def status(foreman_dir, project: str, conn) -> list[dict]:
    """Each loop enabled for a project with its last-run time and verdict (or never run)."""
    foreman_dir = Path(foreman_dir)
    reg = yaml.safe_load((foreman_dir / "registry.yaml").read_text())
    proj = next((p for p in reg.get("projects", []) if p["slug"] == project), None)
    if proj is None:
        raise ValueError(f"unknown project {project!r}")
    lr = last_runs(conn)
    out = []
    for loop in proj.get("cadences", []):
        info = lr.get((project, loop))
        out.append({"loop": loop,
                    "last_run": info["ended"] if info else None,
                    "verdict": info["verdict"] if info else None,
                    "status": info["status"] if info else "never run"})
    return out


def enable(foreman_dir, project: str, loop: str) -> dict:
    """One-click: materialise the loop (built-in or library) if new, and wire the project."""
    foreman_dir = Path(foreman_dir)
    reg_path = foreman_dir / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text())
    if not any(p["slug"] == project for p in reg.get("projects", [])):
        raise ValueError(f"unknown project {project!r}")

    cad_path = foreman_dir / "cadences" / f"{loop}.yaml"
    created = False
    if cad_path.is_file():
        cad_path.write_text(_add_to_applies_to(cad_path.read_text(), project))
    else:
        spec = _spec_for(foreman_dir, loop)
        if spec is None:
            raise ValueError(f"unknown loop {loop!r}; see the catalog")
        if spec.get("builtin"):
            raise ValueError(f"{loop!r} is a built-in cadence but its file is missing")
        cad_path.write_text(_cadence_yaml(loop, spec, project))
        (foreman_dir / "prompts" / f"{loop}.md").write_text(_prompt_md(loop, spec))
        created = True

    reg_path.write_text(_add_to_project_cadences(reg_path.read_text(), project, loop))
    return {"loop": loop, "project": project, "created_cadence": created,
            "cadence": str(cad_path)}


def disable(foreman_dir, project: str, loop: str) -> dict:
    """Inverse of :func:`enable`: unwire a loop from a project. Removes it from the project's
    ``cadences`` list and from the cadence file's ``applies_to``, and clears any per-loop
    ``schedules``/``tiers`` override for it. Idempotent. Never deletes the cadence file itself
    (other projects may still use it, and files are cheap to keep — invariant 1)."""
    from collectors import scheduler        # local import: scheduler imports loops-adjacent bits
    foreman_dir = Path(foreman_dir)
    reg_path = foreman_dir / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text())
    proj = next((p for p in reg.get("projects", []) if p["slug"] == project), None)
    if proj is None:
        raise ValueError(f"unknown project {project!r}")
    if loop not in (proj.get("cadences") or []):
        return {"loop": loop, "project": project, "removed": False}

    reg_path.write_text(_remove_from_project_cadences(reg_path.read_text(), project, loop))
    cad_path = foreman_dir / "cadences" / f"{loop}.yaml"
    if cad_path.is_file():
        cad_path.write_text(_remove_from_applies_to(cad_path.read_text(), project))
    # Drop now-orphaned overrides so they don't linger and fail validation of the cadences list.
    scheduler.set_loop_schedule(foreman_dir, project, loop, "default")
    scheduler.set_loop_tier(foreman_dir, project, loop, "default")
    return {"loop": loop, "project": project, "removed": True, "cadence": str(cad_path)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(prog="loops")
    ap.add_argument("--foreman-dir", default=os.environ.get("FOREMAN_DIR", "."))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    pe = sub.add_parser("enable")
    pe.add_argument("project")
    pe.add_argument("loop")
    pn = sub.add_parser("new", help="add a reusable custom loop template to the library")
    pn.add_argument("name")
    pn.add_argument("--metrics", help="comma-separated metric names")
    pn.add_argument("--title")
    pn.add_argument("--schedule", default="33 9 * * 3")
    pc = sub.add_parser("create", help="new library template + enable it for a project")
    pc.add_argument("name")
    pc.add_argument("project")
    pc.add_argument("--metrics")
    pc.add_argument("--title")
    psc = sub.add_parser("schedule", help="set a loop's frequency for a project (needs auto-run)")
    psc.add_argument("project")
    psc.add_argument("loop")
    psc.add_argument("preset", choices=["daily", "weekdays", "weekly", "monthly", "default"],
                     help="daily|weekdays|weekly|monthly, or 'default' to clear the override")
    pst = sub.add_parser("status")
    pst.add_argument("project")
    pst.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    args = ap.parse_args(argv)

    try:
        return _run(args)
    except ValueError as exc:               # business error -> clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(args) -> int:
    import json
    import sys

    if args.cmd == "list":
        for e in catalog(args.foreman_dir):
            on = f"  enabled: {', '.join(e['enabled_for'])}" if e["enabled_for"] else ""
            print(f"  {e['loop']:<16} {e['kind']:<9} {e['title']}{on}")
        return 0
    if args.cmd == "enable":
        print(json.dumps(enable(args.foreman_dir, args.project, args.loop)))
        return 0
    if args.cmd in ("new", "create"):
        metrics = [m.strip() for m in args.metrics.split(",")] if args.metrics else None
        if args.cmd == "new":
            print(json.dumps(new(args.foreman_dir, args.name, metrics=metrics,
                                 schedule=args.schedule, title=args.title), indent=2))
        else:
            print(json.dumps(create(args.foreman_dir, args.name, args.project,
                                    metrics=metrics, title=args.title), indent=2))
        return 0
    if args.cmd == "schedule":
        from collectors import scheduler
        scheduler.set_loop_schedule(Path(args.foreman_dir), args.project, args.loop, args.preset)
        eff = "cadence default" if args.preset == "default" else args.preset
        print(json.dumps({"project": args.project, "loop": args.loop, "frequency": eff}))
        return 0
    if args.cmd == "status":
        from collectors import db
        index = args.index or str(db.default_path())
        if not Path(index).exists():
            print(f"note: index {index} not built yet — 'last run' is unknown, not 'never run'. "
                  f"Run `python -m collectors.collect` first.", file=sys.stderr)
        conn = db.open_index(index)
        for s in status(args.foreman_dir, args.project, conn):
            when = s["last_run"] or "never run"
            verdict = f" [{s['verdict']}]" if s["verdict"] else ""
            print(f"  {s['loop']:<16} last run: {when}{verdict}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

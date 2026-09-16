"""Validate the registry and every cadence against the schemas and the tier constraints.

Run as:

    python -m collectors.validate

Exit 0 when everything is consistent, 1 otherwise. This is the gate named in
``CLAUDE.md`` under "Validation before commit" and in ``BUILD.md`` M1 task 1.

Two layers of checking:

1. JSON Schema validation of ``registry.yaml`` and each ``cadences/*.yaml`` against
   ``schema/registry.schema.json`` and ``schema/cadence.schema.json``.
2. The tier constraints from SPEC.md section 6, plus referential integrity that no
   schema can express (a cadence naming a project that does not exist, a prompt file
   that is not on disk, a cloud prompt that hardcodes an absolute local path).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent

# Minimum spacing between two consecutive fires, per tier, from SPEC.md section 6.
TIER_MIN_INTERVAL_MINUTES = {"cloud": 60, "local": 1, "session": 1}

# Absolute local paths a cloud prompt must never contain: a cloud run has no local
# filesystem and a fresh clone per run (SPEC.md sections 3 and 6). Env-var-rooted
# paths ($FOREMAN_SPOOL/...) and repo-relative paths are fine; filesystem roots are not.
ABS_PATH_RE = re.compile(
    r"""(?<![\w$])/(?:Users|home|srv|opt|var|tmp|etc|mnt|private|root)/\S+"""
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- cron

def _expand_field(spec: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field (``*``, ``a``, ``a,b``, ``a-b``, ``*/n``, ``a-b/n``)."""
    values: set[int] = set()
    for part in spec.split(","):
        step = 1
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"non-positive step in {part!r}")
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            start_s, end_s = body.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(body)
        if start < lo or end > hi or start > end:
            raise ValueError(f"field {part!r} out of range [{lo},{hi}]")
        values.update(range(start, end + 1, step))
    return values


@dataclass
class Cron:
    minute: set[int]
    hour: set[int]
    dom: set[int]
    month: set[int]
    dow: set[int]
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"expected 5 cron fields, got {len(fields)}")
        m, h, dom, mon, dow = fields
        dow_set = {0 if v == 7 else v for v in _expand_field(dow, 0, 7)}
        return cls(
            minute=_expand_field(m, 0, 59),
            hour=_expand_field(h, 0, 23),
            dom=_expand_field(dom, 1, 31),
            month=_expand_field(mon, 1, 12),
            dow=dow_set,
            dom_restricted=dom.strip() != "*",
            dow_restricted=dow.strip() != "*",
        )

    def matches(self, when: dt.datetime) -> bool:
        if when.minute not in self.minute:
            return False
        if when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        # cron day-of-week: Sunday is 0; python weekday() has Monday 0, Sunday 6.
        cron_dow = (when.weekday() + 1) % 7
        if self.dom_restricted and self.dow_restricted:
            # Both restricted -> OR (standard Vixie cron behaviour).
            return when.day in self.dom or cron_dow in self.dow
        if self.dom_restricted:
            return when.day in self.dom
        if self.dow_restricted:
            return cron_dow in self.dow
        return True

    def _gaps(self, window_days: int) -> list[int]:
        """Minutes between consecutive fires over a fixed, deterministic window.

        The window starts on a fixed epoch so results are reproducible. Forty days
        captures sub-hourly, hourly, daily and weekly patterns.
        """
        start = dt.datetime(2025, 1, 1, 0, 0)  # a Wednesday
        prev: dt.datetime | None = None
        gaps: list[int] = []
        cur = start
        end = start + dt.timedelta(days=window_days)
        while cur < end:
            if self.matches(cur):
                if prev is not None:
                    gaps.append(int((cur - prev).total_seconds() // 60))
                prev = cur
            cur += dt.timedelta(minutes=1)
        return gaps

    def min_interval_minutes(self, window_days: int = 40) -> int:
        """Smallest gap between consecutive fires; the tier floor is checked against this."""
        gaps = self._gaps(window_days)
        return min(gaps) if gaps else window_days * 24 * 60

    def fires_between(self, since: dt.datetime, now: dt.datetime) -> bool:
        """True if the schedule matches any minute in (since, now]. Interpreted in whatever
        (local) clock the caller passes. Lookback is capped at 2 days so a long-idle
        scheduler still catches recent fires without scanning forever."""
        floor = now - dt.timedelta(days=2)
        start = max(since, floor).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        end = now.replace(second=0, microsecond=0)
        cur = start
        while cur <= end:
            if self.matches(cur):
                return True
            cur += dt.timedelta(minutes=1)
        return False

    def previous_fire(self, now: dt.datetime, *, horizon_days: int = 45) -> dt.datetime | None:
        """The most recent scheduled minute at or before ``now``, or None if none within the
        horizon. Interpreted in the caller's (local) clock.

        This is what makes catch-up scheduling fire the *most recent* missed occurrence and
        only that one: after downtime the scheduler asks for the last scheduled time, not
        every time it slept through. The horizon (45 days) covers daily/weekly/monthly
        cadences with margin; a sparser cadence falls back to receipt-absence staleness.
        """
        cur = now.replace(second=0, microsecond=0)
        floor = cur - dt.timedelta(days=horizon_days)
        while cur >= floor:
            if self.matches(cur):
                return cur
            cur -= dt.timedelta(minutes=1)
        return None

    def next_fire(self, now: dt.datetime, *, horizon_days: int = 45) -> dt.datetime | None:
        """The next scheduled minute strictly after ``now``, or None if none within the horizon.
        Interpreted in the caller's (local) clock. The forward twin of ``previous_fire`` — used
        to show operators when an auto-scheduled loop will next run."""
        cur = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        ceil = cur + dt.timedelta(days=horizon_days)
        while cur <= ceil:
            if self.matches(cur):
                return cur
            cur += dt.timedelta(minutes=1)
        return None

    def max_interval_minutes(self, window_days: int = 40) -> int:
        """Largest normal gap between consecutive fires; the staleness period doubles this.

        Using the max gap (not the min) means an irregular schedule -- e.g. Mon and Thu,
        whose Thu->Mon gap is longer than Mon->Thu -- is not called stale during its
        naturally longer quiet stretch.
        """
        gaps = self._gaps(window_days)
        return max(gaps) if gaps else window_days * 24 * 60


# ----------------------------------------------------------------------- loading

def _load_yaml(path: Path):
    with path.open() as fh:
        return yaml.safe_load(fh)


def _load_schema(schema_dir: Path, name: str) -> Draft202012Validator:
    with (schema_dir / name).open() as fh:
        return Draft202012Validator(json.load(fh))


def _schema_errors(validator: Draft202012Validator, doc) -> list[str]:
    out = []
    for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        out.append(f"{loc}: {e.message}")
    return out


# -------------------------------------------------------------------- validation

def validate(root: Path = ROOT) -> Report:
    rep = Report()

    schema_dir = root / "schema"
    cadence_dir = root / "cadences"
    registry_path = root / "registry.yaml"
    registry_schema = _load_schema(schema_dir, "registry.schema.json")
    cadence_schema = _load_schema(schema_dir, "cadence.schema.json")

    try:
        registry = _load_yaml(registry_path)
    except (OSError, yaml.YAMLError) as exc:
        rep.err("registry.yaml", f"could not load: {exc}")
        return rep

    for msg in _schema_errors(registry_schema, registry):
        rep.err("registry.yaml", msg)

    project_slugs = {p["slug"] for p in registry.get("projects", []) if "slug" in p}
    hosts = set(registry.get("hosts", {}).keys())

    # NB: a project may list cadences whose files do not exist yet. Cadence files
    # land incrementally across milestones (docs-sync in M1, the rest in M6), so a
    # missing file is not an error here. The reverse direction -- a cadence file
    # naming a project that does not exist -- is checked per cadence below.
    for proj in registry.get("projects", []):
        # A worktree must name a host that the registry declares.
        for host in proj.get("worktree", {}):
            if host not in hosts:
                rep.err(
                    f"registry.yaml/projects/{proj.get('slug')}",
                    f"worktree host {host!r} is not declared under hosts",
                )
        # A per-loop schedule override must name a loop the project actually runs.
        proj_cadences = set(proj.get("cadences") or [])
        for loop in (proj.get("schedules") or {}):
            if loop not in proj_cadences:
                rep.err(
                    f"registry.yaml/projects/{proj.get('slug')}",
                    f"schedules override for {loop!r} but it is not in this project's cadences",
                )

    for cad_path in sorted(cadence_dir.glob("*.yaml")):
        _validate_cadence(cad_path, cadence_schema, project_slugs, rep, root)

    return rep


def _validate_cadence(
    path: Path,
    schema: Draft202012Validator,
    project_slugs: set[str],
    rep: Report,
    root: Path,
) -> None:
    where = f"cadences/{path.name}"
    try:
        cad = _load_yaml(path)
    except (OSError, yaml.YAMLError) as exc:
        rep.err(where, f"could not load: {exc}")
        return

    schema_errs = _schema_errors(schema, cad)
    for msg in schema_errs:
        rep.err(where, msg)
    if schema_errs:
        # Structural problems make the semantic checks below unreliable.
        return

    # Slug must match the filename so /dispatch and receipt paths line up.
    if cad["slug"] != path.stem:
        rep.err(where, f"slug {cad['slug']!r} does not match filename {path.stem!r}")

    tier = cad["tier"]

    # applies_to must reference real projects.
    for slug in cad["applies_to"]:
        if slug not in project_slugs:
            rep.err(where, f"applies_to references unknown project {slug!r}")

    # --- schedule / cron -----------------------------------------------------
    try:
        cron = Cron.parse(cad["schedule"])
    except ValueError as exc:
        rep.err(where, f"schedule {cad['schedule']!r}: {exc}")
        cron = None

    if cron is not None:
        # Convention (SPEC.md section 6): avoid :00 and :30 minute fields.
        if 0 in cron.minute or 30 in cron.minute:
            rep.err(where, "schedule minute field must avoid :00 and :30 (one-shot jitter)")
        floor = TIER_MIN_INTERVAL_MINUTES[tier]
        gap = cron.min_interval_minutes()
        if gap < floor:
            rep.err(
                where,
                f"tier {tier!r} requires >= {floor} min between fires; "
                f"schedule fires every {gap} min",
            )

    # --- tier constraints (SPEC.md section 6) --------------------------------
    if tier == "cloud":
        if "allowed_hosts" in cad:
            rep.err(where, "cloud tier must not set 'allowed_hosts'")
        _check_cloud_prompt(cad, rep, root)
    elif tier == "session":
        if "escalate_when" in cad:
            rep.err(where, "session tier must not set 'escalate_when' (nothing durable runs)")

    if tier != "cloud" and "connectors" in cad:
        # Connectors are the cloud tier's managed integrations; a local/session run
        # would need a matching local MCP config, which this repo does not model yet.
        rep.err(where, f"tier {tier!r} sets 'connectors'; only cloud connectors are supported")

    # --- prompt reference ----------------------------------------------------
    prompt_path = root / cad["prompt_ref"]
    if not prompt_path.is_file():
        rep.err(where, f"prompt_ref {cad['prompt_ref']!r} does not exist")

    # --- metrics / verdict consistency --------------------------------------
    metric_names = set(cad["metrics"])
    referenced = set()
    for level in ("green", "amber", "red"):
        expr = cad["verdict"][level]
        if expr.strip() == "otherwise":
            continue
        referenced.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
    # A verdict expression may contain only declared metric names, boolean operators,
    # and literals. Any other identifier is a typo that would crash build_receipt at run
    # time (it raises on an unknown name), so catch it here.
    keywords = {"and", "or", "not", "True", "False", "true", "false", "None"}
    unknown = (referenced - keywords) - metric_names
    for name in sorted(unknown):
        rep.err(where, f"verdict references undeclared metric {name!r}")


def _check_cloud_prompt(cad, rep: Report, root: Path) -> None:
    prompt_path = root / cad.get("prompt_ref", "")
    if not prompt_path.is_file():
        return
    text = prompt_path.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        m = ABS_PATH_RE.search(line)
        if m:
            rep.err(
                f"prompts (cloud cadence {cad['slug']})",
                f"absolute local path {m.group(0)!r} at {cad['prompt_ref']}:{lineno}; "
                "cloud runs have no local filesystem",
            )


def main() -> int:
    rep = validate()
    if rep.ok():
        print("validate: OK (registry + all cadences pass schema and tier constraints)")
        return 0
    print("validate: FAILED", file=sys.stderr)
    for line in rep.errors:
        print(f"  - {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

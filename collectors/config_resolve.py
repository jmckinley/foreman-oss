"""C6 -- the config resolver (SPEC.md section 10).

Answers three questions per project per host: what is declared, what is effective, and what
actually fired. File resolution is a *prediction*; the ``claude -p`` probe is the
*measurement*. Where they disagree the probe wins and the key is flagged as drift -- never a
silent overwrite (invariant 5, failure guard 5).

The pure functions (layer merge, MCP AND-gate, skill invocability, memory-import walk, probe
reconciliation, budget) take already-loaded structures so they are testable without a live
``~/.claude``. Thin loaders read the real tree for production use; IO (the probe, plugin
details) is injected so it can be faked.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# Settings layers, highest precedence first (SPEC.md section 10).
LAYER_ORDER = ["managed", "cli", "local", "project", "user"]


# ----------------------------------------------------------------- layer merge

def flatten(settings: dict, prefix: str = "") -> dict:
    """Flatten a settings dict to dot-paths; lists and scalars are leaves."""
    out: dict = {}
    for k, v in (settings or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def merge_layers(layers: dict[str, dict]) -> dict:
    """Resolve every key across the layers (SPEC.md section 10 algorithm).

    List-valued keys (permissions.allow/deny/ask, the MCP allow-lists) concatenate across
    layers in precedence order -- which is exactly why a lower-layer ``permissions.deny``
    still applies over a higher-layer allow. Scalar keys take the highest layer present.
    """
    flat = {name: flatten(layers.get(name) or {}) for name in LAYER_ORDER}
    keys = set().union(*[set(f) for f in flat.values()]) if flat else set()
    resolved: dict = {}
    for key in sorted(keys):
        cands = [(name, flat[name][key]) for name in LAYER_ORDER if key in flat[name]]
        if cands and all(isinstance(v, list) for _, v in cands):
            eff: list = []
            for _, v in cands:
                for item in v:
                    if item not in eff:
                        eff.append(item)
            resolved[key] = {"effective": eff, "winning_layer": "merged",
                             "shadowed": [n for n, _ in cands[1:]], "disagrees_with_probe": False}
        else:
            winning, effective = cands[0]
            resolved[key] = {"effective": effective, "winning_layer": winning,
                             "shadowed": [n for n, _ in cands[1:]], "disagrees_with_probe": False}
    return resolved


def apply_env_overlay(resolved: dict, env: dict) -> None:
    """Per-pair model overlay (not a layer): hard var beats files, soft var only fills a gap."""
    hard, soft = env.get("ANTHROPIC_MODEL"), env.get("ANTHROPIC_DEFAULT_MODEL")
    if hard:
        prev = resolved.get("model")
        shadowed = ([prev["winning_layer"], *prev["shadowed"]] if prev else [])
        resolved["model"] = {"effective": hard, "winning_layer": "env:hard",
                             "shadowed": shadowed, "disagrees_with_probe": False}
    elif soft and "model" not in resolved:
        resolved["model"] = {"effective": soft, "winning_layer": "env:soft",
                             "shadowed": [], "disagrees_with_probe": False}


# --------------------------------------------------------------------- MCP gates

def resolve_mcp(*, user_servers: dict, project_disabled: list, mcpjson_servers: dict,
                enable_all: bool, enabled_json: list, disabled_json: list) -> dict:
    """AND the four independent MCP gates; report the effective state, not any one file."""
    proj_off = set(project_disabled or [])
    on_json = set(enabled_json or [])
    off_json = set(disabled_json or [])
    out: dict = {}
    for name in (user_servers or {}):
        out[name] = {"source": "user", "enabled": name not in proj_off}
    for name in (mcpjson_servers or {}):
        opted_in = bool(enable_all) or name in on_json          # gate 1
        enabled = opted_in and name not in off_json and name not in proj_off  # gates 2, 3
        out[name] = {"source": ".mcp.json", "enabled": bool(enabled)}
    return out


# ------------------------------------------------------------------ skills

def _skill_denied(name: str, deny_rules: list) -> bool:
    for rule in deny_rules or []:
        if re.fullmatch(rf"Skill\(\s*{re.escape(name)}\s*(:.*)?\)", rule) or rule == f"Skill({name})":
            return True
    return False


def resolve_skills(skills: list[dict], *, deny_rules: list | None = None,
                   skill_overrides: dict | None = None) -> list[dict]:
    """Installed is not invocable. A skill is invocable unless it opts out, a Skill deny
    rule matches, or a skillOverrides entry disables it (SPEC.md section 10)."""
    overrides = skill_overrides or {}
    out = []
    for s in skills:
        fm = s.get("frontmatter") or {}
        ov = overrides.get(s["name"])
        override_off = ov is False or (isinstance(ov, dict) and ov.get("enabled") is False)
        invocable = (not fm.get("disable-model-invocation")
                     and not _skill_denied(s["name"], deny_rules or [])
                     and not override_off)
        out.append({"kind": "skill", "name": s["name"], "version": fm.get("version"),
                    "scope": s.get("scope"), "source": s.get("source"),
                    "invocable": bool(invocable), "always_on_tokens": None})
    return out


def read_skill_frontmatter(skill_md: Path) -> dict:
    """Parse a SKILL.md YAML frontmatter block. Malformed frontmatter degrades to {} rather
    than aborting the collector (a broken skill file must not stop config resolution)."""
    import yaml
    try:
        text = skill_md.read_text()
    except OSError:
        return {}
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ memory imports

_IMPORT_RE = re.compile(r"^@(\S+)", re.MULTILINE)


def walk_memory(entry_files: list[Path]) -> list[dict]:
    """Walk memory files and their transitive @imports, recording depth (SPEC.md section 10)."""
    seen: set[Path] = set()
    out: list[dict] = []

    def rec(path: Path, depth: int) -> None:
        try:
            rp = path.resolve()
        except OSError:
            return
        if rp in seen or not path.is_file():
            return
        seen.add(rp)
        out.append({"kind": "memory", "name": str(path), "scope": None, "source": None,
                    "version": None, "invocable": None, "always_on_tokens": None, "depth": depth})
        try:
            text = path.read_text()
        except OSError:
            return
        for imp in _IMPORT_RE.findall(text):
            rec((path.parent / imp).expanduser(), depth + 1)

    for f in entry_files:
        rec(Path(f), 0)
    return out


# ------------------------------------------------------------------ probe + drift

def reconcile_probe(resolved: dict, probe: dict | None) -> list[tuple[str, str]]:
    """Compare prediction to the probe. Disagreement -> winning_layer='probe' + drift."""
    drift: list[tuple[str, str]] = []
    if not probe:
        return drift
    for key, probe_key in (("model", "model"), ("permission_mode", "permission_mode")):
        observed = probe.get(probe_key)
        if observed is None:
            continue
        predicted = resolved.get(key, {}).get("effective")
        if predicted is not None and observed != predicted:
            resolved[key] = {"effective": observed, "winning_layer": "probe",
                             "shadowed": resolved.get(key, {}).get("shadowed", []),
                             "disagrees_with_probe": True}
            drift.append(("probe", f"{key}: predicted {predicted!r}, probe reports {observed!r}"))
    return drift


def _probe_list(probe: dict, *keys: str) -> list | None:
    """The first probe field that is a list, or None if the probe reported none of them.
    None means 'the probe did not enumerate this kind' -- treated as unknown, never empty."""
    for k in keys:
        v = probe.get(k)
        if isinstance(v, list):
            return v
    return None


def _probe_name(x) -> str | None:
    if isinstance(x, dict):
        return x.get("name") or x.get("skill") or x.get("server")
    return x if isinstance(x, str) else None


def reconcile_components(components: list[dict], probe: dict | None) -> list[str]:
    """Reconcile the file-derived component inventory against the probe, the measurement.

    Sets ``disagrees_with_probe`` on any component the probe contradicts and returns a human
    drift message per contradiction. Deliberately conservative: only an *explicit* mismatch
    counts. A probe that simply omits a kind (no list for it) is 'unknown', never 'absent' --
    the probe JSON is model-authored and not guaranteed exhaustive, and a false drift that
    cries wolf is worse than a missed one. The one exception is the invocability contradiction
    (predicted invocable, probe says it will not fire): a silent-no-op cadence is the exact
    failure the effective/declared split exists to catch (SPEC §10 skills trap).
    """
    drift: list[str] = []
    if not probe:
        return drift

    pskills = _probe_list(probe, "skills")
    if pskills is not None:
        observed = {s["name"]: s.get("invocable") for s in pskills
                    if isinstance(s, dict) and s.get("name") is not None}
        for c in components:
            if c["kind"] == "skill" and c.get("invocable") and observed.get(c["name"]) is False:
                c["disagrees_with_probe"] = True
                drift.append(f"skill {c['name']} predicted invocable, probe says it will not "
                             f"fire unprompted (silent no-op)")

    pmcp = _probe_list(probe, "mcp", "mcp_servers", "connected_mcp")
    if pmcp is not None:
        names = {_probe_name(x) for x in pmcp}
        for c in components:
            if c["kind"] == "mcp" and c.get("invocable") and c["name"] not in names:
                c["disagrees_with_probe"] = True
                drift.append(f"mcp {c['name']} predicted enabled, probe does not report it connected")

    pplug = _probe_list(probe, "plugins")
    if pplug is not None:
        names = {_probe_name(x) for x in pplug}
        for c in components:
            if c["kind"] == "plugin" and c["name"] not in names:
                c["disagrees_with_probe"] = True
                drift.append(f"plugin {c['name']} predicted active, probe does not report it loaded")

    return drift


def shadow_conflicts(resolved: dict) -> list[str]:
    """Patterns present in both permissions.allow and permissions.deny -- deny wins, and the
    allow is a shadowing surprise (the documented trap, SPEC.md section 10)."""
    allow = resolved.get("permissions.allow", {}).get("effective") or []
    deny = resolved.get("permissions.deny", {}).get("effective") or []
    return [p for p in allow if p in deny]


def plugin_budget(active_plugins: list[str], details_fn, budget: int) -> dict:
    total = 0
    for name in active_plugins:
        d = details_fn(name) or {}
        total += int(d.get("always_on_tokens") or 0)
    return {"always_on_total": total, "budget": budget, "breach": total > budget}


# --------------------------------------------------------------------- persistence

def write_snapshot(conn, *, project: str, host: str, taken: str, cc_version: str,
                   resolved: dict, components: list[dict], probe_ok: bool) -> int:
    from collectors import db
    effective_json = json.dumps(
        {k: v["effective"] for k, v in resolved.items()}, sort_keys=True, default=str)
    snap_hash = hashlib.sha1(f"{project}|{host}|{effective_json}".encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO config_snapshot(project, host, taken, cc_version, hash, probe_ok, effective_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (project, host, taken, cc_version, snap_hash, 1 if probe_ok else 0, effective_json))
    snapshot_id = cur.lastrowid
    for key, v in resolved.items():
        db.upsert(conn, "config_key", {
            "snapshot_id": snapshot_id, "key": key,
            "effective": json.dumps(v["effective"], default=str),
            "winning_layer": v["winning_layer"],
            "shadowed": json.dumps(v.get("shadowed") or []),
            "disagrees_with_probe": 1 if v.get("disagrees_with_probe") else 0,
        }, keys=["snapshot_id", "key"])
    for c in components:
        db.upsert(conn, "component", {
            "snapshot_id": snapshot_id, "kind": c["kind"], "name": c["name"],
            "version": c.get("version"), "scope": c.get("scope"), "source": c.get("source"),
            "invocable": (None if c.get("invocable") is None else (1 if c["invocable"] else 0)),
            "always_on_tokens": c.get("always_on_tokens"),
            "disagrees_with_probe": 1 if c.get("disagrees_with_probe") else 0,
        }, keys=["snapshot_id", "kind", "name", "scope"])
    conn.commit()
    return snapshot_id


def latest_snapshot(conn, project: str, host: str | None = None):
    q = "SELECT * FROM config_snapshot WHERE project = ?"
    args: list = [project]
    if host:
        q += " AND host = ?"
        args.append(host)
    q += " ORDER BY taken DESC LIMIT 1"
    return conn.execute(q, args).fetchone()


def latest_snapshots_by_host(conn, project: str) -> dict[str, int]:
    """The id of the latest config snapshot per host for a project."""
    hosts = [r["host"] for r in conn.execute(
        "SELECT DISTINCT host FROM config_snapshot WHERE project = ?", (project,))]
    out: dict[str, int] = {}
    for h in hosts:
        snap = latest_snapshot(conn, project, h)
        if snap is not None:
            out[h] = snap["id"]
    return out


def _comp_label(v) -> str:
    """Component state on a host: absent / installed (invocability n/a) / invocable / inert."""
    if v == "absent":
        return "absent"
    if v is None:
        return "installed"          # plugin/memory: no invocability concept
    return "invocable" if v else "inert"


def fleet_diff(conn, project: str) -> dict:
    """Cross-host divergence of the resolved config for a project (SPEC §10, M8.7).

    Answers "what is activated on mbp but not vps?": config keys whose effective value
    differs across hosts (present-vs-absent counts), and components (skills/plugins/MCP)
    whose presence or invocability differs. A single-host project yields empty diffs -- there
    is nothing to diverge from -- which is the honest answer, not a warning.
    """
    by_host = latest_snapshots_by_host(conn, project)
    hosts = sorted(by_host)

    key_vals: dict[str, dict[str, str]] = {}
    for h in hosts:
        for r in conn.execute("SELECT key, effective FROM config_key WHERE snapshot_id = ?",
                              (by_host[h],)):
            key_vals.setdefault(r["key"], {})[h] = r["effective"]
    keys = []
    for key in sorted(key_vals):
        hv = key_vals[key]
        vals = [hv.get(h) for h in hosts]                  # None = key absent on that host
        if len(set(vals)) > 1:
            keys.append({"key": key, "by_host": {h: hv.get(h) for h in hosts}})

    comp_state: dict[tuple, dict[str, object]] = {}
    for h in hosts:
        for r in conn.execute(
                "SELECT kind, name, scope, invocable FROM component WHERE snapshot_id = ?",
                (by_host[h],)):
            comp_state.setdefault((r["kind"], r["name"], r["scope"]), {})[h] = r["invocable"]
    components = []
    for ident in sorted(comp_state, key=lambda t: (t[0], t[1], t[2] or "")):
        hv = comp_state[ident]
        labels = [_comp_label(hv.get(h, "absent")) for h in hosts]
        if len(set(labels)) > 1:
            kind, name, scope = ident
            components.append({"kind": kind, "name": name, "scope": scope,
                               "by_host": {h: _comp_label(hv.get(h, "absent")) for h in hosts}})
    return {"project": project, "hosts": hosts, "keys": keys, "components": components}


def render_fleet_diff(diff: dict) -> str:
    """One-screen text render of a fleet diff (the /fleet-diff CLI output)."""
    hosts = diff["hosts"]
    if len(hosts) < 2:
        return (f"FLEET DIFF — {diff['project']}\n"
                f"  only {len(hosts)} host with a snapshot ({', '.join(hosts) or 'none'}); "
                "nothing to diff")
    lines = [f"FLEET DIFF — {diff['project']}  ({' vs '.join(hosts)})"]
    if not diff["keys"] and not diff["components"]:
        lines.append("  identical across hosts")
        return "\n".join(lines)
    if diff["keys"]:
        lines.append("  config keys:")
        for k in diff["keys"]:
            cells = ", ".join(f"{h}={_short(k['by_host'][h])}" for h in hosts)
            lines.append(f"    {k['key']:<28} {cells}")
    if diff["components"]:
        lines.append("  components:")
        for c in diff["components"]:
            cells = ", ".join(f"{h}={c['by_host'][h]}" for h in hosts)
            lines.append(f"    {c['kind']}/{c['name']:<22} {cells}")
    return "\n".join(lines)


def _short(v) -> str:
    if v is None:
        return "(absent)"
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "..."


def explain(conn, key: str, project: str, host: str | None = None) -> dict | None:
    """Name the winning layer and every shadowed layer for a key (the /explain-config query)."""
    snap = latest_snapshot(conn, project, host)
    if snap is None:
        return None
    row = conn.execute(
        "SELECT * FROM config_key WHERE snapshot_id = ? AND key = ?",
        (snap["id"], key)).fetchone()
    if row is None:
        return None
    return {
        "key": key, "project": project,
        "effective": json.loads(row["effective"]),
        "winning_layer": row["winning_layer"],
        "shadowed": json.loads(row["shadowed"] or "[]"),
        "disagrees_with_probe": bool(row["disagrees_with_probe"]),
        "taken": snap["taken"],
    }


def drift_lines(conn, registry: dict) -> list[tuple[str, str]]:
    """Build the brief's DRIFT rows: pin drift, shadowing surprises, budget breach."""
    lines: list[tuple[str, str]] = []
    defaults = registry.get("defaults") or {}
    pin_default = defaults.get("marketplace_pin")
    budget = defaults.get("context_budget_tokens")

    for proj in registry.get("projects") or []:
        slug = proj["slug"]
        pin = proj.get("marketplace_pin") or pin_default
        snap = latest_snapshot(conn, slug)
        if snap is None:
            continue
        # pin drift: installed plugin version vs the registry pin.
        for c in conn.execute(
                "SELECT name, version FROM component WHERE snapshot_id = ? AND kind = 'plugin'",
                (snap["id"],)):
            if c["version"] and pin and c["version"] != pin:
                lines.append((slug, f"{c['name']} {c['version']}, pinned {pin}"))
        # shadowing surprises: probe disagreements + allow/deny conflicts.
        for k in conn.execute(
                "SELECT key, winning_layer FROM config_key WHERE snapshot_id = ? "
                "AND disagrees_with_probe = 1", (snap["id"],)):
            lines.append((slug, f"{k['key']} resolved by probe ({k['winning_layer']} lost)"))
        # component drift: a skill/plugin/mcp whose live behaviour contradicts the prediction
        # (the moat -- a cadence that would silently no-op is caught here, not in production).
        for c in conn.execute(
                "SELECT kind, name, invocable FROM component WHERE snapshot_id = ? "
                "AND disagrees_with_probe = 1", (snap["id"],)):
            detail = (" would not fire (silent no-op)"
                      if c["kind"] == "skill" and c["invocable"] else " not confirmed by probe")
            lines.append((slug, f"{c['kind']} {c['name']}{detail}"))
        allow = _key_list(conn, snap["id"], "permissions.allow")
        deny = _key_list(conn, snap["id"], "permissions.deny")
        for pat in allow:
            if pat in deny:
                lines.append((slug, f"permissions.deny shadows allow for {pat}"))

    # budget breach is amber on every project at once.
    total, any_snap = _budget_total(conn, registry)
    if any_snap and budget is not None and total > budget:
        lines.append(("all", f"always-on plugin cost {total:,} tok, budget {budget:,}"))
    return lines


def _key_list(conn, snapshot_id: int, key: str) -> list:
    row = conn.execute("SELECT effective FROM config_key WHERE snapshot_id = ? AND key = ?",
                       (snapshot_id, key)).fetchone()
    if not row:
        return []
    try:
        val = json.loads(row["effective"])
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def _budget_total(conn, registry: dict) -> tuple[int, bool]:
    """Max always-on plugin total across the latest snapshots -- promoting a plugin taxes all."""
    best = 0
    seen = False
    for proj in registry.get("projects") or []:
        snap = latest_snapshot(conn, proj["slug"])
        if snap is None:
            continue
        seen = True
        row = conn.execute(
            "SELECT COALESCE(SUM(always_on_tokens), 0) t FROM component "
            "WHERE snapshot_id = ? AND kind = 'plugin'", (snap["id"],)).fetchone()
        best = max(best, int(row["t"] or 0))
    return best, seen


# --------------------------------------------------------------------- FS loaders

def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _managed_path() -> Path:
    import sys
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def load_layers(claude_home: Path, project_dir: Path) -> dict:
    """Assemble the five settings layers from disk (missing files -> empty)."""
    return {
        "managed": _read_json(_managed_path()) or {},
        "cli": {},  # session-scoped; supplied per invocation, not read from disk
        "local": _read_json(project_dir / ".claude" / "settings.local.json") or {},
        "project": _read_json(project_dir / ".claude" / "settings.json") or {},
        "user": _read_json(claude_home / "settings.json") or {},
    }


def load_skills(claude_home: Path, project_dir: Path, *, deny_rules, skill_overrides) -> list[dict]:
    found: list[dict] = []
    for base, scope in ((claude_home / "skills", "user"), (project_dir / ".claude" / "skills", "project")):
        if not base.is_dir():
            continue
        for skill_md in base.glob("*/SKILL.md"):
            found.append({"name": skill_md.parent.name, "scope": scope,
                          "source": str(base), "frontmatter": read_skill_frontmatter(skill_md)})
    return resolve_skills(found, deny_rules=deny_rules, skill_overrides=skill_overrides)


def load_plugins(claude_home: Path, details_fn) -> list[dict]:
    """Only installed_plugins.json counts as active (SPEC.md section 10 plugin trap)."""
    data = _read_json(claude_home / "plugins" / "installed_plugins.json") or {}
    out: list[dict] = []
    entries = data.get("plugins", data) if isinstance(data, dict) else {}
    for name, meta in (entries.items() if isinstance(entries, dict) else []):
        version = meta.get("version") if isinstance(meta, dict) else None
        details = details_fn(name) or {}
        out.append({"kind": "plugin", "name": name, "version": version, "scope": "user",
                    "source": "installed_plugins.json", "invocable": None,
                    "always_on_tokens": details.get("always_on_tokens")})
    return out


def load_memory_entries(claude_home: Path, project_dir: Path) -> list[Path]:
    entries = [claude_home / "CLAUDE.md", project_dir / "CLAUDE.md",
               project_dir / "CLAUDE.local.md"]
    mem_dir = claude_home / "memory"
    if mem_dir.is_dir():
        entries.extend(sorted(mem_dir.glob("*.md")))
    return [e for e in entries if e.is_file()]


def _claude_probe(project_dir: Path) -> dict | None:
    """Run the live probe. File resolution is a prediction; this is the measurement."""
    import subprocess
    prompt = ("Report as JSON only: active model, permission mode, every enabled plugin "
              "with version and source, every skill with scope and whether you may invoke it "
              "unprompted, every connected MCP server, every hook, and every memory file in "
              "your context with its import chain.")
    try:
        p = subprocess.run(["claude", "-p", "--output-format", "json", prompt],
                           cwd=str(project_dir), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def resolve_project(conn, *, project: str, host: str, taken: str, cc_version: str,
                    claude_home: Path, project_dir: Path, env: dict,
                    probe_fn=None, details_fn=lambda _name: {}) -> dict:
    """Full C6 resolution for one project/host: merge, overlay, probe, persist a snapshot."""
    layers = load_layers(claude_home, project_dir)
    resolved = merge_layers(layers)
    apply_env_overlay(resolved, env)

    user = layers["user"]
    project_settings = layers["project"]
    deny_rules = (resolved.get("permissions.deny", {}) or {}).get("effective") or []
    skills = load_skills(claude_home, project_dir, deny_rules=deny_rules,
                         skill_overrides=user.get("skillOverrides") or {})
    plugins = load_plugins(claude_home, details_fn)
    memory = walk_memory(load_memory_entries(claude_home, project_dir))

    mcp = resolve_mcp(
        user_servers=(_read_json(claude_home.parent / ".claude.json") or {}).get("mcpServers") or {},
        project_disabled=project_settings.get("disabledMcpServers") or [],
        mcpjson_servers=(_read_json(project_dir / ".mcp.json") or {}).get("mcpServers") or {},
        enable_all=bool(project_settings.get("enableAllProjectMcpServers")),
        enabled_json=project_settings.get("enabledMcpjsonServers") or [],
        disabled_json=project_settings.get("disabledMcpjsonServers") or [])
    mcp_components = [{"kind": "mcp", "name": n, "version": None, "scope": None,
                      "source": v["source"], "invocable": v["enabled"], "always_on_tokens": None}
                     for n, v in mcp.items()]

    probe = (probe_fn or _claude_probe)(project_dir)
    reconcile_probe(resolved, probe)

    components = [*skills, *plugins, *mcp_components, *memory]
    reconcile_components(components, probe)
    write_snapshot(conn, project=project, host=host, taken=taken, cc_version=cc_version,
                   resolved=resolved, components=components, probe_ok=probe is not None)
    return {"keys": len(resolved), "components": len(components), "probe_ok": probe is not None}


# ---------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    from collectors import db

    ap = argparse.ArgumentParser(prog="config_resolve")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("explain", help="name the winning and shadowed layers for a key")
    pe.add_argument("key")
    pe.add_argument("project")
    pe.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    pe.add_argument("--host")

    pr = sub.add_parser("resolve", help="resolve one project/host and write a snapshot")
    pr.add_argument("project")
    pr.add_argument("--host", default=os.environ.get("FOREMAN_HOST", "mbp"))
    pr.add_argument("--project-dir", required=True)
    pr.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))
    pr.add_argument("--claude-home", default=os.path.expanduser("~/.claude"))

    pf = sub.add_parser("fleet-diff", help="cross-host diff of a project's resolved config")
    pf.add_argument("project")
    pf.add_argument("--index", default=os.environ.get("FOREMAN_INDEX"))

    args = ap.parse_args(argv)
    index = args.index or str(db.default_path())

    if args.cmd == "explain":
        conn = db.open_index(index)
        result = explain(conn, args.key, args.project, args.host)
        if result is None:
            print(f"no config snapshot for {args.project} (run collectors first)")
            return 1
        print(f"{args.key} on {args.project} (snapshot {result['taken']}):")
        print(f"  effective:     {result['effective']}")
        print(f"  winning layer: {result['winning_layer']}")
        print(f"  shadowed:      {result['shadowed'] or '(none)'}")
        if result["disagrees_with_probe"]:
            print("  NOTE: resolved by the live probe; the file prediction disagreed (drift).")
        return 0

    if args.cmd == "resolve":
        import datetime as _dt
        conn = db.open_index(index)
        taken = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = resolve_project(conn, project=args.project, host=args.host, taken=taken,
                              cc_version="unknown", claude_home=Path(args.claude_home),
                              project_dir=Path(args.project_dir), env=dict(os.environ))
        print(f"resolved {args.project}: {out}")
        return 0

    if args.cmd == "fleet-diff":
        conn = db.open_index(index)
        print(render_fleet_diff(fleet_diff(conn, args.project)))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

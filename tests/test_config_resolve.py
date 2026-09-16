"""config_resolve — the settings-layer merge, MCP/skill gates, probe reconciliation, and the
smaller pure helpers (SPEC.md section 10). Complements the C6 integration test in test_index."""
from collectors import config_resolve as C


def test_flatten():
    assert C.flatten({"a": 1, "b": {"c": 2, "d": {"e": 3}}, "l": [1, 2]}) == {
        "a": 1, "b.c": 2, "b.d.e": 3, "l": [1, 2]}


def test_merge_layers_scalar_and_list():
    layers = {
        "user":    {"model": "sonnet", "permissions": {"allow": ["Read"], "deny": ["Bash(rm*)"]}},
        "project": {"model": "opus",   "permissions": {"allow": ["Write"]}},
    }
    r = C.merge_layers(layers)
    # scalar: highest layer present wins (project beats user in LAYER_ORDER)
    assert r["model"]["effective"] == "opus" and r["model"]["winning_layer"] == "project"
    assert r["model"]["shadowed"] == ["user"]
    # lists concatenate across layers in precedence order, de-duped
    assert r["permissions.allow"]["effective"] == ["Write", "Read"]
    assert r["permissions.allow"]["winning_layer"] == "merged"
    assert r["permissions.deny"]["effective"] == ["Bash(rm*)"]


def test_apply_env_overlay():
    r = {"model": {"effective": "opus", "winning_layer": "project", "shadowed": [],
                   "disagrees_with_probe": False}}
    C.apply_env_overlay(r, {"ANTHROPIC_MODEL": "haiku"})       # hard var wins
    assert r["model"]["effective"] == "haiku" and r["model"]["winning_layer"] == "env:hard"
    assert "project" in r["model"]["shadowed"]
    r2: dict = {}
    C.apply_env_overlay(r2, {"ANTHROPIC_DEFAULT_MODEL": "sonnet"})   # soft only fills a gap
    assert r2["model"]["effective"] == "sonnet" and r2["model"]["winning_layer"] == "env:soft"
    r3 = {"model": {"effective": "opus", "winning_layer": "user", "shadowed": [],
                    "disagrees_with_probe": False}}
    C.apply_env_overlay(r3, {"ANTHROPIC_DEFAULT_MODEL": "sonnet"})   # soft does NOT override
    assert r3["model"]["effective"] == "opus"


def test_resolve_mcp_gates():
    out = C.resolve_mcp(user_servers={"u1": {}}, project_disabled=["u1", "m2"],
                        mcpjson_servers={"m1": {}, "m2": {}, "m3": {}},
                        enable_all=False, enabled_json=["m1", "m2"], disabled_json=["m1"])
    assert out["u1"]["enabled"] is False                 # user server disabled by project
    assert out["m1"]["enabled"] is False                 # opted-in but in disabled_json
    assert out["m2"]["enabled"] is False                 # opted-in but project-disabled
    assert out["m3"]["enabled"] is False                 # never opted in (enable_all False)
    out2 = C.resolve_mcp(user_servers={}, project_disabled=[], mcpjson_servers={"m3": {}},
                         enable_all=True, enabled_json=[], disabled_json=[])
    assert out2["m3"]["enabled"] is True                 # enable_all opts everything in


def test_resolve_skills_invocability():
    skills = [
        {"name": "a", "scope": "user", "source": "x", "frontmatter": {"version": "1"}},
        {"name": "b", "scope": "user", "source": "x",
         "frontmatter": {"disable-model-invocation": True}},
        {"name": "c", "scope": "user", "source": "x", "frontmatter": {}},
        {"name": "d", "scope": "user", "source": "x", "frontmatter": {}},
    ]
    out = {s["name"]: s for s in C.resolve_skills(
        skills, deny_rules=["Skill(c)"], skill_overrides={"d": False})}
    assert out["a"]["invocable"] is True
    assert out["b"]["invocable"] is False                # opts out via frontmatter
    assert out["c"]["invocable"] is False                # Skill() deny rule
    assert out["d"]["invocable"] is False                # skillOverrides disables


def test_shadow_conflicts():
    resolved = {"permissions.allow": {"effective": ["Read", "Bash(rm*)"]},
                "permissions.deny": {"effective": ["Bash(rm*)"]}}
    assert C.shadow_conflicts(resolved) == ["Bash(rm*)"]
    assert C.shadow_conflicts({}) == []


def test_plugin_budget():
    details = {"p1": {"always_on_tokens": 1000}, "p2": {"always_on_tokens": 1600}}
    r = C.plugin_budget(["p1", "p2"], lambda n: details.get(n), budget=2400)
    assert r["always_on_total"] == 2600 and r["breach"] is True
    assert C.plugin_budget(["p1"], lambda n: details.get(n), budget=2400)["breach"] is False


def test_reconcile_probe_model_drift():
    resolved = {"model": {"effective": "opus", "winning_layer": "project", "shadowed": [],
                          "disagrees_with_probe": False}}
    drift = C.reconcile_probe(resolved, {"model": "haiku"})
    assert resolved["model"]["effective"] == "haiku"
    assert resolved["model"]["winning_layer"] == "probe"
    assert resolved["model"]["disagrees_with_probe"] is True
    assert drift and "predicted 'opus'" in drift[0][1] and "haiku" in drift[0][1]
    # a matching probe (or no probe) produces no drift
    ok = {"model": {"effective": "opus", "winning_layer": "p", "shadowed": [], "disagrees_with_probe": False}}
    assert C.reconcile_probe(ok, {"model": "opus"}) == []
    assert C.reconcile_probe(ok, None) == []


def test_read_skill_frontmatter(tmp_path):
    good = tmp_path / "SKILL.md"
    good.write_text("---\nname: x\nversion: 2\n---\n# body\n")
    assert C.read_skill_frontmatter(good) == {"name": "x", "version": 2}
    bad = tmp_path / "b.md"
    bad.write_text("no frontmatter here")
    assert C.read_skill_frontmatter(bad) == {}
    assert C.read_skill_frontmatter(tmp_path / "missing.md") == {}


def test_walk_memory_imports_and_depth(tmp_path):
    (tmp_path / "root.md").write_text("root\n@child.md\n@child.md\n")   # duplicate import
    (tmp_path / "child.md").write_text("child\n@root.md\n")             # cycle back to root
    out = C.walk_memory([tmp_path / "root.md"])
    names = {__import__("os").path.basename(m["name"]): m["depth"] for m in out}
    assert names["root.md"] == 0 and names["child.md"] == 1
    assert len(out) == 2                                                 # each file once (cycle-safe)

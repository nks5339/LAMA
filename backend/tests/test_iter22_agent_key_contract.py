"""iter-22 — every agent_key a call site passes must have a declared tier.

`test_iter19_prompt_contracts.py` guards one direction: a tier with no call
site. Nothing guarded the other, and that is the direction the real defects
were in.

`resolve_model` reads `agent_configs.complexity` first and falls back to
`AGENT_COMPLEXITY`, defaulting to "medium" on a miss — silently. So a call
site passing a key nobody declared routes at the default tier forever, and
the only symptom is that the agent is quietly weaker than its config says.

The case that motivated this: the Gap Analyzer passed the bare string
`gap_analyzer` while the map declared `tools.gap_analyzer: high` and the
prompt row was `tools.gap_analyzer`. Three names for one agent, and the tier
it declares had never once been applied — since iter-16.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from fabric.model_fabric import AGENT_COMPLEXITY, TIER_ORDER  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent

# Keys that are deliberately NOT tiered.
_UNTIERED_BY_DESIGN = {
    # `llm.fabric_call`'s own sentinel when the frame-walk cannot infer a
    # caller. It is the "we do not know" value, not an agent.
    "unknown",
}


def _product_files() -> list[Path]:
    """Every backend .py that is not a test."""
    out: list[Path] = []
    for p in BACKEND.rglob("*.py"):
        rel = p.relative_to(BACKEND).as_posix()
        if rel.startswith("tests/") or "/__pycache__/" in f"/{rel}":
            continue
        if rel.startswith("venv/") or rel.startswith(".venv/"):
            continue
        out.append(p)
    return out


def _literal_agent_keys() -> dict[str, str]:
    """{agent_key: "file:line"} for every literal `agent_key=` in product code.

    Parsed with `ast` rather than regex so a key inside a comment or a
    docstring cannot masquerade as a call site — the mistake that made the
    iter-19 orphan guard vacuous was exactly this class of confusion.
    """
    found: dict[str, str] = {}
    for path in _product_files():
        try:
            tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
        except SyntaxError:  # pragma: no cover — defensive
            continue
        rel = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(tree):
            # `fabric_call(..., agent_key="x")`
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "agent_key" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str) and kw.value.value:
                        found.setdefault(kw.value.value, f"{rel}:{kw.value.lineno}")
            # `agent_key: str = "x"` and `agent_key = "x"` — the DCTE agents
            # carry their key as a parameter default, which is still the key
            # that reaches the fabric.
            targets: list[ast.expr] = []
            if isinstance(node, ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.Assign):
                targets = list(node.targets)
            if targets and isinstance(getattr(node, "value", None), ast.Constant):
                val = node.value.value
                if isinstance(val, str) and val:
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id == "agent_key":
                            found.setdefault(val, f"{rel}:{node.lineno}")
        # Parameter defaults: `async def f(*, agent_key: str = "dcte.build_fixer")`
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            pairs = list(zip(args.args[len(args.args) - len(args.defaults):],
                             args.defaults))
            pairs += list(zip(args.kwonlyargs, args.kw_defaults))
            for arg, default in pairs:
                if arg.arg != "agent_key" or default is None:
                    continue
                if isinstance(default, ast.Constant) and isinstance(default.value, str) \
                        and default.value:
                    found.setdefault(default.value, f"{rel}:{node.lineno}")
    return found


def test_every_agent_key_at_a_call_site_declares_a_tier():
    """A key nobody declared routes at the default `medium`, silently, for
    as long as it exists. This is the guard the Gap Analyzer needed."""
    undeclared = {
        key: where for key, where in _literal_agent_keys().items()
        if key not in AGENT_COMPLEXITY and key not in _UNTIERED_BY_DESIGN
    }
    assert undeclared == {}, (
        "agent_key passed at a call site with no AGENT_COMPLEXITY entry — "
        f"each of these routes at the default tier: {undeclared}"
    )


def test_the_gap_analyzer_uses_the_key_that_declares_its_tier():
    """The specific regression. Three names for one agent — the call site's
    `gap_analyzer`, the map's `tools.gap_analyzer`, the prompt row's
    `tools.gap_analyzer` — and only the two that matched did anything."""
    src = (BACKEND / "routes" / "tools.py").read_text()
    assert 'agent_key="gap_analyzer"' not in src, (
        "the bare key is back; it is in no tier map and routes at `medium`"
    )
    assert 'agent_key="tools.gap_analyzer"' in src
    assert AGENT_COMPLEXITY["tools.gap_analyzer"] == "high"


def test_the_deep_analyzers_are_tiered_for_the_work_they_do():
    """Both read a whole legacy codebase in one call. `legacy.deep_analyzer`
    passed no agent_key at all, so the frame-walk resolved it to `unknown`
    and it ran at `medium`; `kb_deep_analysis` was not in the map and used
    an underscore name that broke the `a.b` convention besides."""
    assert AGENT_COMPLEXITY["legacy.deep_analyzer"] == "high"
    assert AGENT_COMPLEXITY["kb.deep_analysis"] == "high"
    src = (BACKEND / "kb" / "legacy_analyzer.py").read_text()
    assert 'agent_key="legacy.deep_analyzer"' in src


def test_the_deep_analyzer_reads_the_content_field_not_the_dict():
    """`chat_completion` is `llm.fabric_call`, which returns a dict.
    `(response or "").strip()` raised AttributeError on every single run,
    the surrounding `except Exception` turned it into `{"error": ...}`, and
    the agent had never once succeeded.

    Asserted on the parsed AST, not on the file's text: the explanation of
    this bug quotes the old expression in a comment, and a grep cannot tell
    a comment from code. That confusion is exactly what let the iter-19
    orphan guard pass while proving nothing.
    """
    tree = ast.parse((BACKEND / "kb" / "deep_analyzer.py").read_text())
    # Only the assignment that derives raw_text FROM the response. The other
    # two strip markdown fences off it and mention neither.
    exprs = [
        ast.unparse(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "raw_text" for t in n.targets)
        and "response" in ast.unparse(n.value)
    ]
    assert len(exprs) == 1, f"expected one response->raw_text assignment, got {exprs}"
    expr = exprs[0]
    assert "content" in expr, f"raw_text does not read .content: {expr}"
    assert "isinstance" in expr, (
        f"raw_text does not distinguish the dict from a bare string: {expr}"
    )


@pytest.mark.parametrize("key", sorted(AGENT_COMPLEXITY))
def test_every_declared_tier_is_a_real_tier(key):
    """A typo'd tier resolves through `TIER_FALLBACK_CHAIN` to something
    plausible, so it never surfaces as an error — just as the wrong model."""
    assert AGENT_COMPLEXITY[key] in TIER_ORDER


def test_agent_keys_follow_the_dotted_convention():
    """`kb_deep_analysis` broke this — it used an underscore where every
    other key uses a dot. The convention is load-bearing: the Console groups
    agents by the segment before the first dot, so an undotted key lands in
    no group. Underscores WITHIN a segment are fine (`agent_memory.rollover`,
    `tools.transformer.devops_expert`); what matters is that there is a dot."""
    bad = [k for k in AGENT_COMPLEXITY
           if not re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+", k)]
    assert bad == [], f"agent keys with no dotted group prefix: {bad}"


def test_the_contextvar_pin_is_honoured_where_a_route_sets_one():
    """`fabric_call` consults the contextvar only when `agent_key` is falsy.
    Both these call sites pass an explicit key, so the route's
    `set_current_agent_key("...regenerate")` was discarded and every
    regenerate ran on the first-pass tier — the opposite of what iter-13.76
    set the pin up to do."""
    cg = (BACKEND / "routes" / "codegen.py").read_text()
    assert "get_current_agent_key() or _codegen_agent_key" in cg

    arch = (BACKEND / "routes" / "architecture.py").read_text()
    assert 'get_current_agent_key() or "arch.recommend"' in arch
    assert 'get_current_agent_key() or "arch.sequence"' in arch


def test_compilation_ready_needs_a_component_that_actually_built(tmp_path):
    """A run where every component SKIPPED — no manifest emitted by the
    Coder, or a tool outside the native-support set — used to print
    "COMPILATION READY ✔" at overall_score 0, which set `compile_green` in
    the multi-agent pipeline and let the DevOps audit report
    `production_ready=True`. Nothing had been compiled.

    Behavioural, not a grep: the comment explaining the fix quotes the old
    expression, so a source scan matches its own explanation."""
    import asyncio

    import routes.tools as tools_mod

    # `_run_compiler` writes an agent-run row on entry and exit. Same stub
    # `test_iter1544_compiler_agent.py` uses — without it motor reaches for a
    # loop that `asyncio.run` has already closed.
    async def _noop(*_a, **_kw):
        return "run-1"

    _orig_log, _orig_upd = tools_mod._log_agent_run, tools_mod._update_agent_run
    tools_mod._log_agent_run = _noop
    tools_mod._update_agent_run = _noop
    try:
        # No pom.xml anywhere -> the maven component skips.
        result = asyncio.run(tools_mod._run_compiler(
            "tx-skip", str(tmp_path), {"backend": "maven"},
        ))
    finally:
        tools_mod._log_agent_run, tools_mod._update_agent_run = _orig_log, _orig_upd
    assert result["components"][0]["status"] == "skipped"
    assert result["compilation_ready"] is False, (
        "a skipped component is not a passing one"
    )
    assert "nothing was compiled" in result["summary"]


def test_no_playbook_alias_points_at_another_frameworks_idioms():
    """`_playbook_for` renders what it resolves as "authoritative for idiom
    choices" and tells the Coder the playbook wins any conflict. Aliasing
    `angular-17` to `react-18` therefore instructed an Angular target, with
    authority, to write React function components and a react@18
    package.json."""
    from routes.tools import MIGRATION_PLAYBOOKS, _PLAYBOOK_ALIASES

    # An alias is safe only between stacks whose IDIOMS are interchangeable.
    # spring-boot-2 -> spring-boot-3 is; angular-17 -> react-18 is not.
    # Databases are one family here because the playbook for a SQL target is
    # about dialect, and `mysql -> postgresql` is a deliberate "migrate to
    # PostgreSQL" alias rather than a claim the two are the same.
    families = {
        "react": "react", "angular": "angular", "vue": "vue",
        "spring": "spring", "quarkus": "jvm-di", "micronaut": "jvm-di",
        "postgres": "sql", "mysql": "sql", "oracle": "sql",
    }

    def _family(stack_id: str) -> str:
        for token, fam in families.items():
            if token in stack_id:
                return fam
        return stack_id

    for src, dst in _PLAYBOOK_ALIASES.items():
        assert dst in MIGRATION_PLAYBOOKS, f"{src} aliases to a missing entry {dst}"
        assert _family(src) == _family(dst), (
            f"{src} -> {dst} crosses framework families; the playbook is "
            "rendered as authoritative, so this hands the Coder the wrong "
            "framework's rules"
        )


def test_angular_and_vue_have_their_own_playbooks():
    """The specific regression: both aliased to `react-18`, so `_playbook_for`
    told an Angular target — under the heading "authoritative for idiom
    choices", with "the playbook wins" appended — to write React function
    components and a react@18 package.json."""
    from routes.tools import MIGRATION_PLAYBOOKS, _playbook_for

    assert "angular-17" in MIGRATION_PLAYBOOKS
    assert "vue-3" in MIGRATION_PLAYBOOKS

    rendered = _playbook_for({"frontend": "angular-17"})
    assert "standalone components" in rendered
    # Word-boundary, because Angular's own idioms legitimately say
    # "typed reactive forms".
    assert not re.search(r"\breact\b", rendered, re.IGNORECASE), (
        "an Angular target is still being handed React's idioms"
    )
    assert "function components with hooks" not in rendered
    assert "react-dom" not in rendered

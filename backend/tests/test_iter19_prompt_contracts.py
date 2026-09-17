"""iter-19.2 — prompts must not assert things that are no longer true.

A prompt is code the model executes, but nothing type-checks it, so it
drifts silently as the pipeline around it changes. Two kinds of drift had
already happened here and neither was visible from any test:

  1. FACTUAL DRIFT. `tools.transformer.tester` introduced itself as "the
     final quality gate". That stopped being true at iter-15.44, when a
     real native build became the compile signal and this pass was demoted
     to a supplementary narrative. A model told it is the authority
     reports `compilation_ready` with authority it does not have.

  2. INPUT DRIFT — the worse one. `codegen.tester` asked the model to
     check "imports resolve to real modules", "method signatures match
     their callers" and "no circular dependencies" — from an input that
     contains no code at all. `_run_tester_for_codegen_wave` sends a task
     LEDGER: task_id, target_path, layer, status. Every code-level finding
     it produced was necessarily invented.

These tests read the seeded text and assert the claims that drifted. They
cannot prove a prompt is good; they prove it does not lie about what it is
or what it can see.
"""
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

SEED = (Path(__file__).resolve().parent.parent / "seed.py").read_text()


def prompt_template(key: str) -> str:
    """The seeded template text for one prompt key."""
    m = re.search(
        r'"key":\s*"' + re.escape(key) + r'".*?"template":\s*(?:f?"""(.*?)"""|\((.*?)\n        \),)',
        SEED, re.S,
    )
    assert m, f"prompt {key} not found in seed.py"
    return m.group(1) or m.group(2) or ""


def flat(key: str) -> str:
    """Lower-cased, whitespace-collapsed template.

    Prompts are hand-wrapped at ~72 columns, so a phrase the author wrote
    as one sentence is usually split across lines. Matching on the raw
    text would make these tests fail on a reflow that changed nothing.
    """
    return re.sub(r"\s+", " ", prompt_template(key)).lower()


# ── factual drift ─────────────────────────────────────────────────────

def test_transformer_tester_does_not_claim_to_be_the_final_gate():
    t = flat("tools.transformer.tester")
    assert "you are not the final quality gate" in t
    # And it must say what IS authoritative, so the model has somewhere to
    # defer to rather than just being told it is not in charge.
    assert "native" in t and "compiler" in t


def test_transformer_tester_is_told_not_to_contradict_the_real_build():
    """The native compiler sees the whole classpath; this pass sees a
    truncated slice of files as text. When they disagree, the compiler is
    right — so a suspicion must be a WARN, not a false verdict."""
    t = flat("tools.transformer.tester")
    assert "never contradict" in t
    assert "warn" in t


def test_codegen_tester_does_not_ask_for_analysis_of_code_it_never_receives():
    """`_run_tester_for_codegen_wave` sends a task ledger — task_id,
    target_path, layer, status. Asking for import resolution or signature
    matching against that guarantees invention."""
    t = flat("codegen.tester")
    assert "you are not given file contents" in t
    for impossible in ("all imports resolve", "method signatures match",
                       "no circular dependencies", "version compatibility"):
        assert impossible not in t, f"asks for {impossible!r} without the code"


def test_codegen_tester_checks_are_decidable_from_a_ledger():
    """What replaced the impossible checks has to be genuinely answerable
    from the ledger, or the agent is just as useless in a new way."""
    t = flat("codegen.tester")
    for real in ("completeness", "collision", "layer", "duplicate"):
        assert real in t, f"missing the decidable check: {real}"


# ── output contracts the code already defends against ─────────────────

@pytest.mark.parametrize("key", ["tools.transformer.tester", "codegen.tester"])
def test_string_fields_are_declared_as_strings(key):
    """Both parsers carry a coercion loop that stringifies these fields,
    added because models nested a self-invented object there and the
    frontend — which renders them straight as text — threw React error #31
    and blanked the page. The guard stays as defence in depth; the prompt
    is where the rule belongs."""
    t = prompt_template(key)
    assert "PLAIN STRING" in t
    low = flat(key)
    for field in ("details", "fix_suggestion", "category", "status"):
        assert field in low, field
    # The specific failure observed in production: a roll-up object
    # replacing the checks list.
    assert "checks" in low and "list" in low


@pytest.mark.parametrize("key", ["tools.transformer.tester", "codegen.tester"])
def test_score_is_declared_as_a_bare_number(key):
    t = flat(key)
    assert "overall_score" in t
    assert "bare number" in t


# ── grounding ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,needle", [
    ("tools.transformer.tester", "never name a path you were not given"),
    ("codegen.tester", "never name a file that is not in the ledger"),
    ("diff.srs", "never invent an id"),
    ("arch.chat", "grounding"),
    ("srs.edit", "must appear in the kb context"),
])
def test_prompt_forbids_inventing_what_it_was_not_given(key, needle):
    assert needle in flat(key), f"{key} lacks its grounding rule"


def test_srs_edit_states_the_preservation_contract():
    """An SRS edit that silently drops a requirement is the failure that
    matters: the section is frozen downstream, so anything omitted
    disappears from the data model and the generated code with nobody
    told."""
    t = flat("srs.edit")
    assert "preserve every requirement" in t
    assert "verbatim" in t
    assert "never renumber" in t


def test_diff_srs_keeps_every_heading_even_when_empty():
    """A missing heading reads as "no changes of that kind" and an omitted
    Removed section is how a deletion gets shipped unnoticed."""
    t = prompt_template("diff.srs")
    for h in ("## Added", "## Removed", "## Modified", "## Impact Assessment"):
        assert h in t
    assert 'write "none"' in flat("diff.srs")


# ── retired prompts must be gone from BOTH places ─────────────────────

def test_retired_prompts_are_removed_from_the_seed():
    """Four one-line stubs with no call site. Leaving them in the Prompt
    Library advertises a capability that does not exist."""
    for key in ("arch.decompose", "code.generate", "datamodel.optimise", "test.unit"):
        assert f'"key": "{key}"' not in SEED, f"{key} is still seeded"


def test_retired_prompts_are_actively_pruned_from_mongo():
    """`seed_prompts` only inserts and updates. Removing an entry from
    GLOBAL_PROMPTS leaves the row in the database forever, so the Prompt
    Library would keep listing it — the delete has to be explicit."""
    import seed
    assert set(seed.RETIRED_PROMPT_KEYS_19) == {
        "arch.decompose", "code.generate", "datamodel.optimise", "test.unit",
    }
    assert "await prune_retired_prompts_19()" in SEED, "prune is never called"


# Tiers that exist WITHOUT an invoking call site, on purpose. Every entry
# needs a reason, because this list is how a genuine orphan hides.
_TIER_ONLY_BY_DESIGN = {
    # Console renders one row per orchestrator so an operator can pin a model
    # per stage; the orchestration itself is Python, not an LLM call.
    "orchestrator.discovery", "orchestrator.datamodel",
    "orchestrator.architecture", "orchestrator.codegen", "orchestrator.living",
    # iter-14.x replaced these three Architecture sub-stages with the
    # deterministic generators in `arch_deterministic.py`. The tiers stay so a
    # Console row does not vanish from under an operator who had pinned one.
    "arch.hld", "arch.lld", "arch.api_contracts",
    # Super-agents are pure orchestration: they log a run, they do not call a
    # model. `AGENT_LLM_BACKED` in routes/tools.py says so explicitly.
    "tools.transformer.super_agent", "codegen.super_agent",
    # Prompt-library keys whose call sites pass a different agent_key on
    # purpose — the tier belongs to the prompt, not the call.
    "tools.gap_verifier", "tools.gap_analyzer.doc_parser",
    "srs.edit", "srs.diff", "codegen.docs", "codegen.regenerate",
    "datamodel.bus_matrix",
    # iter-22 — `dcte.devops` gained its call site; `dcte.tester` has not.
    # The TESTING phase is real and fully deterministic (subprocess + regex),
    # and two incoming suites assert the registration. See HUMAN_INTERVENTION
    # DT-1: building it or dropping it is an open product decision.
    "dcte.tester",
}

# Keys that ARE invoked, but are assembled at the call site so no literal
# appears anywhere for the scan to find. Each names where, because "the
# regex cannot see it" and "nothing calls it" look identical from here and
# only one of them is a bug.
_ASSEMBLED_AT_CALL_SITE = {
    # routes/codegen.py:9965 — `agent_key = f"codegen.{assigned}"`, where
    # `assigned` comes from `_route_task_to_coder` and is one of these two.
    "codegen.coder_be", "codegen.coder_fe",
    # routes/chat.py — `agent_key=(intent or f"{stage}.chat")`, where `stage`
    # is the LOWERCASED project stage. iter-22 added the three spellings that
    # produces and nobody had declared; `living.chat` and `srs.chat` escape
    # this list only because they also appear as literals in
    # `agent_memory.AGENT_MEMORY` and `models.AgentSession`.
    "discovery.chat", "architecture.chat",
}


def test_no_agent_tier_survives_without_a_call_site():
    """`arch.decompose` held an AGENT_COMPLEXITY tier with no invoking
    code — the same defect shape as the Validator before iter-18, which
    sat seeded and unreachable for two iterations. This catches the next
    one.

    iter-22 — this test could not fail. Its haystack included
    `backend/fabric/`, which contains the AGENT_COMPLEXITY map itself, so
    every key matched its own definition and `orphans` was always []. It had
    been passing since iter-19 while `dcte.devops` and `dcte.tester` sat
    exactly as `arch.decompose` had. The map's own file is now excluded, and
    `backend/dcte/` plus the top-level modules are included — without those
    the real call sites for `dcte.transformer`, `agent_memory.rollover` and
    friends are invisible and the test fails for the wrong reason.
    """
    import re as _re
    from pathlib import Path as _P
    from fabric.model_fabric import AGENT_COMPLEXITY

    backend = _P(__file__).resolve().parent.parent
    tier_map_file = (backend / "fabric" / "model_fabric.py").resolve()
    parts = [
        p.read_text(errors="replace")
        for d in ("routes", "kb", "codegen", "fabric", "dcte", "datamodel",
                  "integrations")
        for p in (backend / d).rglob("*.py")
        # The file that DEFINES the map cannot be evidence that the map's
        # keys are used. This single line is what made the test vacuous.
        if p.resolve() != tier_map_file
    ]
    parts += [
        (backend / f).read_text(errors="replace")
        for f in ("llm.py", "confidence.py", "confidence_langgraph.py",
                  "agent_memory.py", "onboarding.py", "factory_orchestrator.py",
                  "context_bundler.py", "tools_kb_builder.py")
        if (backend / f).is_file()
    ]
    haystack = "\n".join(parts)

    known = _TIER_ONLY_BY_DESIGN | _ASSEMBLED_AT_CALL_SITE
    orphans = [
        k for k in AGENT_COMPLEXITY
        if k not in known
        and not _re.search(r'["\']' + _re.escape(k) + r'["\']', haystack)
    ]
    assert orphans == [], f"agent tiers with no call site: {orphans}"


def test_the_orphan_guard_can_actually_fail():
    """The guard above silently passed for three iterations because it
    searched a haystack containing its own subject. A guard that cannot fail
    is worse than no guard: it reads as coverage. This proves it fails."""
    import re as _re
    from pathlib import Path as _P

    backend = _P(__file__).resolve().parent.parent
    tier_map_file = (backend / "fabric" / "model_fabric.py").resolve()
    haystack = "\n".join(
        p.read_text(errors="replace")
        for d in ("routes", "kb", "codegen", "fabric", "dcte")
        for p in (backend / d).rglob("*.py")
        if p.resolve() != tier_map_file
    )
    assert not _re.search(r'["\']zzz\.invented\.agent["\']', haystack)


# ── every prompt parsed as JSON declares its shape ────────────────────

_JSON_PARSED_AGENTS = [
    "tools.transformer.tester", "tools.transformer.verifier",
    "tools.transformer.planner", "tools.transformer.validator",
    "tools.transformer.devops_audit", "tools.transformer.context_manager",
    "codegen.tester", "codegen.verifier", "codegen.reviewer",
    "codegen.planner", "codegen.context_manager",
]


@pytest.mark.parametrize("key", _JSON_PARSED_AGENTS)
def test_json_agents_declare_an_output_format(key):
    """These are all called with response_format=json_object and their
    replies go through `_extract_json_object`. A prompt with no declared
    shape leaves the parser guessing."""
    t = flat(key)
    assert "output_format" in t or "return only" in t, f"{key} declares no output shape"

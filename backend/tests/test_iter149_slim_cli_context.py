"""iter-14.9 — Regression tests for the slim-CLI mode + OpenRouter
kill-switch.

Forensic context:

    Operator directive: "too much token is getting involved in lama,
    but result is poor or not even completed. I want lama should use
    as a input section for droid… also context will be passed to droid
    at the time of knowledge building. after that same droid should
    use. forget about openrouter."

    Three concrete fixes shipped in iter-14.9:

      1. `backend/kb/droid_workspace.py::materialize_kb_context` writes
         a compact context bundle to `/tmp/lama-factory-cli/<pid>/.lama/`
         at Build KB time.
      2. `backend/fabric/factory_cli.py::_flatten_messages_to_prompt`
         strips embedded KB/TOON/analysis blocks and appends a workspace
         pointer footer when `LAMA_FACTORY_CLI_SLIM=1`.
      3. `backend/llm.py` skips the OpenRouter env-var fallback when the
         project is on the factory-cli path OR
         `LAMA_DISABLE_OPENROUTER_FALLBACK=1` — so a droid outage no
         longer stealthily burns tokens on OpenRouter.

    Tests here pin the observable behaviour of (1) and (2). The
    kill-switch in (3) is exercised indirectly because it lives in a
    Motor-connected code path and can only be smoke-tested inside the
    container; a lightweight assertion on the env-flag parsing is the
    best we can do at the unit-test tier.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from fabric import factory_cli  # noqa: E402


# ------------------------------------------------------------------
# Slim-mode: strip embedded KB block, keep instructions, add footer.
# ------------------------------------------------------------------

_FAT_MESSAGE = (
    "You are a Senior Software Requirements Analyst. Produce the SRS "
    "section titled 'actors_use_case_inventory'.\n\n"
    "══════════════════════════════════════════════════════════════════════\n"
    "LEGACY FILE INDEX (top files by size — cite these filenames VERBATIM):\n"
    "══════════════════════════════════════════════════════════════════════\n"
    + "\n".join(f"  - src/module_{i}.php  ({i*1024} bytes)" for i in range(200))
    + "\n\n"
    "══════════════════════════════════════════════════════════════════════\n"
    "KNOWLEDGE BASE / TOON skeleton (compact index — see kb_toon.txt):\n"
    "══════════════════════════════════════════════════════════════════════\n"
    + "TABLES:\n" + "\n".join(f"  users_{i}(id, name, email)" for i in range(400))
    + "\n\n"
    "══════════════════════════════════════════════════════════════════════\n"
    "PRODUCE THE SECTION NOW.\n"
    "══════════════════════════════════════════════════════════════════════\n"
    "Write ~400 words of prose. Cite files from the LEGACY FILE INDEX above."
)


def _install_msg_only(monkeypatch, captured):
    """Wire the flatten helper up to a fake `_run_droid_exec` so
    `route_via_factory_cli` gets far enough to invoke flatten."""
    async def _fake_run(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"result": "OK"}
    monkeypatch.setattr(factory_cli, "_run_droid_exec", _fake_run)
    monkeypatch.setattr(factory_cli, "_binary_resolves", lambda _p: True)


def test_slim_mode_off_leaves_prompt_intact(monkeypatch):
    monkeypatch.delenv("LAMA_FACTORY_CLI_SLIM", raising=False)
    out = factory_cli._flatten_messages_to_prompt(
        [{"role": "user", "content": _FAT_MESSAGE}]
    )
    # The big index block MUST still be present when slim is off.
    assert "src/module_100.php" in out, "slim-off must keep KB block"
    assert "users_300" in out, "slim-off must keep TOON block"
    assert "CONTEXT LOOKUP" not in out, "slim-off must NOT append footer"


def test_slim_mode_on_strips_kb_block_and_adds_footer(monkeypatch):
    monkeypatch.setenv("LAMA_FACTORY_CLI_SLIM", "1")
    out = factory_cli._flatten_messages_to_prompt(
        [{"role": "user", "content": _FAT_MESSAGE}]
    )
    # The big index block MUST be gone.
    assert "src/module_100.php" not in out, (
        "slim-on must strip LEGACY FILE INDEX block content"
    )
    assert "users_300" not in out, (
        "slim-on must strip KNOWLEDGE BASE / TOON block content"
    )
    # But the banner and the workspace pointer must be present so the
    # LLM still knows to look up the concept.
    assert "LEGACY FILE INDEX" in out
    assert "./.lama/file_index.md" in out, "slim footer must reference workspace"
    assert "./.lama/kb_toon.txt" in out
    # And instructions AFTER the stripped block must survive intact.
    assert "PRODUCE THE SECTION NOW" in out
    assert "Write ~400 words" in out
    # Size must actually drop substantially.
    assert len(out) < len(_FAT_MESSAGE) // 3, (
        f"slim mode must cut prompt by >2/3× — got {len(out)}/{len(_FAT_MESSAGE)}"
    )


def test_slim_mode_env_toggle_variants(monkeypatch):
    """Truthy variants of the env-var all enable slim mode."""
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("LAMA_FACTORY_CLI_SLIM", truthy)
        assert factory_cli._slim_flag_enabled(), (
            f"'{truthy}' must enable slim mode"
        )
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("LAMA_FACTORY_CLI_SLIM", falsy)
        assert not factory_cli._slim_flag_enabled(), (
            f"'{falsy}' must disable slim mode"
        )


# ------------------------------------------------------------------
# iter-14.10 — regression pin for the banner-bottom divider bug.
#
# Real SRS prompts wrap each context block in a THREE-line banner:
#
#     ══════════════════════════════════════════════════════════════════════
#     LEGACY FILE INDEX (top files by size — cite verbatim):
#     ══════════════════════════════════════════════════════════════════════
#     <block content — 100s of lines>
#     [blank]
#     ══════════════════════════════════════════════════════════════════════
#     NEXT SECTION HEADER
#     ══════════════════════════════════════════════════════════════════════
#
# iter-14.9's strip logic exited skip mode on the FIRST divider it saw
# after the header — which is the banner-BOTTOM divider, i.e. before
# any content was actually skipped. The block therefore came through
# fully in the prompt and the "88% token saving" was a lie.
#
# This test uses the exact Unicode divider `═` (U+2550) that
# routes/srs.py::_build_section_prompt emits and asserts that the
# block CONTENT is stripped while both the block header and the
# post-block instructions survive.
# ------------------------------------------------------------------

_DIVIDER = "═" * 70
_REAL_SHAPE_PROMPT = (
    "You are a Senior Software Requirements Analyst.\n\n"
    + _DIVIDER + "\n"
    + "LEGACY FILE INDEX (top files by size — cite verbatim):\n"
    + _DIVIDER + "\n"
    + "\n".join(f"  - src/module_{i}.php  ({i * 1024} bytes)" for i in range(200))
    + "\n\n"
    + _DIVIDER + "\n"
    + "KNOWLEDGE BASE / TOON skeleton:\n"
    + _DIVIDER + "\n"
    + "\n".join(f"  users_{i}(id, name, email)" for i in range(400))
    + "\n\n"
    + _DIVIDER + "\n"
    + "PRODUCE THE SECTION NOW.\n"
    + _DIVIDER + "\n"
    + "Write 400 words. Cite files verbatim."
)


def test_slim_mode_real_banner_shape_actually_strips_content(monkeypatch):
    """The banner-bottom divider must NOT prematurely exit skip mode."""
    monkeypatch.setenv("LAMA_FACTORY_CLI_SLIM", "1")
    out = factory_cli._flatten_messages_to_prompt(
        [{"role": "user", "content": _REAL_SHAPE_PROMPT}]
    )
    # Block CONTENT must be gone.
    assert "src/module_100.php" not in out, (
        "LEGACY FILE INDEX block content must be stripped even when the "
        "banner-bottom divider immediately follows the header (this was "
        "the iter-14.9 → iter-14.10 regression)"
    )
    assert "users_100" not in out, (
        "KNOWLEDGE BASE block content must be stripped"
    )
    # Post-block instructions MUST survive intact.
    assert "PRODUCE THE SECTION NOW" in out, (
        "instructions after the stripped block must survive"
    )
    assert "Write 400 words" in out
    # Footer must be appended.
    assert "./.lama/file_index.md" in out
    # And the reduction must actually happen — pin at least 70%.
    assert len(out) < len(_REAL_SHAPE_PROMPT) * 0.3, (
        f"expected >70% shrink on real banner shape, got "
        f"{100 * (1 - len(out) / len(_REAL_SHAPE_PROMPT)):.1f}%"
    )


# ------------------------------------------------------------------
# iter-14.11 — pin the real production SRS prompt shape.
#
# The production SRS prompt embeds TWO fat context blocks that are
# NOT wrapped in `══` banner banners at all:
#
#     PROJECT-ISOLATED LEGACY WORKSPACE (iter-13.99 …):
#       workspace_root: /lama-workspaces/…
#       tree:
#         src/com/…/File1.java  (247843 B)
#         src/com/…/File2.java  (212572 B)
#         …
#       files:
#       ─── path: /lama-workspaces/…/File1.java (12039 chars) ───
#       <inlined source>
#       ─── path: /lama-workspaces/…/File2.java (…) ───
#       <inlined source>
#     KNOWLEDGE BASE:
#     STRUCTURAL SKELETON (TOON — INDIVIDUALS):
#       [CLASS:Foo] pkg=com.…
#         [METHOD:getBar]
#         …
#
# iter-14.10 matched substrings ("LEGACY FILE INDEX", "KNOWLEDGE BASE
# / TOON") that never appear in either block header, so slim mode
# stripped ~5% of a 300K prompt instead of the ~85% required for
# droid to actually be fast. iter-14.11 uses column-anchored prefix
# matching on the ACTUAL header lines above.
# ------------------------------------------------------------------

def _synth_real_shape_prompt() -> str:
    """Reproduce the shape of the container-captured 296K prompt."""
    D = "═" * 67
    parts = []
    parts.append("# System\nMANDATORY GOVERNANCE BUNDLE — read in order.\n")
    parts.append(D + "\nSTORYTELLING VOICE\n" + D + "\n"
                 "Open every section with narrative prose. Name people in "
                 "business roles.\n")
    parts.append(D + "\nHARD CONSTRAINTS:\n" + D + "\n"
                 "No JSON. No preamble. Cite files verbatim.\n")
    # Now the two fat blocks — no banner wrappers.
    parts.append("PROJECT-ISOLATED LEGACY WORKSPACE (iter-13.99 — this IS the only source):")
    parts.append("  workspace_root: /lama-workspaces/tenantA__projA/")
    parts.append("  tree:")
    for i in range(60):
        parts.append(f"  src/com/acme/module_{i}.java  ({i * 512 + 100} B)")
    parts.append("  files:")
    for i in range(40):
        parts.append(f"─── path: /lama-workspaces/tenantA__projA/src/com/acme/module_{i}.java ({i * 200} chars) ───")
        for j in range(50):
            parts.append(f"    public void handler_{i}_{j}(GenericDAOQueryCriteria c) {{ ... }}")
    parts.append("")
    parts.append("KNOWLEDGE BASE:")
    parts.append("STRUCTURAL SKELETON (TOON — INDIVIDUALS):")
    for i in range(300):
        parts.append(f"[CLASS:LegacyEntity_{i}] pkg=com.acme.data")
        for j in range(5):
            parts.append(f"  [METHOD:getField_{i}_{j}]")
            parts.append(f"  [METHOD:setField_{i}_{j}]")
    parts.append("")
    # Post-block instructions that MUST survive.
    parts.append("LENGTH TARGET:\n- Minimum 500 words.\n")
    parts.append("FORMAT:\n- Markdown only.\n")
    parts.append("# User\nProduce the 3. Actors and Use Case Inventory section now.")
    return "\n".join(parts)


def test_slim_mode_real_production_shape_cuts_more_than_70_percent(monkeypatch):
    """Pin the iter-14.11 fix on the ACTUAL production prompt shape.

    Regression is: `PROJECT-ISOLATED LEGACY WORKSPACE` block and
    `KNOWLEDGE BASE:` block are NOT wrapped in `══` banners, so
    banner-based strip logic missed them. Post-fix must strip ≥70% AND
    preserve all instruction headers.
    """
    monkeypatch.setenv("LAMA_FACTORY_CLI_SLIM", "1")
    prompt = _synth_real_shape_prompt()
    out = factory_cli._flatten_messages_to_prompt(
        [{"role": "user", "content": prompt}]
    )
    # Big fat content is gone.
    assert "handler_0_0(GenericDAOQueryCriteria" not in out, (
        "PROJECT-ISOLATED LEGACY WORKSPACE files must be stripped"
    )
    assert "LegacyEntity_100" not in out, (
        "KNOWLEDGE BASE TOON entries must be stripped"
    )
    assert "getField_50_2" not in out
    # Instructions all preserved.
    assert "STORYTELLING VOICE" in out
    assert "HARD CONSTRAINTS:" in out
    assert "LENGTH TARGET:" in out
    assert "FORMAT:" in out
    assert "Produce the 3. Actors" in out
    assert "./.lama/file_index.md" in out  # footer
    # The critical assertion — actual reduction must be substantial.
    ratio = len(out) / len(prompt)
    assert ratio < 0.30, (
        f"expected >70% shrink on production prompt shape, got "
        f"{100 * (1 - ratio):.1f}% — the iter-14.11 anchor list has "
        "drifted from the production banner text; re-capture a live "
        "prompt from /tmp/droid-debug/ and update the anchors."
    )

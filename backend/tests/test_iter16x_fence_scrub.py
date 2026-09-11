"""
iter-16.x — user reported that generated .java files still contained
literal ```java opening fences and stray ``` closers even though
`_strip_llm_code_wrapper` had already shipped. Root cause: the LLM
sometimes emits a NESTED ``` block inside a Javadoc example, which
confuses `_strip_llm_code_wrapper`'s non-greedy fence regex — the
"longest match" it picks truncates at the inner fence pair and the
OUTER opener/closer end up back in the "cleaned" content. Also the
`_run_compile_fix_loop._run_one` path persisted Coder output straight
to `transform_files.content` without the defense-in-depth scrub that
export-time uses, so once a fence leaked in it stayed there for every
subsequent fix iteration and produced a "illegal character: '`'"
cascade that the loop could not converge on.

Fix (this iteration):
  1. New `_scrub_code_fences` helper — idempotent leading/trailing/
     stray-line ``` remover. Runs on Coder output in BOTH `_run_coder`
     AND `_run_compile_fix_loop._run_one` before Mongo persistence.
  2. `_sanitize_exported_file_content` now always kills stray fence
     lines for code files, not only when the preamble heuristic
     flagged the content as contaminated.
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes import tools as tools_mod  # noqa: E402


# ─────────────── _scrub_code_fences ───────────────

def test_scrub_removes_leading_java_fence():
    raw = "```java\npackage com.shpp;\npublic class Foo {}\n"
    out = tools_mod._scrub_code_fences(raw)
    assert out.startswith("package com.shpp;")
    assert "```" not in out


def test_scrub_removes_trailing_fence():
    raw = "package com.shpp;\npublic class Foo {}\n```\n"
    out = tools_mod._scrub_code_fences(raw)
    assert "```" not in out
    assert out.rstrip().endswith("}")


def test_scrub_removes_leading_and_trailing_fence_pair():
    raw = "```java\npackage com.shpp;\npublic class Foo {}\n```\n"
    out = tools_mod._scrub_code_fences(raw)
    assert out.startswith("package com.shpp;")
    assert "```" not in out


def test_scrub_removes_stray_midfile_fence_lines():
    # A nested-javadoc-example case that survived `_strip_llm_code_wrapper`.
    raw = (
        "package com.shpp;\n"
        "/**\n"
        " * Example:\n"
        "```\n"
        " * switch(x) { case 1 -> \"a\"; }\n"
        "```\n"
        " */\n"
        "public class Foo {}\n"
    )
    out = tools_mod._scrub_code_fences(raw)
    assert "```" not in out
    assert "package com.shpp;" in out
    assert "public class Foo {}" in out


def test_scrub_is_idempotent_on_clean_content():
    raw = "package com.shpp;\npublic class Foo {}\n"
    assert tools_mod._scrub_code_fences(raw) == raw
    # And a second pass yields the same output.
    once = tools_mod._scrub_code_fences(raw)
    twice = tools_mod._scrub_code_fences(once)
    assert once == twice


def test_scrub_no_op_on_empty():
    assert tools_mod._scrub_code_fences("") == ""
    assert tools_mod._scrub_code_fences(None) is None


# ─────────────── _sanitize_exported_file_content always strips stray fences on code ───────────────

def test_sanitize_strips_stray_fence_even_without_preamble_marker():
    """A file that does NOT trip the `looks_contaminated` heuristic
    (starts with valid code, no chat-preamble, no leading fence) but
    has a stray full-line ``` mid-file must still be scrubbed — a
    backtick on-disk is a guaranteed javac error and previously
    survived this path because the sanitizer only ran the stray-line
    scrub inside the `looks_contaminated` branch.

    Repro of the real failure: a Javadoc example fence leaked through
    `_run_coder._extract_code` (because `_strip_llm_code_wrapper`'s
    non-greedy regex picked the inner fence pair as "the block") and
    then `_sanitize_exported_file_content` was called on export — but
    because the file started with `package ...` and had no LLM
    preamble, `looks_contaminated` was False and the fence stayed."""
    contaminated = (
        "package com.shpp.configuration;\n"
        "public class MaskingRequestFilter {\n"
        "    // Example use:\n"
        "```\n"
        "    public void doFilter() {}\n"
        "```\n"
        "}\n"
    )
    cleaned = tools_mod._sanitize_exported_file_content(
        "src/main/java/com/shpp/configuration/MaskingRequestFilter.java",
        contaminated,
    )
    assert "```" not in cleaned
    assert "package com.shpp.configuration;" in cleaned
    assert "public class MaskingRequestFilter" in cleaned


def test_sanitize_preserves_non_code_files_with_backticks():
    """Markdown / docs legitimately use ``` — must not be scrubbed."""
    md = "# Title\n\n```java\nSystem.out.println(\"hi\");\n```\n"
    out = tools_mod._sanitize_exported_file_content("README.md", md)
    assert "```java" in out
    assert "```" in out


def test_sanitize_still_normalises_smart_quotes():
    """Regression guard — smart-quote normalisation from iter-15.69
    must still run for code files."""
    contaminated = "package com.shpp;\nString x = \u201Chello\u201D;\n"
    out = tools_mod._sanitize_exported_file_content("Foo.java", contaminated)
    assert "\u201C" not in out and "\u201D" not in out
    assert '"hello"' in out

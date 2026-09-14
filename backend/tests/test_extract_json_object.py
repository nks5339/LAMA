"""`_extract_json_object` must survive a fenced JSON block.

Found while benchmarking a local model through the real fabric. Asked to
score a generated file, `qwen2.5-coder:7b` replied — six times out of six —
with exactly this:

    ```json
    {"verdict": "ACCEPT", "confidence": 100, "issues": []}
    ```

and `routes/codegen.py::_extract_json_object` returned None for every one.

The fence-stripping preamble did:

    s.split("```", 2)[-1]

On a correctly fenced block that yields the empty string that follows the
CLOSING fence, not the content between the fences:

    '```json\\n{...}\\n```'.split('```', 2)
        -> ['', 'json\\n{...}\\n', '']      and [-1] is ''

so the brace scan that follows had nothing to scan.

Consequences in the multi-agent CodeGen pipeline, all silent:

  * verifier           -> parsed {} -> confidence 0.0, verdict REJECT
                          -> the task is marked VERIFY_FAILED and a
                             perfectly good file is reported as bad
  * reviewer / tester   -> empty details, no issues surfaced
  * traceability gate   -> LLM summary dropped (the deterministic
                          fallback saves the number, so this one
                          degrades rather than breaks)

`routes/tools.py` has a separate implementation of the same helper which
handles fences correctly, so the Transformer pipeline was unaffected. The
two are catalogued as duplicates in docs/RECON.md; they stay separate per
the ground rules, but both are pinned here so they cannot diverge on this
behaviour again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes.codegen import _extract_json_object as codegen_extract  # noqa: E402
from routes.tools import _extract_json_object as tools_extract  # noqa: E402

_IMPLS = pytest.mark.parametrize(
    "extract",
    [pytest.param(codegen_extract, id="codegen"),
     pytest.param(tools_extract, id="tools")],
)

EXPECTED = {"verdict": "ACCEPT", "confidence": 97}


@_IMPLS
def test_fenced_with_language_tag(extract):
    """The exact shape a local model emits. This is the regression."""
    raw = '```json\n{"verdict": "ACCEPT", "confidence": 97}\n```'
    assert extract(raw) == EXPECTED


@_IMPLS
def test_fenced_without_language_tag(extract):
    raw = '```\n{"verdict": "ACCEPT", "confidence": 97}\n```'
    assert extract(raw) == EXPECTED


@_IMPLS
def test_bare_object(extract):
    assert extract('{"verdict": "ACCEPT", "confidence": 97}') == EXPECTED


@_IMPLS
def test_prose_then_fenced_block(extract):
    raw = ('Sure! Here is the verdict:\n\n'
           '```json\n{"verdict": "ACCEPT", "confidence": 97}\n```\n\n'
           'Let me know if you need anything else.')
    assert extract(raw) == EXPECTED


@_IMPLS
def test_unterminated_fence_from_a_max_tokens_cutoff(extract):
    """Truncation mid-fence is common when the budget is tight."""
    raw = '```json\n{"verdict": "ACCEPT", "confidence": 97}\n'
    assert extract(raw) == EXPECTED


@_IMPLS
def test_nested_objects_survive(extract):
    raw = ('```json\n'
           '{"verdict": "REJECT", "checks": {"imports": false, "annotations": true}}\n'
           '```')
    out = extract(raw)
    assert out["verdict"] == "REJECT"
    assert out["checks"]["imports"] is False


@_IMPLS
def test_braces_inside_string_values_survive(extract):
    raw = '```json\n{"verdict": "REJECT", "issues": ["missing { brace in body"]}\n```'
    out = extract(raw)
    assert out["issues"] == ["missing { brace in body"]


@_IMPLS
@pytest.mark.parametrize("raw", ["", "   ", "no json here at all", "```json\n```"])
def test_unparseable_input_returns_none(extract, raw):
    """Returning None rather than raising is the contract callers rely on."""
    assert extract(raw) is None

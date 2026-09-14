"""A provider row must not hand out key material.

Found by calling `GET /api/console/providers` with no bearer token during the
deep test. For a real 32-character Azure key the response contained:

    api_key            'yuATf0...p6pG'      6 leading + 4 trailing
    detected_from_key  'yuATf0sx...'        8 leading, NOT masked at all

Together that is 12 of the 32 characters — positions 0-7 and 28-31 — plus the
full endpoint URL and the deployment name.

`_serialize_provider` masked `api_key` and then forgot `detected_from_key`,
which `setup_default_provider` populates as ``api_key[:8] + "..."``. A field
whose only job is to say *which* key a row came from was storing and emitting
raw key material.

Twelve characters will not let anyone brute-force the key. It is still wrong:
it is enough to CONFIRM a key someone already holds, it survives into logs and
screenshots, and there is no reason for any caller to receive it. The standard
practice everyone else follows is a trailing fragment only.
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

from routes.console import _mask_key, _serialize_provider  # noqa: E402

# Shape-accurate, never-real sample keys.
AZURE_KEY = "AbCdEf01GhIjKl23MnOpQr45StUvWx67"          # 32 chars, no prefix
ANTHROPIC_KEY = "sk-ant-api03-" + "Z" * 40
OPENROUTER_KEY = "sk-or-v1-" + "Q" * 48


@pytest.mark.parametrize("key", [AZURE_KEY, ANTHROPIC_KEY, OPENROUTER_KEY])
def test_mask_reveals_at_most_a_trailing_fragment(key: str):
    masked = _mask_key(key)

    revealed = sum(1 for ch in masked if ch != "*" and ch != ".")
    assert revealed <= 4, (
        f"{masked!r} reveals {revealed} characters of the key; at most a "
        f"4-character trailing fragment is acceptable"
    )


@pytest.mark.parametrize("key", [AZURE_KEY, ANTHROPIC_KEY, OPENROUTER_KEY])
def test_mask_never_reveals_the_leading_characters(key: str):
    """The regression. A 6-character prefix was being emitted."""
    masked = _mask_key(key)
    assert key[:6] not in masked, (
        f"{masked!r} still contains the first 6 characters of the key"
    )


def test_short_or_empty_keys_are_fully_hidden():
    assert _mask_key("") == ""
    for k in ("abc", "1234567"):
        assert set(_mask_key(k)) <= {"*"}, _mask_key(k)


# ── the serialized row is what actually goes over the wire ────────────

def _row(**over):
    base = {
        "id": "p1", "name": "Azure (test)", "provider_type": "azure",
        "base_url": "https://example.openai.azure.com",
        "api_key": AZURE_KEY,
        "detected_from_key": AZURE_KEY[:8] + "...",
        "azure_deployment": "gpt-5.1",
        "is_default": True, "is_active": True,
    }
    base.update(over)
    return base


def test_serialized_row_contains_no_contiguous_run_of_the_key():
    """Nothing in the response may carry a recognisable slice of the key.

    Checked as a substring scan over the whole serialized row rather than
    field by field, so a future field that happens to embed the key is
    caught too.
    """
    import json
    blob = json.dumps(_serialize_provider(_row()))

    for size in (6, 8):
        for i in range(0, len(AZURE_KEY) - size + 1):
            frag = AZURE_KEY[i:i + size]
            assert frag not in blob, (
                f"serialized provider leaks the {size}-char fragment "
                f"{frag!r} (key offset {i})"
            )


def test_detected_from_key_is_masked_too():
    """The field that was forgotten."""
    out = _serialize_provider(_row())
    dfk = str(out.get("detected_from_key", ""))

    assert AZURE_KEY[:8] not in dfk, (
        f"detected_from_key={dfk!r} still carries 8 raw characters of the key"
    )


def test_serialization_still_identifies_the_provider():
    """Masking must not make the row useless to an operator.

    They still need to tell two configured providers apart, so everything
    that is not key material stays.
    """
    out = _serialize_provider(_row())
    assert out["name"] == "Azure (test)"
    assert out["provider_type"] == "azure"
    assert out["base_url"] == "https://example.openai.azure.com"
    assert out["azure_deployment"] == "gpt-5.1"
    assert out["is_default"] is True


def test_a_missing_key_does_not_crash_serialization():
    out = _serialize_provider(_row(api_key="", detected_from_key=""))
    assert out["api_key"] == ""

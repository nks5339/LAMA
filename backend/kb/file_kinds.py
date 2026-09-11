"""Source-file kind taxonomy (iter-13.69).

Every KB file now carries a `kind` so downstream stages know HOW to use it:

* `legacy_code`    — actual legacy source (PHP / Java / .NET / Python …)
* `legacy_db`      — DB DDL / schema dump / SQL migrations
* `existing_srs`   — prior IEEE-style SRS / BRD / FRD document
* `business_doc`   — process notes, policy PDFs, user manuals
* `figma_export`   — Figma JSON export / design-token JSON
* `design_mockup`  — image / PDF wireframe or screen mockup
* `api_spec`       — OpenAPI / Swagger / Postman / WSDL
* `test_artifact`  — UAT scripts, test plans, recorded scenarios
* `other`          — uncategorised

The kind is set by the user at upload-time (or per-file later) and is fed
into:
  • SRS prompts as the "SOURCE MATERIAL INVENTORY" block — the LLM is told
    what reference docs exist and that it MAY cite them.
  • CodeGen `codegen.frontend` prompt as the `{theme_brief}` block — UI
    files are generated with the user-supplied Figma / mockup theme honoured.

This file is the single source of truth for the taxonomy; both the
backend (routes/kb.py, routes/srs.py, routes/codegen.py) and the frontend
(`lib/api.js::FILE_KINDS`) must stay in sync.
"""
from __future__ import annotations

import os
from typing import Optional


KIND_LEGACY_CODE = "legacy_code"
KIND_LEGACY_DB = "legacy_db"
KIND_EXISTING_SRS = "existing_srs"
KIND_BUSINESS_DOC = "business_doc"
KIND_FIGMA_EXPORT = "figma_export"
KIND_DESIGN_MOCKUP = "design_mockup"
KIND_API_SPEC = "api_spec"
KIND_TEST_ARTIFACT = "test_artifact"
KIND_OTHER = "other"


# Public ordered list — frontend dropdowns iterate this for stable order.
FILE_KINDS: list[dict] = [
    {"value": KIND_LEGACY_CODE,    "label": "Legacy code",
     "hint": "PHP / Java / .NET / Python / JS / etc. (default)."},
    {"value": KIND_LEGACY_DB,      "label": "Legacy DB / schema",
     "hint": "SQL DDL, schema dumps, stored-procedure scripts."},
    {"value": KIND_EXISTING_SRS,   "label": "Existing SRS / BRD",
     "hint": "Prior requirements doc — LAMA cites it in the new SRS."},
    {"value": KIND_BUSINESS_DOC,   "label": "Business / process doc",
     "hint": "Policy notes, SOPs, user manuals, training material."},
    {"value": KIND_FIGMA_EXPORT,   "label": "Figma export / design tokens",
     "hint": "Figma JSON / design-token JSON — FE codegen follows this theme."},
    {"value": KIND_DESIGN_MOCKUP,  "label": "Design mockup / wireframe",
     "hint": "Screen mockup PDFs / images — FE codegen mirrors the layout."},
    {"value": KIND_API_SPEC,       "label": "API spec (OpenAPI / Postman)",
     "hint": "OpenAPI / Swagger / Postman / WSDL — Stage 3 uses verbatim."},
    {"value": KIND_TEST_ARTIFACT,  "label": "Test artifact / UAT script",
     "hint": "Test plans / recorded scenarios — Stage 5 uses for parity."},
    {"value": KIND_OTHER,          "label": "Other",
     "hint": "Anything else — kept for context but not specially treated."},
]
VALID_KINDS: set[str] = {k["value"] for k in FILE_KINDS}


# Extensions that should never be auto-tagged as legacy_code regardless of
# their MIME / filetype. (PDFs and Word docs are almost always docs, not code.)
_DOC_EXTS = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".md", ".txt"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
_API_HINT_NAMES = ("openapi", "swagger", "postman_collection", "wsdl", "asyncapi")
_FIGMA_HINT_NAMES = ("figma", "design-tokens", "design_tokens", "tokens.json")
_SRS_HINT_NAMES = ("srs", "brd", "frd", "requirement", "specification")
_TEST_HINT_NAMES = ("uat", "test-plan", "test_plan", "test_cases", "testcases")


def normalize_kind(value: Optional[str], default: str = KIND_LEGACY_CODE) -> str:
    """Coerce any user input to a valid kind, falling back to `default`."""
    v = (value or "").strip().lower()
    return v if v in VALID_KINDS else default


def detect_kind(filename: str, filetype: str = "") -> str:
    """Best-effort auto-detect of a file's kind from its name + filetype.

    Used as the DEFAULT when the user did not explicitly pick a kind for
    a file. The user can still override on a per-file basis.
    """
    name = (filename or "").lower()
    ext = os.path.splitext(name)[1]
    base = os.path.basename(name)

    if ext == ".sql" or filetype == "sql":
        return KIND_LEGACY_DB
    if any(h in base for h in _FIGMA_HINT_NAMES):
        return KIND_FIGMA_EXPORT
    if any(h in base for h in _API_HINT_NAMES):
        return KIND_API_SPEC
    if any(h in base for h in _SRS_HINT_NAMES):
        return KIND_EXISTING_SRS
    if any(h in base for h in _TEST_HINT_NAMES):
        return KIND_TEST_ARTIFACT
    if ext in _IMAGE_EXTS:
        return KIND_DESIGN_MOCKUP
    if ext in _DOC_EXTS:
        # PDFs / docx default to business_doc — user can re-tag as
        # existing_srs / design_mockup if needed.
        return KIND_BUSINESS_DOC
    return KIND_LEGACY_CODE


def kind_label(value: str) -> str:
    for k in FILE_KINDS:
        if k["value"] == value:
            return k["label"]
    return value or "—"


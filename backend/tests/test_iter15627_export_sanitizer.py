"""
iter-15.62.7 — user reported two related symptoms after downloading a
generated project:

  1. "why is this http:// path coming" in the generated pom.xml — this
     turned out to be a non-issue: `http://maven.apache.org/POM/4.0.0`,
     `http://www.w3.org/2001/XMLSchema-instance` and
     `http://maven.apache.org/xsd/maven-4.0.0.xsd` are the MANDATORY XML
     namespace/schema-location declarations required by the Maven POM
     4.0.0 XSD — every valid pom.xml (hand-written or generated) has
     these exact strings. Nothing to fix.

  2. "why generated project is not getting load properly in intelij" —
     THIS was a real bug. Live-data inspection found `transform_files`
     records (created before iter-15.56/iter-15.57 shipped) whose
     `content` still had the raw LLM chat wrapper baked in, e.g.:
       "Here is the transformed code from Java 17 to Java 21:\n\n```xml\n<?xml ..."
       "Based on the provided code and transformation rules, I will ...\n```\n<?xml ..."
     That is not valid XML, so IntelliJ's Maven importer fails to parse
     the POM at all and the project never loads as a Maven project.
     `_run_coder`'s `_extract_code` (iter-15.56) already prevents this
     for NEW transformations, and the iter-15.57 structural gate already
     flags contaminated output as REJECT/`compilable: False` — but
     neither of those retroactively cleans already-stored legacy records,
     and nothing re-checked content at export time. Fix: a shared
     `_strip_llm_code_wrapper` (used by both `_run_coder` AND export) plus
     a defense-in-depth `_sanitize_exported_file_content` applied right
     before a file leaves LAMA via ZIP download or GitHub push.
"""
import asyncio
import os
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes import tools as tools_mod  # noqa: E402


# ─────────────── the http:// "false alarm" ───────────────
def test_maven_pom_namespace_urls_are_mandatory_boilerplate():
    """Documents the direct answer to question 1: these are the
    required Maven POM 4.0.0 XSD namespace/schema-location URLs, present
    in every valid pom.xml — not something LAMA injects incorrectly."""
    pom = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
        'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
        "</project>\n"
    )
    # A sanitize pass must be a no-op on a well-formed pom.xml — these
    # namespace URLs are exactly what a valid Maven POM must contain.
    assert tools_mod._sanitize_exported_file_content("pom.xml", pom) == pom


# ─────────────── _strip_llm_code_wrapper ───────────────
def test_strip_llm_code_wrapper_removes_preamble_and_fence():
    raw = (
        "Here is the transformed code from Java 17 to Java 21:\n\n"
        '```xml\n<?xml version="1.0"?>\n<project>ok</project>\n```'
    )
    out = tools_mod._strip_llm_code_wrapper(raw)
    assert out.startswith('<?xml version="1.0"?>')
    assert "```" not in out
    assert "Here is the transformed code" not in out


def test_strip_llm_code_wrapper_handles_unclosed_leading_fence_only():
    """Reproduces one of the exact live-data shapes found:
    content that starts with a bare ``` (no language tag), no
    matching closing fence anywhere."""
    raw = '```\n<?xml version="1.0" encoding="UTF-8"?>\n<project>x</project>\n'
    out = tools_mod._strip_llm_code_wrapper(raw)
    assert out.startswith('<?xml version="1.0"')
    assert "```" not in out


def test_strip_llm_code_wrapper_handles_based_on_preamble():
    """Reproduces the other exact live-data shape found: "Based on the
    provided code and transformation rules, I will ..." with no fence."""
    raw = (
        "Based on the provided code and transformation rules, I will "
        "update the pom.xml as follows:\n"
        '<?xml version="1.0" encoding="UTF-8"?>\n<project>x</project>\n'
    )
    out = tools_mod._strip_llm_code_wrapper(raw)
    assert out.startswith('<?xml version="1.0"')


def test_strip_llm_code_wrapper_noop_on_already_clean_content():
    raw = '<?xml version="1.0" encoding="UTF-8"?>\n<project>clean</project>\n'
    assert tools_mod._strip_llm_code_wrapper(raw) == raw.strip()


# ─────────────── _sanitize_exported_file_content ───────────────
def test_sanitize_exported_file_content_cleans_contaminated_pom():
    raw = (
        "Here is the transformed code from Java 17 to Java 21:\n\n"
        '```xml\n<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"></project>\n```'
    )
    out = tools_mod._sanitize_exported_file_content("helidon-quickstart/pom.xml", raw)
    assert out.startswith('<?xml version="1.0"')
    assert "Here is the transformed code" not in out
    assert "```" not in out


def test_sanitize_exported_file_content_is_noop_on_clean_content():
    raw = '<?xml version="1.0" encoding="UTF-8"?>\n<project>clean</project>\n'
    assert tools_mod._sanitize_exported_file_content("pom.xml", raw) == raw


def test_sanitize_exported_file_content_skips_markdown_files():
    """A README.md legitimately starting with a fenced code sample (or
    prose) must NOT be mangled — only source/config files are sanitized."""
    raw = "Here is how to run this project:\n\n```bash\nmvn clean install\n```\n"
    assert tools_mod._sanitize_exported_file_content("README.md", raw) == raw


def test_sanitize_exported_file_content_handles_empty_content():
    assert tools_mod._sanitize_exported_file_content("pom.xml", "") == ""
    assert tools_mod._sanitize_exported_file_content("pom.xml", None) is None


# ─────────────── ZIP download applies the sanitizer ───────────────
def test_download_transformed_code_sanitizes_legacy_contaminated_records(monkeypatch):
    """End-to-end check of the actual reported symptom: a LEGACY
    (pre-fix) `transform_files` record with the LLM wrapper baked in
    must come out of the downloaded ZIP as clean, valid XML."""
    import io
    import zipfile

    contaminated_pom = (
        "Here is the transformed code from Java 17 to Java 21:\n\n"
        '```xml\n<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion></project>\n```"
    )

    async def _fake_find_one(*a, **kw):
        return {"_id": "tx-1", "name": "demo-project"}

    class _FakeCursor:
        async def to_list(self, n):
            return [{"path": "helidon-quickstart/pom.xml", "content": contaminated_pom}]

    def _fake_find(*a, **kw):
        return _FakeCursor()

    monkeypatch.setattr(tools_mod.transformations, "find_one", _fake_find_one)
    monkeypatch.setattr(tools_mod.transform_files, "find", _fake_find)

    resp = asyncio.run(tools_mod.download_transformed_code("tx-1", scope="code"))

    async def _read_body():
        return b"".join([chunk async for chunk in resp.body_iterator])

    zip_bytes = asyncio.run(_read_body())

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    content = zf.read("helidon-quickstart/pom.xml").decode()
    assert content.startswith('<?xml version="1.0"')
    assert "Here is the transformed code" not in content
    assert "```" not in content


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

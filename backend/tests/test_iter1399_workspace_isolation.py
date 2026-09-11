"""iter-13.99 — Project-isolated workspace + hard-prefix guard.

Forensic context (do not delete):

    Despite iters 13.90 / 13.92 / 13.94 / 13.95 / 13.98 layering ever-
    stronger sanitizers, TJHS SRS was STILL leaking PMIS paths:

        src/com/ahct/medicalAudit/service/MedicalAuditSchedular.java:56

    Root cause class — ALL of the prior sanitizers were SUFFIX matchers.
    When the candidate path's basename (or 2-component suffix) happened
    to collide with something in TJHS's own kb_files, the foreign path
    survived. With ~700 files in TJHS, collisions on `LoginAction.java`,
    `MedicalAuditSchedular.java`, `*.properties` etc. are inevitable.

    User-asked structural fix (iter-13.99):
        "create a collection based on {project_id}_{tenant_id} … store
         them in a droid location with the same name … now in every
         srs/architechture/code gen etc. they should refer the same
         project's input"

    Fix shipped:
      1. Every project gets a unique pseudo-path
         `/lama-workspaces/{tenant_id}__{project_id}` (iter-13.99).
      2. The SRS prompt declares THIS path as the SOLE legitimate
         file-citation prefix. Examples of valid / invalid citations
         are spelled out explicitly.
      3. The LEGACY CODEBASE LOCATION + FACTORY DROID FILESYSTEM
         hint blocks are REMOVED whenever the inline workspace is
         present — the model is no longer told there's a real
         filesystem worth reading.
      4. New HARD GUARD `_enforce_workspace_prefix` runs over the
         LLM's output and redacts ANY multi-component path that
         doesn't start with the workspace root. No more suffix
         collisions can leak.
      5. Endpoint `GET /api/kb/{pid}/workspace-info` returns the
         per-project workspace root so operators can verify.
      6. Startup banner prints the iter version so cached old
         containers can't silently keep running.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_workspace_root_uses_tid_pid_naming():
    """The pseudo-path must be `/lama-workspaces/{tid}__{pid}` exactly —
    this string is referenced by the hard guard, the SRS prompt, and
    the operator-facing /workspace-info endpoint."""
    from routes.srs import _workspace_root
    assert _workspace_root("tenantA", "projB") == "/lama-workspaces/tenantA__projB"


def test_workspace_root_handles_empty_inputs():
    """Empty tenant/project must fall back to `default` so the path
    is always well-formed (no `//` or trailing separator)."""
    from routes.srs import _workspace_root
    assert _workspace_root("", "") == "/lama-workspaces/default__default"
    assert _workspace_root("t", "") == "/lama-workspaces/t__default"
    assert _workspace_root("", "p") == "/lama-workspaces/default__p"


def test_is_workspace_path_recognises_prefix():
    from routes.srs import _is_workspace_path
    root = "/lama-workspaces/tenA__projB"
    assert _is_workspace_path(f"{root}/foo/bar.java", root)
    assert _is_workspace_path(f"{root}/foo/bar.java:42", root.rstrip("/"))
    # Case + separator insensitivity (Mongo filenames may be either).
    assert _is_workspace_path(f"{root.upper()}/Foo.java", root)
    # Foreign prefix MUST be rejected.
    assert not _is_workspace_path("src/com/ahct/Foo.java", root)
    assert not _is_workspace_path("/lama-workspaces/other__other/Foo.java", root)
    assert not _is_workspace_path("", root)
    assert not _is_workspace_path("/lama-workspaces/tenA__projB-prefix/Foo.java", root)


def test_enforce_workspace_prefix_blocks_basename_collision_leak():
    """The user's exact leak: TJHS has its own `LoginAction.java` so
    suffix-matching let `src/com/ahct/login/.../LoginAction.java`
    through. The hard prefix guard MUST reject it because the prefix
    doesn't match the workspace root."""
    from routes.srs import (
        _enforce_workspace_prefix, _workspace_root, _allowed_basenames,
    )
    root = _workspace_root("tjhsT", "tjhsP")
    bn = _allowed_basenames(["src/com/tjhs/login/LoginAction.java"])
    text = "see src/com/ahct/login/controller/LoginAction.java:42 for the rule."
    out, n = _enforce_workspace_prefix(text, root, bn)
    assert n == 1, f"hard guard didn't redact the foreign path: {out!r}"
    assert "ahct" not in out
    assert "[redacted-foreign-path]" in out


def test_enforce_workspace_prefix_blocks_user_reported_leak():
    """The exact user-reported leak text from the iter-13.99 ticket."""
    from routes.srs import _enforce_workspace_prefix, _workspace_root, _allowed_basenames
    root = _workspace_root("tjhsT", "tjhsP")
    # TJHS has a service dir + medical files but NOT under com/ahct.
    bn = _allowed_basenames([
        "src/com/tjhs/medical/service/MedicalService.java",
        "src/com/tjhs/login/LoginAction.java",
    ])
    out, n = _enforce_workspace_prefix(
        "see src/com/ahct/medicalAudit/service/MedicalAuditSchedular.java:56",
        root, bn,
    )
    assert n == 1
    assert "ahct" not in out
    assert "MedicalAuditSchedular" not in out


def test_enforce_workspace_prefix_accepts_legitimate_workspace_paths():
    """Citations rooted at the workspace root MUST survive."""
    from routes.srs import _enforce_workspace_prefix, _workspace_root, _allowed_basenames
    root = _workspace_root("tjhsT", "tjhsP")
    bn = _allowed_basenames(["src/com/tjhs/login/LoginAction.java"])
    text = (
        f"see {root}/src/com/tjhs/login/LoginAction.java:42 for the rule. "
        f"Also {root}/properties/app.properties:18."
    )
    out, n = _enforce_workspace_prefix(text, root, bn)
    assert n == 0, f"legitimate workspace paths were wrongly redacted: {out!r}"
    assert out == text


def test_enforce_workspace_prefix_accepts_bare_basename_in_prose():
    """A bare `Foo.java` mention in prose (no `/`) shouldn't be
    redacted — there's not enough information to discriminate, and
    casual mentions are common in human-readable SRS prose."""
    from routes.srs import _enforce_workspace_prefix, _workspace_root, _allowed_basenames
    root = _workspace_root("t", "p")
    bn = _allowed_basenames(["src/Foo.java"])
    # Bare basename → path regex doesn't even match (no `/`), so
    # nothing to redact.
    text = "The Foo class handles submit."
    out, n = _enforce_workspace_prefix(text, root, bn)
    assert n == 0
    assert out == text


def test_enforce_workspace_prefix_disabled_when_no_root():
    """An empty workspace root disables the hard guard entirely — the
    soft path-suffix sanitizer remains in charge as it always was."""
    from routes.srs import _enforce_workspace_prefix
    text = "src/com/something/Foo.java"
    out, n = _enforce_workspace_prefix(text, "", {"foo.java"})
    assert n == 0
    assert out == text


def test_workspace_info_endpoint_imports_cleanly():
    """The endpoint imports `_workspace_root` from routes.srs — make
    sure the symbol contract is intact across the iter-13.99 rewrite."""
    from routes.srs import _workspace_root, _workspace_slug
    assert callable(_workspace_root)
    assert callable(_workspace_slug)


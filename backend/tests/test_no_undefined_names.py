"""Repo-wide guard: no undefined names in backend source.

Motivated by a live defect found in Phase 0 recon.

``routes/codegen.py`` formatted ``_todo_hits`` into a message at line 3751,
but that name is only assigned at line 3784 inside a *different* branch. The
branch that read it is the structural-validator final-attempt failure — the
path reached exactly when the LLM emitted structurally invalid code and the
deterministic scaffold is the recovery. So the recovery raised
``NameError: name '_todo_hits' is not defined`` instead of returning the
scaffold, precisely when CodeGen most needed to recover.

A read-before-assignment inside a rarely-taken error branch is invisible to
tests that only exercise the happy path, and invisible to a smoke run that
never trips the validator. The only thing that catches the whole class cheaply
is a static sweep, so that is what this pins.

This is deliberately a *zero* assertion rather than a baseline count. As of
Phase 0 the backend has exactly zero undefined names once the defect above is
fixed, so any regression is a real one and there is no threshold to erode.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

# Every non-test Python file in the backend. Tests are excluded because their
# fixtures legitimately reference names injected by pytest at collection time.
_SOURCE_DIRS = (
    _BACKEND,
    _BACKEND / "routes",
    _BACKEND / "kb",
    _BACKEND / "codegen",
    _BACKEND / "fabric",
    _BACKEND / "datamodel",
    _BACKEND / "integrations",
)


def _source_files() -> list[Path]:
    out: list[Path] = []
    for d in _SOURCE_DIRS:
        if not d.is_dir():
            continue
        out.extend(sorted(
            p for p in d.glob("*.py")
            # _test_* are manual scripts, not collected by pytest.
            # .vulture-whitelist.py is a file of bare names by design --
            # that is the format vulture reads, so every line is
            # legitimately an "undefined name".
            if not p.name.startswith("_test")
            and p.name != ".vulture-whitelist.py"
        ))
    return out


def test_no_undefined_names_in_backend_source():
    """pyflakes reports zero `undefined name` findings across backend source.

    pyflakes rather than ruff because it is the PRD's standing verification
    step and is pinned in requirements.txt, so this test runs the same tool
    the project already commits to.
    """
    files = _source_files()
    assert files, "source sweep found no files — the glob is wrong"

    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *[str(f) for f in files]],
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    undefined = [ln for ln in combined.splitlines() if "undefined name" in ln]

    assert not undefined, (
        "undefined name(s) in backend source — each of these raises NameError "
        "at runtime on whichever branch reaches it:\n  "
        + "\n  ".join(undefined)
    )


def test_structural_recovery_message_cites_missing_markers():
    """The structural-validator recovery path explains the real reason.

    Two different recovery paths in ``_gen_one_file`` both swap LLM output for
    the deterministic scaffold, and they fire for different reasons:

      * structural failure  — the file lacks mandatory framework markers,
                              which are in the local ``missing`` list
      * TODO-carpet         — the file passed structurally but is a stub
                              carpet, counted into ``_todo_hits``

    The structural path used to report a TODO count, which was both a
    ``NameError`` and the wrong explanation for that branch. Pin that it now
    reports what actually went wrong, so an operator reading the generated
    file header is not sent looking for placeholders that were never the
    problem.
    """
    src = (_BACKEND / "routes" / "codegen.py").read_text(encoding="utf-8")

    marker = "deterministic scaffold (LLM output failed structural validation"
    assert marker in src, (
        "the structural-recovery scaffold header changed shape; update this "
        "test deliberately rather than loosening it"
    )

    # The structural branch must not reach for the TODO-carpet counter.
    head, _, tail = src.partition(marker)
    window = tail[:400]
    assert "_todo_hits" not in window, (
        "the structural-recovery message references _todo_hits again — that "
        "name is only bound on the TODO-carpet branch"
    )


@pytest.mark.parametrize("path", ["routes/codegen.py", "routes/tools.py"])
def test_big_route_files_compile(path: str):
    """The two largest files parse. Cheap tripwire for a truncated decorator.

    ``routes/projects.py`` was once committed with a syntax-corrupted
    decorator (``e")`` in place of ``@router.get(...)``) which took the whole
    backend down at import. These two files are the ones most often edited by
    an agent, so parsing them is worth a second.
    """
    source = (_BACKEND / path).read_text(encoding="utf-8")
    compile(source, str(_BACKEND / path), "exec")

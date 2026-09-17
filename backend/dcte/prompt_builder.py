"""Generic migration brief — iter-21.

Before this, the migration prompt was a Helidon → Spring Boot essay pasted
into two modules: `ai_refactor._SYSTEM` and `droid_agent._AGENTIC_BRIEF`.
Every job got it regardless of what the user actually selected, so a
JSP → React job was told to "convert from Helidon (MicroProfile / SE) to
Spring Boot 3.x" and to eradicate `@ApplicationScoped`. That is not a
prompt with a bug in it; it is a prompt for a different job, and the model
either follows it (wrong output) or ignores it (unpredictable output).

The brief is now BUILT from the selected `(source, target)` pair using the
catalogue in `stacks.py`. The role, the three tasks and the three strict
rules are fixed — they are the operator's contract and read identically for
every pair. Everything stack-specific is looked up:

    idioms      the target's house style
    forbidden   markers that must not survive from the source
    manifest    what the build file has to declare
    api_docs    how this target exposes OpenAPI/Swagger
    build_cmd   the command that must exit 0

A pair with no catalogue entry still produces a usable brief: the stack id
is used verbatim and the stack-specific sections are omitted rather than
being filled with another stack's rules. An empty section beats a wrong one.

Two renderings, same content:
  * `TRANSFORMER` — per-file, returns the strict JSON contract `ai_refactor`
    parses.
  * `AGENTIC` — whole-service, for `droid exec`, which has a shell and
    iterates to a green build itself.
"""
from __future__ import annotations

from .stacks import Stack, get_stack, stack_label

# Bump when the wording changes materially, so a cached/echoed brief can be
# told apart from the current one. `droid_agent` records it on every run.
BRIEF_REV = 2

_ROLE = ("You are a Senior Backend Developer and Legacy-to-Modernization "
         "expert.")

_STRICT_RULES = (
    "Do NOT alter any business logic. Variable names, branch semantics, DB "
    "column names, request/response shapes, HTTP verbs and URL paths are "
    "preserved exactly unless the target framework makes the old form "
    "impossible to express.",
    "Do NOT conclude with broken code. Every file you emit must be "
    "self-consistent: imports match usages, the declared type matches the "
    "file name, and no half-migrated symbol from the source stack survives.",
    "Ensure token efficiency with 100% accuracy. A file already correct for "
    "the target stack is left alone and NOT re-emitted. Never truncate a "
    "file you do rewrite — there is no way to merge a partial rewrite, so "
    "truncated output is discarded.",
)


def _bullets(items, indent: str = "     ") -> str:
    return "\n".join(f"{indent}- {i}" for i in items)


def _target_section(tgt: Stack | None, target_id: str) -> str:
    """Idioms + manifest + build command for the target, if we know it."""
    if tgt is None:
        return (f"  Target stack: {target_id}. Follow that stack's mainstream "
                "conventions as its own documentation defines them.\n")
    out = [f"  Target: {tgt.label} ({tgt.language})."]
    if tgt.idioms:
        out.append("  Write idiomatic code for it:")
        out.append(_bullets(tgt.idioms))
    if tgt.manifest:
        out.append("  The build manifest must declare:")
        out.append(_bullets(tgt.manifest))
    if tgt.build_cmd:
        out.append(f"  The build command that must exit 0: `{tgt.build_cmd}`")
    return "\n".join(out) + "\n"


def _residue_section(src: Stack | None, source_id: str) -> str:
    """The 'no leftovers' clause — built from the SOURCE stack's markers."""
    if src is None or not src.forbidden:
        return (f"  No construct specific to {source_id} may remain in a file "
                "you report as migrated.\n")
    markers = ", ".join(f"`{m}`" for m in src.forbidden)
    return (f"  No {src.label} construct may remain in a migrated file. In "
            f"particular none of: {markers}.\n")


def _api_docs_section(tgt: Stack | None) -> str:
    if tgt is not None and tgt.api_docs:
        return f"  {tgt.api_docs}\n"
    return ("  Expose an OpenAPI/Swagger description using whatever the "
            "target stack's standard tooling is, and make sure its UI route "
            "resolves on a running app.\n")


def _header(source_id: str, target_id: str) -> tuple[Stack | None, Stack | None, str, str]:
    src, tgt = get_stack(source_id), get_stack(target_id)
    return src, tgt, stack_label(source_id), stack_label(target_id)


def stack_sections(source_stack: str, target_stack: str) -> str:
    """The pair-specific half of every DCTE brief, as one block.

    iter-22 — extracted so a seeded Prompt Library row can mark where it
    goes with `{stack_sections}` (see `dcte/prompt_store.py`). This is the
    part that CANNOT live in the library: it is derived from the operator's
    own source/target selection, and there are 98 selectable pairs.
    """
    src, tgt = get_stack(source_stack), get_stack(target_stack)
    return (
        "TARGET CONVENTIONS\n"
        f"{_target_section(tgt, target_stack)}"
        "NO SOURCE RESIDUE\n"
        f"{_residue_section(src, source_stack)}"
        "API DOCUMENTATION\n"
        f"{_api_docs_section(tgt)}"
    )


def build_migration_brief(source_stack: str, target_stack: str) -> str:
    """The whole-service brief handed to an agent with a shell (droid)."""
    src, tgt, src_label, tgt_label = _header(source_stack, target_stack)
    build_cmd = (tgt.build_cmd if tgt and tgt.build_cmd else "the project's build command")

    return f"""{_ROLE}

You have full shell and file access inside the current working directory
(--cwd). Treat that directory as the ROOT of a single service that must be
migrated in place.

TASK
  1. Convert this project from {src_label} to {tgt_label}.
  2. Implement Swagger / OpenAPI.
  3. Ensure the build is error free.

STRICT RULES
  1. {_STRICT_RULES[0]}
  2. {_STRICT_RULES[1]}
  3. {_STRICT_RULES[2]}

TARGET CONVENTIONS
{_target_section(tgt, target_stack)}
NO SOURCE RESIDUE
{_residue_section(src, source_stack)}
API DOCUMENTATION
{_api_docs_section(tgt)}
WORKFLOW
  1. Read the source tree in --cwd. Do not read anything outside it.
  2. Apply the conversion across every file that needs it, including the
     build manifest and configuration.
  3. Run `{build_cmd}`. Read the errors. Fix them. Loop.
  4. When the build is green, print DROID_AGENT_OK on its own line and stop.

If after 5 build attempts you cannot make it green, stop and print
DROID_AGENT_INCOMPLETE: followed by the exact remaining errors, so the
operator can take over. Do not print DROID_AGENT_OK on a red build.
"""


def build_transformer_brief(source_stack: str, target_stack: str) -> str:
    """The per-file brief for `ai_refactor`, which parses a JSON reply.

    The JSON contract itself is appended by the caller, so the schema stays
    beside the parser that has to satisfy it.
    """
    src, tgt, src_label, tgt_label = _header(source_stack, target_stack)

    return f"""{_ROLE}

TASK
  1. Convert the given project files from {src_label} to {tgt_label}.
  2. Implement Swagger / OpenAPI.
  3. Ensure the emitted code is build-error free — every rewritten file must
     compile as-is against the target stack.

STRICT RULES
  1. {_STRICT_RULES[0]}
  2. {_STRICT_RULES[1]}
  3. {_STRICT_RULES[2]}

TARGET CONVENTIONS
{_target_section(tgt, target_stack)}
NO SOURCE RESIDUE
{_residue_section(src, source_stack)}
API DOCUMENTATION
{_api_docs_section(tgt)}"""


def build_build_fixer_brief(source_stack: str, target_stack: str,
                            release_note: str = "") -> str:
    """Brief for the compile-error triage agent (`dcte/build_agent.py`).

    iter-22 — `build_agent._SYSTEM` was a hardcoded essay opening "You are a
    Senior Backend Developer and Spring Boot 3.x / Java 17-21 build-error
    triage expert", listing Spring Boot 3 starters, and forbidding Helidon
    symbols. Every pair received it. With `spring-boot-4` (Java 25) now the
    catalogue's recommended target, the agent repairing those builds was
    being told to target Java 17-21 — so it "fixed" valid Java 25 source.

    `release_note` carries the toolchain reality when it differs from what
    the target pins (the image ships JDK 17; the catalogue pins Java 25).
    Saying so is the difference between the model rewriting good code to
    please an old compiler and the model leaving it alone.
    """
    src, tgt, src_label, tgt_label = _header(source_stack, target_stack)
    build_cmd = tgt.build_cmd if tgt and tgt.build_cmd else "the project's build command"

    parts = [
        f"{_ROLE} You are triaging BUILD ERRORS for a {tgt_label} project "
        f"that was just migrated from {src_label}.",
        "",
        "TASK",
        "  You are given ONE source file and the exact compiler/build errors it",
        "  produced. Return the FULL corrected source of that file so that",
        f"  `{build_cmd}` no longer reports those errors.",
        "",
        "STRICT RULES",
        f"  1. {_STRICT_RULES[0]}",
        "  2. Do NOT truncate. Return the file from its first line through its",
        "     last. Never end with `...` or \"rest of file omitted\".",
        "  3. Fix only what the compiler complained about, plus any imports or",
        "     types that fix requires. Do not restructure unrelated code.",
        "  4. A wall of 'cannot find symbol' under ONE 'package does not exist'",
        "     is ONE fault, not fifty. Fix the cause, not each symptom.",
        "  5. If a fix needs a class you cannot see, add a reasonable",
        f"     {tgt_label} equivalent, preserving the field names.",
        "",
        "TARGET CONVENTIONS",
        _target_section(tgt, target_stack),
        "NO SOURCE RESIDUE",
        _residue_section(src, source_stack),
    ]
    if release_note:
        parts += ["TOOLCHAIN", f"  {release_note}", ""]
    parts += [
        "RESPONSE FORMAT — STRICT JSON, one object, no prose, no code fences:",
        '  {"file": "<absolute path echoed back exactly>",',
        '   "action": "rewrite" | "leave",',
        '   "content": "<full corrected file source when action==rewrite>",',
        '   "changes": ["short bullet", "short bullet"],',
        '   "risk": "low" | "medium" | "high"}',
        "",
        '  If you genuinely cannot fix the file without more context, return',
        '  "action":"leave" with empty "content" and one line in "changes"',
        "  saying what you would need. A guess that compiles but changes",
        "  behaviour is worse than an honest 'leave'.",
    ]
    return "\n".join(parts)


def build_devops_brief(source_stack: str, target_stack: str) -> str:
    """Brief for the DevOps agent's escalation pass (iter-22).

    It is shown ONE structural gap that no template could close, the service
    tree, and the current contents of the file the gap names. Its job is to
    return one file. It is not a migration agent and must not become one:
    the deterministic patchers keep first refusal precisely because a model
    asked to invent an `application.yml` will invent one.
    """
    _src, tgt, src_label, tgt_label = _header(source_stack, target_stack)

    return f"""You are a DevOps engineer finishing a {src_label} → {tgt_label}
migration. The code has been converted; what remains are STRUCTURAL gaps —
things a compiler cannot see but that stop the service running: a missing
entrypoint, a configuration file with no section for something the code
reads, a build manifest missing a plugin the runtime needs.

TASK
  You are given ONE gap, the service tree, and the current contents of the
  file it names (when one exists). Return the FULL contents of exactly ONE
  file that closes it.

STRICT RULES
  1. {_STRICT_RULES[0]}
  2. Close ONLY the gap described. Do not refactor, reformat or "improve"
     anything else in the file you return.
  3. Prefer the smallest change that works. A config key is better than a
     new class; a new class is better than restructuring the tree.
  4. If you cannot close the gap from what you were given, say so with
     `"action":"skip"` and one line explaining what you would need. An
     invented value that looks plausible — a database URL, a credential, a
     port another service already owns — is worse than an honest skip,
     because it will be reported as fixed.
  5. The path you return is relative to the service root and must stay
     inside it. Never `..`.

TARGET CONVENTIONS
{_target_section(tgt, target_stack)}
RESPONSE FORMAT — STRICT JSON, one object, no prose, no code fences:
  {{"action": "write" | "skip",
   "file": "<path relative to the service root>",
   "content": "<the full file contents when action==write>",
   "why": "<one sentence: what was missing and what you did about it>"}}
"""


def build_fixup_directive(source_stack: str, target_stack: str) -> str:
    """Appended on a SECOND pass over a file that still carries residue.

    The primary brief is deliberately general; this one adds a no-escape
    clause, because the primary already had its chance on this file and
    either returned `action=leave` on a dirty file or rewrote it with
    leftovers still in place.
    """
    src, _tgt, src_label, tgt_label = _header(source_stack, target_stack)
    markers = ""
    if src and src.forbidden:
        markers = " (for example " + ", ".join(f"`{m}`" for m in src.forbidden[:6]) + ")"

    return f"""

FIX-UP DIRECTIVE — RESIDUE REMEDIATION
  You are being called a SECOND time on this file because the first pass
  left {src_label} markers behind{markers}. The user block below lists the
  exact markers still present.

  You MUST return `"action":"rewrite"` for every file listed, with the
  markers gone. `"action":"leave"` is NOT acceptable on a file that still
  carries one: if you believe a marker is a false positive (it appears only
  inside a comment or a string literal), rewrite the file anyway and remove
  or reword that comment so the scan is satisfied.

  The rewritten file must be valid {tgt_label} and must still do exactly
  what the original did.
"""

"""TOON (Typed Object Oriented Notation) serialiser.

Format example:
  [CLASS:CCA_approval] pkg=App.Controllers extends=BaseController
    [METHOD:cca_fls_calculation] auth=session(admin_detail) role=cca db=pmis_projects
    [SESSION:admin_detail] fields=user_id,user_agency,role_id
  [TABLE:pmis_projects] pk=project_id
    [COL:scheme_id] type=int fk->pmis_schemes
    [COL:project_type] type=varchar
"""
from typing import List, Dict, Any


def _csv(values):
    return ",".join(str(v) for v in values if v not in (None, ""))


def serialise_class(entity: Dict[str, Any]) -> str:
    name = entity.get("name", "Unknown")
    ns = entity.get("namespace") or ""
    extends = entity.get("extends") or ""
    line = f"[CLASS:{name}]"
    if ns:
        line += f" pkg={ns}"
    if extends:
        line += f" extends={extends}"
    lines = [line]

    sess_fields = entity.get("session_fields") or []
    if sess_fields:
        lines.append(f"  [SESSION:admin_detail] fields={_csv(sess_fields)}")

    for m in entity.get("methods", []) or []:
        parts = [f"  [METHOD:{m['name']}]"]
        if m.get("sessions"):
            parts.append(f"auth=session({_csv(m['sessions'])})")
        if m.get("tables"):
            parts.append(f"db={_csv(m['tables'])}")
        lines.append(" ".join(parts))

    return "\n".join(lines)


def serialise_table(entity: Dict[str, Any]) -> str:
    name = entity.get("name", "Unknown")
    pk = entity.get("pk", "")
    line = f"[TABLE:{name}]"
    if pk:
        line += f" pk={pk}"
    lines = [line]

    fk_map = {fk["column"]: fk for fk in entity.get("fks", [])}

    for col in entity.get("columns", []) or []:
        cname = col["name"]
        ctype = col["type"]
        part = f"  [COL:{cname}] type={ctype}"
        if cname in fk_map:
            part += f" fk->{fk_map[cname]['ref_table']}"
        lines.append(part)

    return "\n".join(lines)


def serialise_route(entity: Dict[str, Any]) -> str:
    return f"[ROUTE:{entity.get('name')}] handler={entity.get('handler','')}"


def serialise_module(entity: Dict[str, Any]) -> str:
    """User-imported business module: '[MODULE:Name] src=excel components=12 tables=45'
    followed by up to 10 top components by table_count."""
    name = entity.get("name", "")
    src = entity.get("source_format", "import")
    cc = entity.get("component_count", 0)
    tc = entity.get("table_ref_count", 0)
    lines = [f"[MODULE:{name}] src={src} components={cc} tables={tc}"]
    for comp in sorted(
        entity.get("components_detail", []) or [],
        key=lambda x: x.get("table_count", 0),
        reverse=True,
    )[:10]:
        refs = ",".join((comp.get("tables") or [])[:8])
        lines.append(f"  [COMPONENT:{comp.get('name','')}] refs->{refs}")
    if entity.get("description"):
        lines.append(f"  [DESC:{str(entity['description'])[:120]}]")
    return "\n".join(lines)


def serialise(entities: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []

    modules = [e for e in entities if e.get("type") == "MODULE"]
    classes = [e for e in entities if e.get("type") == "CLASS"]
    tables = [e for e in entities if e.get("type") == "TABLE"]
    routes = [e for e in entities if e.get("type") == "ROUTE"]
    individuals = [e for e in entities if e.get("type") == "INDIVIDUAL"]

    if modules:
        blocks.append("# MODULES")
        blocks.extend(
            serialise_module(m)
            for m in sorted(modules, key=lambda x: x.get("table_ref_count", 0), reverse=True)
        )
    if classes:
        blocks.append("\n# CLASSES" if modules else "# CLASSES")
        blocks.extend(serialise_class(c) for c in classes)
    if tables:
        blocks.append("\n# TABLES")
        blocks.extend(serialise_table(t) for t in tables)
    if routes:
        blocks.append("\n# ROUTES")
        blocks.extend(serialise_route(r) for r in routes)
    if individuals:
        blocks.append("\n# INDIVIDUALS")
        # group by category
        by_cat: Dict[str, List[str]] = {}
        for ind in individuals:
            by_cat.setdefault(ind.get("category", "misc"), []).append(ind.get("name", ""))
        for cat, names in by_cat.items():
            blocks.append(f"[GROUP:{cat}] members={','.join(sorted(set(names)))}")

    return "\n".join(blocks)


# ──────────────────────────────────────────────────────────────────────
# Stage-aware pruning
# ──────────────────────────────────────────────────────────────────────
# The canonical home for TOON slicing. `routes/chat.py::prune_toon` is
# kept as a thin re-export for backward compatibility with existing
# imports and tests (iter-13.30 contract). Every stage carries a
# different "hot" slice; sending the other slices verbatim wastes
# tokens without helping the LLM ground its answer.
_STAGE_SECTION_ORDER: Dict[str, List[str]] = {
    "discovery":    ["CLASSES", "ROUTES", "INDIVIDUALS", "TABLES"],
    "datamodel":    ["TABLES", "CLASSES", "ROUTES", "INDIVIDUALS"],
    "architecture": ["ROUTES", "CLASSES", "TABLES", "INDIVIDUALS"],
    "codegen":      ["ROUTES", "CLASSES", "TABLES", "INDIVIDUALS"],
    "living":       ["ROUTES", "CLASSES", "TABLES", "INDIVIDUALS"],
}


def _split_sections(toon: str) -> Dict[str, List[str]]:
    """Parse a serialised TOON blob back into its `# SECTION` blocks."""
    out: Dict[str, List[str]] = {}
    current: str | None = None
    for line in toon.split("\n"):
        if line.startswith("# "):
            current = line[2:].strip().split()[0]
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return out


def prune(toon: str, max_chars: int, stage: str) -> str:
    """Stage-aware pruning of a serialised TOON blob.

    - Returns the input unchanged when already under `max_chars`.
    - Emits sections in the stage-priority order; sections not relevant
      to the stage are demoted to a "names-only" projection so the LLM
      still sees they EXIST (avoids hallucinated new entities) without
      paying the full column/method cost.
    - Truncates at the section boundary once the budget is hit.
    """
    if not toon:
        return ""
    if len(toon) <= max_chars:
        return toon

    sections = _split_sections(toon)
    stage_l = (stage or "").lower()
    order = _STAGE_SECTION_ORDER.get(stage_l, ["ROUTES", "CLASSES", "TABLES", "INDIVIDUALS"])

    result: List[str] = []
    total = 0
    hot_key = order[0]

    for key in order:
        block_lines = sections.get(key)
        if not block_lines:
            continue

        # For non-hot sections, keep header rows only ([CLASS:name],
        # [TABLE:name], …) — drop indented detail lines. Massive win
        # for irrelevant slices: a 40-KB TABLES block collapses to
        # ~2 KB of names when the stage is Discovery.
        if key != hot_key:
            header_lines = [ln for ln in block_lines if ln and not ln.startswith("  ")]
            block = f"# {key} (names only)\n" + "\n".join(header_lines)
        else:
            block = f"# {key}\n" + "\n".join(block_lines)

        remaining = max_chars - total
        if len(block) > remaining:
            if remaining > 200:
                result.append(block[:remaining] + "\n...[truncated]")
            break
        result.append(block)
        total += len(block) + 1  # +1 for join newline

    return "\n".join(result)


def summarise(
    entities: List[Dict[str, Any]],
    stats: Dict[str, int],
    language: str | None = None,
) -> str:
    """Short textual summary of the KB for system prompts and toast messages.

    The class-count label is derived from the detected primary `language` (e.g.
    "Java classes", "Python classes", "C# classes"). Falls back to a neutral
    "classes" label when language is unknown — never hard-codes "PHP" again
    (iter 13.7 fix; previously every project's KB-built toast said "PHP classes"
    regardless of actual stack).
    """
    lang_label = (language or "").strip()
    # Normalise a few common aliases coming out of tech_detector so the toast
    # reads naturally: "javascript" -> "JS", "typescript" -> "TS", "csharp" /
    # "c#" / "dotnet" -> "C#", everything else title-cased.
    _alias = {
        "javascript": "JS",
        "js": "JS",
        "typescript": "TS",
        "ts": "TS",
        "csharp": "C#",
        "c#": "C#",
        "dotnet": "C#",
        ".net": "C#",
        "vb": "VB.NET",
        "vb.net": "VB.NET",
        "python": "Python",
        "java": "Java",
        "jsp": "Java",
        "php": "PHP",
        "sql": "SQL",
    }
    key = lang_label.lower()
    if key in _alias:
        class_label = f"{_alias[key]} classes"
    elif lang_label:
        class_label = f"{lang_label} classes"
    else:
        class_label = "classes"
    parts = [
        f"Entities: {stats.get('entities', 0)} total",
        f"{stats.get('classes', 0)} {class_label}",
        f"{stats.get('methods', 0)} methods",
        f"{stats.get('tables', 0)} DB tables",
        f"{stats.get('columns', 0)} columns",
        f"{stats.get('relationships', 0)} foreign keys",
        f"{stats.get('roles', 0)} roles",
        f"{stats.get('routes', 0)} routes",
    ]
    return ", ".join(parts)

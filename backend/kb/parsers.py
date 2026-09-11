"""File parsers: extract text from PHP / SQL / PDF / DOCX / CSV / TXT / ZIP."""
import io
import csv
import zipfile
from typing import Optional


def parse_php(content: bytes) -> str:
    return content.decode("utf-8", errors="ignore")


def parse_java(content: bytes) -> str:
    """Java / JSP / XML / properties — return raw text (UTF-8 best-effort)."""
    return content.decode("utf-8", errors="ignore")


def parse_code(content: bytes) -> str:
    """Generic source-code parser (JS/TS/HTML/CSS/Python/C#/VB/etc.)."""
    return content.decode("utf-8", errors="ignore")


def parse_sql(content: bytes) -> str:
    return content.decode("utf-8", errors="ignore")


def parse_txt(content: bytes) -> str:
    return content.decode("utf-8", errors="ignore")


def parse_csv(content: bytes) -> str:
    text = content.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = []
    for i, row in enumerate(reader):
        rows.append(" | ".join(row))
        if i > 5000:
            break
    return "\n".join(rows)


def parse_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts)
    except Exception as e:
        return f"[PDF parse error: {e}]"


def parse_docx(content: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception as e:
        return f"[DOCX parse error: {e}]"


def parse_zip(content: bytes) -> str:
    """Extract a .zip in memory and concatenate text from all supported members."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception as e:
        return f"[ZIP open error: {e}]"

    parts: list[str] = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        # skip junk + nested zips (avoid recursion bombs)
        lower = name.lower()
        if any(skip in lower for skip in ["__macosx", ".ds_store", "node_modules/", ".git/", "vendor/", "__pycache__/", "meta-inf/"]):
            continue
        if lower.endswith(".zip"):
            continue
        try:
            data = zf.read(name)
        except Exception:
            continue
        # recurse via parse_file but avoid re-zip
        _ftype, text = parse_file(name, data, allow_zip_recurse=False)
        if text.strip():
            parts.append(f"\n===== FILE: {name} =====\n{text}")
    return "\n".join(parts)


def parse_file(filename: str, content: bytes, allow_zip_recurse: bool = True) -> tuple[str, str]:
    """Returns (filetype, extracted_text)."""
    name = filename.lower()
    if name.endswith(".php"):
        return "php", parse_php(content)
    if name.endswith(".sql"):
        return "sql", parse_sql(content)
    if name.endswith(".java"):
        return "java", parse_java(content)
    if name.endswith((".jsp", ".jspx", ".jspf", ".tag", ".tld", ".xhtml")):
        return "jsp", parse_java(content)
    if name.endswith(".xml"):
        return "xml", parse_java(content)
    if name.endswith(".properties"):
        return "config", parse_java(content)
    if name.endswith((".cs", ".vb", ".aspx", ".cshtml", ".vbhtml", ".config")):
        return "dotnet", parse_code(content)
    if name.endswith((".js", ".jsx", ".ts", ".tsx")):
        return "js", parse_code(content)
    if name.endswith((".html", ".htm", ".css", ".scss")):
        return "web", parse_code(content)
    if name.endswith(".py"):
        return "python", parse_code(content)
    if name.endswith((".yaml", ".yml")):
        return "yaml", parse_code(content)
    if name.endswith(".json"):
        return "json", parse_code(content)
    if name.endswith(".pdf"):
        return "pdf", parse_pdf(content)
    if name.endswith(".docx"):
        return "docx", parse_docx(content)
    if name.endswith(".csv"):
        return "csv", parse_csv(content)
    if name.endswith((".txt", ".md")):
        return "txt", parse_txt(content)
    if name.endswith(".zip") and allow_zip_recurse:
        return "zip", parse_zip(content)
    # Default: treat as text
    return "txt", parse_txt(content)


def _char_window_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Sliding-window character chunker (legacy fallback)."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + chunk_size)
        if end < n:
            nl = text.rfind("\n", start, end)
            if nl > start + chunk_size // 2:
                end = nl
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


# iter-13.18 — Structure-aware splitters. Regex anchors top-level symbol
# boundaries per language; we slice between consecutive matches so a chunk
# contains a coherent unit (class, function, route handler, SQL statement,
# YAML top-level key, markdown heading). Each piece is then passed through
# the char-window splitter ONLY if it exceeds the budget — keeps small
# methods atomic instead of shredding them across a sliding window.
import re as _chunk_re

_STRUCTURE_PATTERNS: dict[str, _chunk_re.Pattern] = {
    # Java / JSP-like — class, interface, enum, top-level methods, @Annotation routes
    "java": _chunk_re.compile(
        r"^(?:\s*(?:public|private|protected|static|final|abstract|@\w[\w.]*)\s+)*"
        r"(?:class|interface|enum|record)\s+\w+|"
        r"^\s*(?:public|private|protected|static)?\s*(?:[\w<>,\[\]\s]+)\s+\w+\s*\([^)]*\)\s*(?:throws[^{]+)?\{",
        _chunk_re.MULTILINE,
    ),
    "jsp": _chunk_re.compile(r"<%[!@=]?|</?(?:c:|fmt:|sql:|jsp:)\w+", _chunk_re.MULTILINE),
    "dotnet": _chunk_re.compile(
        r"^\s*(?:public|private|protected|internal|static)?\s*(?:class|interface|struct|enum|record)\s+\w+|"
        r"^\s*\[(?:HttpGet|HttpPost|HttpPut|HttpDelete|Route)\b",
        _chunk_re.MULTILINE,
    ),
    "php": _chunk_re.compile(
        r"^\s*(?:abstract\s+|final\s+)?class\s+\w+|"
        r"^\s*(?:public|private|protected|static)?\s*function\s+\w+\s*\(|"
        r"^\s*(?:public|private|protected|static)?\s*function\s+\w+\s*\(",
        _chunk_re.MULTILINE,
    ),
    "python": _chunk_re.compile(r"^(?:class|def|async\s+def)\s+\w+", _chunk_re.MULTILINE),
    "js": _chunk_re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(?:async\s*)?\(?[^=]*=>)",
        _chunk_re.MULTILINE,
    ),
    "yaml": _chunk_re.compile(r"^[A-Za-z_][\w.-]*\s*:", _chunk_re.MULTILINE),
    "json": None,  # one chunk per object — handled below
    "txt": _chunk_re.compile(r"^#{1,6}\s+\S", _chunk_re.MULTILINE),  # markdown headings
    "config": _chunk_re.compile(
        r"^\s*<(?:bean|servlet|filter|action|mapping|form-bean|controller)\b|"
        r"^[A-Za-z_][\w.-]*\s*=",
        _chunk_re.MULTILINE,
    ),
}

# SQL gets its own splitter — semicolon-terminated statements + CREATE/ALTER anchors.
_SQL_SPLIT = _chunk_re.compile(
    r"(?im)^(?:\s*)(CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER|INDEX|TYPE|SEQUENCE)|"
    r"ALTER\s+TABLE|"
    r"INSERT\s+INTO|"
    r"UPDATE\s+\w+|"
    r"DELETE\s+FROM|"
    r"WITH\s+\w+\s+AS|"
    r"SELECT\s+)"
)

_SYMBOL_NAME = _chunk_re.compile(r"\b(?:class|interface|enum|record|struct|function|def|async\s+def)\s+(\w+)")


def _extract_symbol(block: str) -> str:
    m = _SYMBOL_NAME.search(block[:400])
    return m.group(1) if m else ""


def _split_on_structure(text: str, filetype: str, max_piece: int) -> list[str]:
    """Cut on language-aware anchors; return raw pieces (may exceed budget)."""
    if not text:
        return []
    if filetype == "sql":
        # Split on statement starts; rejoin with their leading anchor.
        parts = _SQL_SPLIT.split(text)
        if len(parts) <= 1:
            return [text]
        out: list[str] = []
        # parts[0] is preamble; subsequent pairs are (anchor, body)
        if parts[0].strip():
            out.append(parts[0].strip())
        for i in range(1, len(parts) - 1, 2):
            out.append((parts[i] + parts[i + 1]).strip())
        return [p for p in out if p]

    pat = _STRUCTURE_PATTERNS.get(filetype)
    if pat is None:
        return [text]
    matches = list(pat.finditer(text))
    if len(matches) < 2:
        # Not enough structure to be worth the overhead; let char-window handle it.
        return [text]
    pieces: list[str] = []
    # Prefix (before first match) — keep it as its own piece if non-trivial.
    head = text[: matches[0].start()].strip()
    if len(head) > 80:
        pieces.append(head)
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            pieces.append(block)
    return pieces


def chunk_text(
    text: str,
    chunk_size: int = 1500,
    overlap: int = 150,
    *,
    filetype: str | None = None,
    filename: str | None = None,
) -> list[str]:
    """Structure-aware chunker (iter-13.18).

    Backward-compatible signature: existing callers that pass only `text`
    fall through to the char-window splitter.

    When `filetype` is supplied, the text is first cut on top-level symbol
    boundaries (class / function / SQL statement / YAML key / markdown
    heading / route handler) so each chunk contains a coherent unit.
    Pieces that still exceed `chunk_size` are re-sliced with the legacy
    sliding-window splitter so total chunk length stays bounded.

    When `filename` is also supplied, every chunk is prefixed with a
    one-line CONTEXT header so the embedding represents the chunk's
    *role* (filename + parent symbol), not just its raw substring —
    materially improves retrieval precision on copy-pasted legacy code.
    """
    text = (text or "").strip()
    if not text:
        return []
    # iter-14.8 — Bypass the structure-aware splitter for very large or
    # minified/generated files. `_split_on_structure` runs regex-heavy
    # anchor detection whose worst-case is catastrophic-backtracking on
    # generated Java (1MB+ single-method DAOs), minified JS bundles, or
    # concatenated SQL dumps. That was the second cause of the "ingest
    # stuck at ~85%" report (the first — event-loop starvation — was
    # fixed in iter-14.7). The char-window fallback is O(n) and safe.
    _bypass = False
    if len(text) > 200_000:
        _bypass = True
    elif filetype in {"java", "js", "dotnet", "php", "sql"}:
        # Heuristic mirror of _looks_minified: single-line or extremely long
        # average lines almost always indicate generated / bundled code.
        n = len(text)
        if n >= 20_000:
            newlines = text.count("\n")
            if newlines < 5 or (n >= 100_000 and (n / max(1, newlines)) > 500):
                _bypass = True
    if _bypass or not filetype:
        return _char_window_chunks(text, chunk_size, overlap)

    raw_pieces = _split_on_structure(text, filetype, chunk_size)
    out: list[str] = []
    for piece in raw_pieces:
        symbol = _extract_symbol(piece)
        header_bits = []
        if filename:
            header_bits.append(filename)
        if symbol:
            header_bits.append(symbol)
        if filetype:
            header_bits.append(filetype.upper())
        header = " ▸ ".join(header_bits)
        prefix = f"# CONTEXT: {header}\n" if header else ""

        if len(piece) <= chunk_size:
            out.append((prefix + piece).strip())
            continue
        # Oversized piece — char-window split, but carry the header into
        # every sub-chunk so retrieval still knows the symbol.
        sub = _char_window_chunks(piece, chunk_size, overlap)
        for s in sub:
            out.append((prefix + s).strip())
    return [c for c in out if c]


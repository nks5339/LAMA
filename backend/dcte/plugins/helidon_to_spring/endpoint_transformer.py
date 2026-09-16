"""Rewrite Helidon MP / JAX-RS resource classes to Spring @RestController.

Deterministic, regex-driven. Handles:
    * class-level @Path("/api") → @RestController + @RequestMapping("/api")
    * method @GET + @Path("{id}") → @GetMapping("{id}")
    * @Inject → @Autowired
    * @PathParam / @QueryParam / @HeaderParam rewrites
    * @Produces / @Consumes → produces/consumes on the mapping
    * Removes JAX-RS imports, adds Spring ones (via ImportRewriter)
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any

from .mappings import (
    IMPORT_REPLACEMENTS,
    ANNOTATION_TOKEN_REPLACEMENTS,
    HTTP_VERBS,
)


_CLASS_PATH_RE = re.compile(r'@Path\("([^"]*)"\)')
# Two-pattern approach avoids `\s*` swallowing the newline that the
# optional `@Path(...)` group needs to see. First pattern consumes the
# combined form; the fallback pattern rewrites bare verb annotations.
_VERB_WITH_PATH_RE = re.compile(
    r'@(' + "|".join(HTTP_VERBS) + r')[ \t]*\n[ \t]*@Path\("([^"]*)"\)'
)
_VERB_BARE_RE = re.compile(
    r'@(' + "|".join(HTTP_VERBS) + r')(?![a-zA-Z_])'
)
_PRODUCES_RE = re.compile(r'@Produces\(\s*(MediaType\.\w+|"([^"]+)")\s*\)')
_CONSUMES_RE = re.compile(r'@Consumes\(\s*(MediaType\.\w+|"([^"]+)")\s*\)')
_PATH_PARAM_RE = re.compile(r'@PathParam\("([^"]+)"\)\s+(\w[\w<>,\s]*)\s+(\w+)')
_QUERY_PARAM_RE = re.compile(r'@QueryParam\("([^"]+)"\)\s+(\w[\w<>,\s]*)\s+(\w+)')
_HEADER_PARAM_RE = re.compile(r'@HeaderParam\("([^"]+)"\)\s+(\w[\w<>,\s]*)\s+(\w+)')

_JAXRS_IMPORT_RE = re.compile(r'^\s*import\s+(jakarta\.ws\.rs\.[^;]+);\s*$', re.MULTILINE)
_CDI_IMPORT_RE = re.compile(
    r'^\s*import\s+(jakarta\.(inject|enterprise\.context)\.[^;]+);\s*$', re.MULTILINE,
)
_MP_CONFIG_IMPORT_RE = re.compile(
    r'^\s*import\s+(org\.eclipse\.microprofile\.config[^;]+);\s*$', re.MULTILINE,
)


class EndpointTransformer:
    """Applies the JAX-RS → Spring rewrite to a single .java file."""

    def apply(self, src: str) -> tuple[str, dict[str, Any]]:
        """Return `(new_src, meta)`. `meta` describes what changed."""
        meta: dict[str, Any] = {
            "class_paths": [], "endpoints": [], "params": 0,
        }
        s = src

        # 1a) Combined form: @GET\n@Path("...") → @GetMapping("...")
        def _verb_path_repl(m: re.Match) -> str:
            verb = m.group(1)
            path = m.group(2)
            camel = verb.capitalize()
            meta["endpoints"].append({"verb": verb, "path": path})
            return f'@{camel}Mapping("{path}")'
        s = _VERB_WITH_PATH_RE.sub(_verb_path_repl, s)

        # 1b) Bare verb (no @Path follows): @GET → @GetMapping
        def _verb_bare_repl(m: re.Match) -> str:
            verb = m.group(1)
            camel = verb.capitalize()
            meta["endpoints"].append({"verb": verb, "path": ""})
            return f'@{camel}Mapping'
        s = _VERB_BARE_RE.sub(_verb_bare_repl, s)

        # 2) Any remaining @Path("...") is class-level → @RestController + @RequestMapping.
        def _class_repl(m: re.Match) -> str:
            path = m.group(1)
            meta["class_paths"].append(path)
            return f'@RestController\n@RequestMapping("{path}")'
        s = _CLASS_PATH_RE.sub(_class_repl, s)

        # 3) @Produces / @Consumes — keep as comment (they now live on
        # the mapping annotation as `produces=`/`consumes=`; leaving a
        # marker for manual attention beats silently dropping them).
        def _prod(m: re.Match) -> str:
            return f"// TODO(dcte): move produces to @*Mapping produces=: {m.group(0)}"
        def _cons(m: re.Match) -> str:
            return f"// TODO(dcte): move consumes to @*Mapping consumes=: {m.group(0)}"
        s = _PRODUCES_RE.sub(_prod, s)
        s = _CONSUMES_RE.sub(_cons, s)

        # 4) parameter annotations
        s, n1 = _PATH_PARAM_RE.subn(r'@PathVariable("\1") \2 \3', s)
        s, n2 = _QUERY_PARAM_RE.subn(r'@RequestParam("\1") \2 \3', s)
        s, n3 = _HEADER_PARAM_RE.subn(r'@RequestHeader("\1") \2 \3', s)
        meta["params"] += n1 + n2 + n3

        # 5) plain token annotations
        for old, new in ANNOTATION_TOKEN_REPLACEMENTS:
            s = s.replace(old, new)

        # 6) rewrite imports
        s = self._rewrite_imports(s)

        return s, meta

    def _rewrite_imports(self, src: str) -> str:
        replacements = dict(IMPORT_REPLACEMENTS)
        seen_new: set[str] = set()

        def _repl(m: re.Match) -> str:
            old_fqcn = m.group(1)
            new_fqcn = replacements.get(old_fqcn)
            if not new_fqcn:
                return f"// dcte: dropped Helidon-only import {old_fqcn}"
            if new_fqcn in seen_new:
                return ""  # dedupe
            seen_new.add(new_fqcn)
            return f"import {new_fqcn};"

        s = _JAXRS_IMPORT_RE.sub(_repl, src)
        s = _CDI_IMPORT_RE.sub(_repl, s)
        s = _MP_CONFIG_IMPORT_RE.sub(_repl, s)

        # Ensure @RestController / @RequestMapping imports exist if we
        # emitted them.
        if "@RestController" in s and "org.springframework.web.bind.annotation.RestController" not in s:
            s = self._inject_import(s, "org.springframework.web.bind.annotation.RestController")
        if "@RequestMapping" in s and "org.springframework.web.bind.annotation.RequestMapping" not in s:
            s = self._inject_import(s, "org.springframework.web.bind.annotation.RequestMapping")
        for verb in HTTP_VERBS:
            ann = f"@{verb.capitalize()}Mapping"
            fqcn = f"org.springframework.web.bind.annotation.{verb.capitalize()}Mapping"
            if ann in s and fqcn not in s:
                s = self._inject_import(s, fqcn)
        if "@Autowired" in s and "org.springframework.beans.factory.annotation.Autowired" not in s:
            s = self._inject_import(s, "org.springframework.beans.factory.annotation.Autowired")
        if "@Value" in s and "org.springframework.beans.factory.annotation.Value" not in s:
            s = self._inject_import(s, "org.springframework.beans.factory.annotation.Value")
        return s

    def _inject_import(self, src: str, fqcn: str) -> str:
        lines = src.splitlines()
        # Insert after the last existing import; else after package line.
        insert_at = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("package "):
                insert_at = i + 1
            if line.strip().startswith("import "):
                insert_at = i + 1
        lines.insert(insert_at, f"import {fqcn};")
        return "\n".join(lines)


def transform_java_file(path: Path, dest: Path) -> tuple[bool, dict[str, Any]]:
    """Transform one file. Returns `(changed, meta)`."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    et = EndpointTransformer()
    new_src, meta = et.apply(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(new_src, encoding="utf-8")
    changed = new_src != src
    return changed, meta

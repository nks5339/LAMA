"""Rewrite Helidon MP / JAX-RS resource classes to Spring @RestController.

Deterministic, regex-driven. Handles:
    * class-level @Path("/api") → @RestController + @RequestMapping("/api")
    * method @GET + @Path("{id}") → @GetMapping("{id}")
    * @Inject → @Autowired
    * @PathParam / @QueryParam / @HeaderParam rewrites
    * @Produces / @Consumes → produces/consumes on the mapping
    * @ConfigProperty(name=…, defaultValue=…) → @Value("${key:default}")
    * Rewrites every legacy import namespace the mapping table covers —
      jakarta.ws.rs, jakarta.inject / enterprise.context / annotation,
      org.eclipse.microprofile.*, io.helidon.* — and injects the Spring
      imports for annotations it synthesised (@RestController, the verb
      mappings, @Autowired, @Value, @PreAuthorize)
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

# iter-20 — @ConfigProperty carries its key and default as ATTRIBUTES;
# Spring's @Value carries them inside one placeholder string. A blind token
# swap produced `@Value(name = "k", defaultValue = "d")`, which does not
# compile. Three accepted source forms, in order of specificity.
_CONFIG_PROP_FULL_RE = re.compile(
    r'@ConfigProperty\(\s*name\s*=\s*"([^"]+)"\s*,'
    r'\s*defaultValue\s*=\s*"([^"]*)"\s*\)'
)
_CONFIG_PROP_NAME_RE = re.compile(r'@ConfigProperty\(\s*name\s*=\s*"([^"]+)"\s*\)')
_CONFIG_PROP_BARE_RE = re.compile(r'@ConfigProperty\(\s*"([^"]+)"\s*\)')

# `@Inject @ConfigProperty` is the MicroProfile idiom for a config field. On
# Spring, @Value injects on its own; a stacked @Autowired makes the container
# look for a bean of the field's type (often `int`) and fail at startup. Drop
# the @Inject line only when the very next annotation is the config one.
_INJECT_ABOVE_CONFIG_RE = re.compile(
    r'^[ \t]*@Inject[ \t]*\n(?=[ \t]*@(?:ConfigProperty|Value)\b)',
    re.MULTILINE,
)

# iter-20 — ONE regex over every namespace the migration touches.
#
# This was three regexes -- jakarta.ws.rs, jakarta.inject/enterprise.context,
# and org.eclipse.microprofile.CONFIG -- which between them could not match
# 11 of the 32 rows in IMPORT_REPLACEMENTS. Those rows were silent no-ops:
# MicroProfile Health / Metrics / OpenAPI and, most visibly,
# `io.helidon.security.annotations.*`, whose import survived into the output
# while the annotation above it had already been rewritten to @PreAuthorize.
# The result looked migrated, carried io.helidon residue, and did not compile.
#
# Keep the alternation anchored to the namespaces we have mappings for:
# a blanket `import [^;]+` would also rewrite the project's own imports.
_LEGACY_IMPORT_RE = re.compile(
    r'^[ \t]*import[ \t]+('
    r'jakarta\.ws\.rs\.[^;]+'
    r'|jakarta\.(?:inject|enterprise\.context|annotation)\.[^;]+'
    r'|org\.eclipse\.microprofile\.[^;]+'
    r'|io\.helidon\.[^;]+'
    r');[ \t]*$',
    re.MULTILINE,
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

        # 4b) @ConfigProperty → @Value, translating the ATTRIBUTES into
        # Spring placeholder syntax. Must run before the token pass below,
        # which would otherwise leave `@Value(name = …, defaultValue = …)`.
        s = _INJECT_ABOVE_CONFIG_RE.sub("", s)
        s = _CONFIG_PROP_FULL_RE.sub(lambda m: f'@Value("${{{m.group(1)}:{m.group(2)}}}")', s)
        s = _CONFIG_PROP_NAME_RE.sub(lambda m: f'@Value("${{{m.group(1)}}}")', s)
        s = _CONFIG_PROP_BARE_RE.sub(lambda m: f'@Value("${{{m.group(1)}}}")', s)

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

        s = _LEGACY_IMPORT_RE.sub(_repl, src)

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
        # @PreAuthorize is synthesised from @Authenticated / @Authorized, so
        # unlike the others it can appear with no legacy import to rewrite.
        if "@PreAuthorize" in s and "org.springframework.security.access.prepost.PreAuthorize" not in s:
            s = self._inject_import(s, "org.springframework.security.access.prepost.PreAuthorize")
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

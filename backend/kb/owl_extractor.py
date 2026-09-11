"""Extract OWL-ish ontology elements (classes, methods, tables, columns, relations, roles)
from PHP / Java / JSP source and SQL DDL/seed scripts."""
import re
from typing import Dict, List, Any


# ---------- Java / JSP ----------
JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
JAVA_CLASS_RE = re.compile(
    r"(?:public\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum)\s+([A-Za-z_]\w*)"
    r"(?:\s*<[^>]+>)?"
    r"(?:\s+extends\s+([\w.<>,\s]+?))?"
    r"(?:\s+implements\s+([\w.<>,\s]+?))?\s*\{",
    re.MULTILINE,
)
JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?"
    r"(?:[\w<>,\[\]\s.]+?)\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)\s*(?:throws\s+[\w.,\s]+)?\s*\{",
    re.MULTILINE,
)
JAVA_ANNOTATION_ROUTE_RE = re.compile(
    r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
    r"\s*\(\s*(?:value\s*=\s*)?[\"']([^\"']+)[\"']"
)
# iter-14.22 — Spring MVC route detection was too shallow: the old regex
# only picked up the FIRST string arg of a mapping annotation, missed
# class-level `@RequestMapping("/users")` prefixes, treated bare
# `@RequestMapping` (without a value) as a non-route, and ignored the
# `method = RequestMethod.POST` HTTP-verb arg. That's why the ceots KB
# had only 3 ROUTE entities from 1202 Java files — the Spring MVC
# controllers were all class-prefixed with method-level `@GetMapping`
# / `@PostMapping` under a `/module/*` base path, so nothing matched.
#
# The regexes below capture:
#   • Class-level controller markers (@Controller / @RestController) + any
#     class-level @RequestMapping prefix (path + method).
#   • Every method-level mapping annotation (Get/Post/Put/Delete/Patch/
#     RequestMapping), including bare `@RequestMapping` (any method).
#   • Method verb from `method = RequestMethod.POST` / `method = POST`
#     inside RequestMapping args.
# Paths are then combined (class prefix + method path).
JAVA_CONTROLLER_ANN_RE = re.compile(
    r"@(RestController|Controller)\b", re.MULTILINE,
)
JAVA_CLASS_REQMAP_RE = re.compile(
    r"@RequestMapping\s*\((?P<args>[^)]*)\)\s*(?:\r?\n\s*)*"
    r"(?:(?:public|abstract|final|static)\s+)*(?:class|interface)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
# Match a single mapping annotation and capture its arg block; also match
# the enclosing method signature that follows so we can bind the annotation
# to a handler method name.
JAVA_METHOD_MAPPING_RE = re.compile(
    r"@(?P<ann>GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
    r"(?:\s*\((?P<args>[^)]*)\))?"
    r"(?=[\s\S]{0,400}?"
    r"(?:(?:public|private|protected)\s+)?(?:static\s+|final\s+)*"
    r"[\w.$][\w<>,\[\]\s.?]*?\s+(?P<method>[A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)
# Path arg — supports `"…"`, `value="…"`, `path="…"`, and array form
# `{"/a","/b"}` (we take the first element).
_REQMAP_PATH_RE = re.compile(
    r"(?:value|path)\s*=\s*(?:\{\s*)?[\"']([^\"']+)[\"']"
    r"|^\s*(?:\{\s*)?[\"']([^\"']+)[\"']",
    re.MULTILINE,
)
_REQMAP_METHOD_RE = re.compile(
    r"method\s*=\s*(?:RequestMethod\.)?(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)",
    re.IGNORECASE,
)
_ANN_TO_VERB = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}

# ─────────────────────────────────────────────────────────────────────────────
# iter-14.81 — JAX-RS / MicroProfile (Helidon / Quarkus / Micronaut) support.
# JAX-RS uses @Path for route prefixes and @GET/@POST/@PUT/@DELETE/@PATCH at
# method level. Helidon MP/SE + Quarkus + RESTEasy all use this pattern.
# ─────────────────────────────────────────────────────────────────────────────
# Class-level @Path — captures resource root path + class name.
JAXRS_CLASS_PATH_RE = re.compile(
    r"@Path\s*\(\s*[\"'](?P<path>[^\"']+)[\"']\s*\)\s*"
    r"(?:@ApplicationScoped|@RequestScoped|@Singleton|@Dependent|@Produces|@Consumes|\s|@\w+(?:\([^)]*\))?)*?"
    r"(?:(?:public|abstract|final|static)\s+)*(?:class|interface)\s+(?P<cls>[A-Za-z_]\w*)",
    re.MULTILINE,
)
# Method-level @GET/@POST/etc. — captures verb + the handler method name.
# iter-15.20 — Previously this regex only recognised `@Path("...")` when it
# appeared IMMEDIATELY after the verb annotation. Real code very often has
# other annotations in between (`@POST` / `@SecuredPayload` / `@Path(...)`),
# which made the optional @Path group fail silently and every method-level
# route lose its path suffix (falling back to the bare class-level prefix,
# so 13 distinct endpoints on one controller all collapsed into the same
# "path"). The path is now resolved separately via `_find_method_path`,
# which scans the whole annotation block between the verb and the handler
# name — order- and intervening-annotation-agnostic.
JAXRS_METHOD_RE = re.compile(
    r"@(?P<verb>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b"
    r"(?=[\s\S]{0,400}?"
    r"(?:(?:public|private|protected)\s+)?(?:static\s+|final\s+)*"
    r"[\w.$][\w<>,\[\]\s.?]*?\s+(?P<method>[A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)
_JAXRS_METHOD_PATH_WINDOW_RE = re.compile(
    r"@Path\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
)


def _find_method_path(content: str, verb_end: int, handler_start: int) -> str:
    """iter-15.20 — Find this handler's own `@Path("...")` regardless of
    annotation order/spacing, by searching the annotation block between
    the verb annotation and the handler method name (rather than requiring
    @Path to be textually adjacent to @GET/@POST)."""
    if handler_start <= verb_end:
        return ""
    window = content[verb_end:handler_start]
    m = _JAXRS_METHOD_PATH_WINDOW_RE.search(window)
    return m.group(1) if m else ""
# CDI injection markers
JAXRS_CDI_INJECT_RE = re.compile(
    r"@Inject\b\s*(?:@Named\s*\([^)]*\))?\s*(?:private|protected|public)?\s+"
    r"(?P<type>[A-Z][A-Za-z0-9_.$<>,\s]*?)\s+(?P<field>[a-z][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
# iter-15.26 — Constructor-injection style (Lombok `@RequiredArgsConstructor`,
# or a manually-written `@Inject`-annotated constructor): the dependency is
# declared as a plain `private final Type field;` with NO `@Inject` on the
# field itself. `JAXRS_CDI_INJECT_RE` above misses this extremely common
# pattern entirely, which was the actual reason service/repository layer
# resolution failed for the `hiring-service` pilot's main controller (it
# uses exactly this style). Restricted to type names ending in a
# service/repository-ish suffix so we don't treat every private final
# field (constants, primitives, etc.) as a dependency injection.
JAVA_CONSTRUCTOR_INJECTED_FIELD_RE = re.compile(
    r"\bprivate\s+(?:final\s+)?"
    r"(?P<type>[A-Z][A-Za-z0-9_.$]*(?:Service|UseCase|Facade|Manager|Repository|Dao|Mapper|Client)\w*)"
    r"\s+(?P<field>[a-z][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
# MicroProfile Config injection
JAXRS_CONFIG_RE = re.compile(
    r"@ConfigProperty\s*\(\s*name\s*=\s*[\"'](?P<key>[^\"']+)[\"']"
    r"(?:\s*,\s*defaultValue\s*=\s*[\"'](?P<default>[^\"']*)[\"'])?\s*\)",
    re.MULTILINE,
)

# iter-15.20 — Real-world JAX-RS resources very commonly declare their
# class-level @Path using a symbolic constant instead of an inline string
# literal, e.g.:
#     @Path(VEHICLE_HIRE_MAIN_URL)
#     public class VehicleHiringController { ... }
# JAXRS_CLASS_PATH_RE (quoted-literal only) silently produced ZERO route
# entities for these classes — every single one of a codebase's real,
# inbound REST endpoints could be dropped this way while unrelated
# outbound REST-CLIENT interfaces (which tend to use inline literals for
# their sub-paths) were kept, making the discovered "API surface" both
# incomplete AND wrong. This identifier-based variant lets us at least
# detect the class + resolve the constant (see _collect_string_constants).
JAXRS_CLASS_PATH_IDENT_RE = re.compile(
    r"@Path\s*\(\s*(?P<ident>[A-Za-z_][\w.]*)\s*\)\s*"
    r"(?:@ApplicationScoped|@RequestScoped|@Singleton|@Dependent|@Produces|@Consumes|\s|@\w+(?:\([^)]*\))?)*?"
    r"(?:(?:public|abstract|final|static)\s+)*(?:class|interface)\s+(?P<cls>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# iter-15.20 — `String NAME = "value";` constant declarations (any
# visibility/modifier combination, `static final` or not). Used to resolve
# symbolic `@Path`/`@RequestMapping` args that reference a constant instead
# of an inline string literal. Deliberately permissive on modifiers since
# real codebases are inconsistent about `public static final` ordering.
_JAVA_STRING_CONST_RE = re.compile(
    r"(?:public\s+|private\s+|protected\s+|static\s+|final\s+)*"
    r"String\s+([A-Za-z_]\w*)\s*=\s*\"([^\"]*)\"\s*;",
)

# iter-15.20 — Markers for OUTBOUND REST client interfaces (the app CALLING
# another service), as opposed to INBOUND REST resources (the app's own
# exposed API). Both MicroProfile Rest Client and Spring Cloud OpenFeign use
# the same `@Path`/`@GetMapping`-style annotations as real controllers, so
# without this distinction outbound client stubs get counted as if they
# were the service's own API surface — exactly backwards for a migration
# tool whose job is to discover what THIS service actually exposes.
JAXRS_CLIENT_MARKER_RE = re.compile(
    r"@(?:RegisterRestClient|FeignClient)\b", re.MULTILINE,
)


def _collect_string_constants(content: str) -> Dict[str, str]:
    """iter-15.20 — Map of CONST_NAME -> literal value for every
    `String NAME = "value";` declaration in this file. Used to resolve
    `@Path(SOME_CONSTANT)` / `@RequestMapping(SOME_CONSTANT)` references."""
    out: Dict[str, str] = {}
    for m in _JAVA_STRING_CONST_RE.finditer(content):
        out[m.group(1)] = m.group(2)
    return out


def _resolve_path_ident(ident: str, constants: Dict[str, str]) -> str:
    """Resolve a symbolic @Path/@RequestMapping argument to its literal
    string value using `constants` (local file constants merged with a
    cross-file global map). Handles simple `Foo.BAR` qualified references
    by trying the bare name too. Falls back to the identifier itself
    (so we still emit *a* route rather than silently dropping the whole
    resource) when it cannot be resolved."""
    if not ident:
        return ""
    bare = ident.rsplit(".", 1)[-1]
    if ident in constants:
        return constants[ident]
    if bare in constants:
        return constants[bare]
    return ""


def _parse_reqmap_args(args: str) -> tuple[str, str]:
    """Return (path, http_method) parsed from a @RequestMapping arg block.
    Empty path/method mean 'not specified'."""
    if not args:
        return "", ""
    path = ""
    pm = _REQMAP_PATH_RE.search(args)
    if pm:
        path = pm.group(1) or pm.group(2) or ""
    verb = ""
    vm = _REQMAP_METHOD_RE.search(args)
    if vm:
        verb = vm.group(1).upper()
    return path, verb


def _combine_route(prefix: str, tail: str) -> str:
    """Merge class-level path prefix with method path (Spring semantics)."""
    if not prefix and not tail:
        return "/"
    if not prefix:
        return tail if tail.startswith("/") else "/" + tail
    if not tail:
        return prefix if prefix.startswith("/") else "/" + prefix
    a = prefix.rstrip("/")
    if not a.startswith("/"):
        a = "/" + a
    b = tail if tail.startswith("/") else "/" + tail
    return a + b
JAVA_JPA_TABLE_RE = re.compile(r"@Table\s*\(\s*name\s*=\s*[\"']([a-zA-Z_]\w*)[\"']")
JAVA_JPA_ENTITY_RE = re.compile(r"@Entity\b")

# iter-14.23 — Field-level evidence extraction. Struts 1 ActionForm
# subclasses + plain DTO classes carry the request/response field shapes
# that CodeGen needs to populate DTO records. Without them the LLM sees a
# ROUTE with no field detail and (per the anti-hallucination contract)
# emits `⚠ EVIDENCE GAP` markers → empty records → confidence collapse.
#
# We extract fields from:
#   • `private <Type> <name>;` declarations
#   • JavaBean accessors `public <Type> get<Name>()` / `set<Name>(<Type> …)`
# and mark a field `required` when it (or its setter) carries
# @NotNull / @NotBlank / @NotEmpty / @Required / @Valid(required).
JAVA_ACTIONFORM_RE = re.compile(
    r"\bclass\s+([A-Za-z_]\w*)\b[\s\S]{0,200}?\bextends\s+([\w.]*"
    r"(?:ActionForm|ValidatorForm|ValidatorActionForm|DynaActionForm|DynaValidatorForm))\b",
    re.MULTILINE,
)
JAVA_FIELD_DECL_RE = re.compile(
    r"(?:^|\n|;|\{)\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*"
    r"(?:private|protected|public)\s+(?:static\s+|final\s+|transient\s+|volatile\s+)*"
    r"([\w.$]+(?:\s*<[^;=]+>)?(?:\s*\[\s*\])?)\s+"
    r"([a-zA-Z_]\w*)\s*(?=[=;])",
    re.MULTILINE,
)
JAVA_GETTER_RE = re.compile(
    r"\bpublic\s+([\w.$]+(?:\s*<[^>]+>)?(?:\s*\[\s*\])?)\s+get([A-Z]\w*)\s*\(\s*\)",
)
JAVA_REQUIRED_ANN_RE = re.compile(
    r"@(?:NotNull|NotBlank|NotEmpty|Required|Nonnull|NonNull)\b",
)
# Spring MVC request-body / model-attribute + response-entity resolution.
JAVA_REQUEST_BODY_RE = re.compile(
    r"@(?:RequestBody|ModelAttribute)(?:\s*\([^)]*\))?\s+(?:final\s+)?([A-Za-z_][\w.]*)\s+\w+",
)
JAVA_RESPONSE_ENTITY_RE = re.compile(
    r"(?:ResponseEntity|HttpEntity)\s*<\s*([A-Za-z_][\w.]*)\s*>",
)
JAVA_HANDLER_SIG_RE = re.compile(
    r"(?:(?:public|private|protected)\s+)?(?:static\s+|final\s+)*"
    r"([\w.$]+(?:\s*<[^>]+>)?)\s+"
    r"([A-Za-z_]\w*)\s*\(([^)]*)\)",
)
# iter-14.30 — Fixed SQL inline regex to properly handle:
# 1. SELECT col1, col2 FROM table_name
# 2. SELECT * FROM schema.table_name (extract table_name, not schema)
# 3. INSERT INTO schema.table_name
# 4. UPDATE schema.table_name SET ...
# 5. DELETE FROM schema.table_name
# The key fix: use negative lookahead for FROM to find actual table names
JAVA_SQL_FROM_RE = re.compile(
    r"\bFROM\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
JAVA_SQL_INTO_RE = re.compile(
    r"\bINTO\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
JAVA_SQL_UPDATE_RE = re.compile(
    r"\bUPDATE\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
JAVA_SQL_JOIN_RE = re.compile(
    r"\bJOIN\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
# Legacy regex kept for backward compat but rarely matches correctly
JAVA_SQL_INLINE_RE = re.compile(
    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:[\w.`\"]+\s+)*?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
# iter-14.30 — Fixed: handle schema.table notation, extract table_name not schema
JSP_SCRIPTLET_TABLE_RE = re.compile(
    r"(?:from|into|update|join)\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE
)
JSP_FORM_ACTION_RE = re.compile(r"<form[^>]+action\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
JSP_INCLUDE_RE = re.compile(r"<%@\s*include\s+file\s*=\s*[\"']([^\"']+)[\"']")
# iter-14.23 — JSP model-attribute bindings (top-level EL vars) so a Struts
# forward's target JSP can supply the response_fields shape when no explicit
# response DTO exists.
JSP_EL_BINDING_RE = re.compile(r"\$\{\s*([A-Za-z_]\w*)")

# Non-domain field types we never surface as DTO fields (framework plumbing).
_FIELD_TYPE_SKIP = {
    "logger", "log", "serialversionuid", "servletcontext", "httpservletrequest",
    "httpservletresponse", "actionmapping", "actionform", "actionforward",
    "actionmessages", "actionerrors", "datasource", "connection",
}
_FIELD_NAME_SKIP = {"serialversionuid", "logger", "log"}


def _normalise_java_type(raw: str) -> str:
    """Collapse a raw Java type token to a compact, prompt-friendly form.
    `java.math.BigDecimal` → `BigDecimal`, `List<String>` preserved."""
    t = (raw or "").strip()
    # Strip a leading FQN package on the *outer* type only (keep generics).
    outer = t
    gen = ""
    lt = t.find("<")
    if lt >= 0:
        outer = t[:lt].strip()
        gen = t[lt:].strip()
    if "." in outer and "[" not in outer:
        outer = outer.rsplit(".", 1)[-1]
    return (outer + gen).strip()


def _extract_java_fields(cls_body: str) -> List[Dict[str, Any]]:
    """Enumerate JavaBean fields (declarations + accessors) from a class body.

    Returns [{name, java_type, required}] de-duplicated by field name,
    preferring an explicit declaration type over a getter-inferred one.
    """
    fields: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def _put(name: str, jtype: str, required: bool):
        if not name:
            return
        key = name
        jt = _normalise_java_type(jtype)
        if jt.lower() in _FIELD_TYPE_SKIP or name.lower() in _FIELD_NAME_SKIP:
            return
        existing = fields.get(key)
        if existing is None:
            fields[key] = {"name": name, "java_type": jt or "String", "required": bool(required)}
            order.append(key)
        else:
            # Upgrade type if we now have a concrete (non-Object) one; OR
            # in the required flag.
            if (existing["java_type"] in ("Object", "String") and jt and jt not in ("Object",)):
                existing["java_type"] = jt
            existing["required"] = existing["required"] or bool(required)

    # 1) Explicit field declarations (carry the @NotNull etc. directly above).
    for m in JAVA_FIELD_DECL_RE.finditer(cls_body):
        jtype = m.group(1)
        name = m.group(2)
        # The declaration regex prefix already swallows leading annotations,
        # so a required-marker on this field is inside the match text itself.
        required = bool(JAVA_REQUIRED_ANN_RE.search(m.group(0)))
        _put(name, jtype, required)

    # 2) Getter accessors — recover fields that only expose via accessors
    #    (DynaActionForm-style or Lombok-hidden declarations).
    for m in JAVA_GETTER_RE.finditer(cls_body):
        jtype = m.group(1)
        prop = m.group(2)
        name = prop[0].lower() + prop[1:]
        _put(name, jtype, False)

    return [fields[k] for k in order]


_JAXRS_NON_BODY_TYPES = {
    "String", "int", "long", "boolean", "double", "float", "int[]", "long[]",
    "Integer", "Long", "Boolean", "Double", "Float", "BigDecimal",
    "HttpHeaders", "HttpServletRequest", "HttpServletResponse", "UriInfo",
    "SecurityContext", "Request", "ContainerRequestContext", "Providers",
    "Sse", "SseEventSink", "AsyncResponse", "Object", "Void", "Response",
    "MultivaluedMap",
}
_JAXRS_PARAM_ANNOTATION_SKIP_RE = re.compile(
    r"@(?:Context|PathParam|QueryParam|HeaderParam|CookieParam|FormParam|Suspended)\b"
)
_JAXRS_PARAM_DECL_RE = re.compile(
    r"^(?:@[\w.]+(?:\([^)]*\))?\s*)*([A-Za-z_][\w.]*)\s*(?:<[^>]*>)?\s*(?:\[\s*\])?\s+\w+$"
)


def _split_top_level_params(params: str) -> List[str]:
    """Split a Java parameter list on top-level commas (ignoring commas
    inside generics `<...>` or annotation args `(...)`)."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in params:
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _parse_handler_signature(window: str) -> Dict[str, str]:
    """From a code window starting at a mapping annotation, resolve the
    handler method's request-body/model-attribute class and response class."""
    out = {"request_body_class": "", "response_body_class": ""}
    sig = JAVA_HANDLER_SIG_RE.search(window)
    if not sig:
        return out
    ret_type = sig.group(1) or ""
    params = sig.group(3) or ""
    rb = JAVA_REQUEST_BODY_RE.search(params)
    if rb:
        out["request_body_class"] = rb.group(1).rsplit(".", 1)[-1]
    else:
        # iter-15.26 — JAX-RS handlers don't use @RequestBody/@ModelAttribute
        # at all; the JSON body is whichever parameter has no @PathParam/
        # @QueryParam/@HeaderParam/@Context annotation (optionally just
        # @Valid). Fall back to that convention so request DTOs are
        # resolved for JAX-RS/Helidon/Quarkus/MicroProfile code, not just
        # Spring MVC.
        for part in _split_top_level_params(params):
            if _JAXRS_PARAM_ANNOTATION_SKIP_RE.search(part):
                continue
            m2 = _JAXRS_PARAM_DECL_RE.match(part)
            if not m2:
                continue
            ptype = m2.group(1).rsplit(".", 1)[-1]
            if not ptype or ptype[0:1].islower() or ptype in _JAXRS_NON_BODY_TYPES:
                continue
            out["request_body_class"] = ptype
            break
    re_m = JAVA_RESPONSE_ENTITY_RE.search(ret_type)
    if re_m:
        out["response_body_class"] = re_m.group(1).rsplit(".", 1)[-1]
    else:
        rt = _normalise_java_type(ret_type)
        # A custom, non-primitive, non-framework return type is a response DTO.
        # iter-15.26 — Added "Response" (javax.ws.rs.core.Response, the
        # generic JAX-RS wrapper almost every Helidon/Quarkus/RESTEasy
        # handler returns) — it was being misreported as if it were
        # itself a response DTO class, which is misleading in the
        # envelope UI (the actual payload type is buried inside
        # `Response.ok(...)`, not statically resolvable by this regex
        # pass, and is left as "not resolved" instead of a wrong guess).
        if rt and rt[0:1].isupper() and rt not in (
            "String", "Void", "Object", "ModelAndView", "ResponseEntity",
            "HttpEntity", "ResponseBody", "Response",
        ) and "<" not in rt:
            out["response_body_class"] = rt
    return out


def extract_java(content: str, filename: str = "", global_constants: Dict[str, str] = None) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    pkg_m = JAVA_PACKAGE_RE.search(content)
    package = pkg_m.group(1) if pkg_m else ""
    is_jpa = bool(JAVA_JPA_ENTITY_RE.search(content))

    # iter-15.20 — Local file constants take precedence over the cross-file
    # global map (a file redefining a same-named constant should win for
    # itself), but most real cases are a static import from a shared
    # constants class, which only the global map can resolve.
    path_constants: Dict[str, str] = dict(global_constants or {})
    path_constants.update(_collect_string_constants(content))

    # iter-15.20 — Outbound REST-CLIENT interfaces (MicroProfile Rest
    # Client / Spring Cloud OpenFeign) use the exact same @Path/@GetMapping
    # style annotations as real inbound resources. Flag every route this
    # file produces so downstream KB/envelope logic can tell "APIs this
    # service exposes" apart from "APIs this service calls out to".
    is_outbound_client = bool(JAXRS_CLIENT_MARKER_RE.search(content))

    # iter-14.22 — Full Spring MVC route detection (class-level +
    # method-level combined). Falls back to the legacy single-regex
    # scan for JAX-RS / non-controller files that carry mapping
    # annotations outside a controller (utility resources etc.).
    is_controller = bool(JAVA_CONTROLLER_ANN_RE.search(content))

    # Pull class-level @RequestMapping prefix (path + optional method).
    # We anchor on the class definition so we don't accidentally attach
    # a nested annotation to the wrong class.
    class_prefix_path = ""
    class_prefix_verb = ""
    class_name_for_prefix = ""
    class_prefix_ann_start: int = -1
    cprefix_m = None
    for m in JAVA_CLASS_REQMAP_RE.finditer(content):
        p, v = _parse_reqmap_args(m.group("args"))
        class_prefix_path = p
        class_prefix_verb = v
        class_name_for_prefix = m.group(2)
        cprefix_m = m
        class_prefix_ann_start = m.start()
        break  # only outermost controller class

    emitted_route_offsets: set = set()
    if is_controller or cprefix_m:
        # Bind method-level mappings to their handler names
        for m in JAVA_METHOD_MAPPING_RE.finditer(content):
            # Skip the class-level @RequestMapping — JAVA_METHOD_MAPPING_RE
            # would otherwise re-bind it to the first handler method it
            # finds, producing a bogus doubled path like /api/users/api/users.
            if m.start() == class_prefix_ann_start:
                continue
            ann = m.group("ann")
            args = m.group("args") or ""
            handler = m.group("method") or ""
            path_tail, verb_arg = _parse_reqmap_args(args)
            verb = _ANN_TO_VERB.get(ann) or verb_arg or class_prefix_verb or "ANY"
            # If bare @RequestMapping without path, use class prefix alone.
            full_path = _combine_route(class_prefix_path, path_tail)
            # Skip stray annotation matches on non-handler fields — the
            # trailing method group must be a real handler-looking name.
            if handler and full_path:
                # iter-14.23 — resolve @RequestBody/@ModelAttribute request
                # DTO + ResponseEntity<…> / custom return DTO so the aggregator
                # can inline request_fields / response_fields onto the ROUTE.
                sig = _parse_handler_signature(content[m.start():m.start() + 600])
                route_ent = {
                    "type": "ROUTE",
                    "verb": verb,
                    "http_method": verb,
                    "name": full_path,
                    "handler_class": class_name_for_prefix,
                    "handler_method": handler,
                    "framework": "spring-mvc",
                    "source": filename,
                    "is_outbound_client": is_outbound_client,
                }
                if sig.get("request_body_class"):
                    route_ent["request_body_class"] = sig["request_body_class"]
                if sig.get("response_body_class"):
                    route_ent["response_body_class"] = sig["response_body_class"]
                entities.append(route_ent)
                emitted_route_offsets.add(m.start())
        # Class-level route with no method-level (rare — @RestController
        # with a single class-mapping and dispatch inside).
        if class_prefix_path and not emitted_route_offsets:
            entities.append({
                "type": "ROUTE",
                "verb": class_prefix_verb or "ANY",
                "http_method": class_prefix_verb or "ANY",
                "name": class_prefix_path if class_prefix_path.startswith("/") else "/" + class_prefix_path,
                "is_outbound_client": is_outbound_client,
                "handler_class": class_name_for_prefix,
                "handler_method": "",
                "framework": "spring-mvc",
                "source": filename,
            })
    else:
        # ─────────────────────────────────────────────────────────────────────
        # iter-14.81 — JAX-RS / Helidon / Quarkus / MicroProfile route detection.
        # If the file has a class-level @Path, extract JAX-RS resources.
        # iter-15.20 — @Path can reference a symbolic constant instead of an
        # inline literal (e.g. `@Path(VEHICLE_HIRE_MAIN_URL)`); try the
        # quoted-literal regex first, then fall back to the identifier form
        # and resolve it via path_constants so real controllers using this
        # (very common) pattern aren't silently skipped entirely.
        # ─────────────────────────────────────────────────────────────────────
        jaxrs_class_path = ""
        jaxrs_class_name = ""
        jaxrs_path_unresolved = False
        for cp in JAXRS_CLASS_PATH_RE.finditer(content):
            jaxrs_class_path = cp.group("path")
            jaxrs_class_name = cp.group("cls")
            break  # take first resource class
        if not jaxrs_class_path:
            for cp in JAXRS_CLASS_PATH_IDENT_RE.finditer(content):
                jaxrs_class_name = cp.group("cls")
                resolved = _resolve_path_ident(cp.group("ident"), path_constants)
                if resolved:
                    jaxrs_class_path = resolved
                else:
                    # Couldn't resolve the constant (e.g. it's imported from
                    # a file outside this upload). Still emit method-level
                    # routes below using an empty prefix rather than
                    # dropping the whole resource — a route with a
                    # slightly-wrong/missing prefix is far more useful to a
                    # migration reviewer than a completely missing endpoint.
                    jaxrs_class_path = ""
                    jaxrs_path_unresolved = True
                break

        jaxrs_routes_emitted = set()
        if jaxrs_class_name and (jaxrs_class_path or jaxrs_path_unresolved):
            # This is a JAX-RS resource — extract @GET/@POST/etc methods.
            for m in JAXRS_METHOD_RE.finditer(content):
                verb = m.group("verb")
                handler = m.group("method") or ""
                method_path = _find_method_path(content, m.end("verb"), m.start("method")) if handler else ""
                full_path = _combine_route(jaxrs_class_path, method_path)
                if handler and full_path:
                    sig = _parse_handler_signature(content[m.start():m.start() + 600])
                    route_ent = {
                        "type": "ROUTE",
                        "verb": verb,
                        "http_method": verb,
                        "name": full_path,
                        "handler_class": jaxrs_class_name,
                        "handler_method": handler,
                        "framework": "jaxrs",  # Helidon/Quarkus/RESTEasy
                        "source": filename,
                        "is_outbound_client": is_outbound_client,
                    }
                    if jaxrs_path_unresolved:
                        route_ent["path_prefix_unresolved"] = True
                    if sig.get("request_body_class"):
                        route_ent["request_body_class"] = sig["request_body_class"]
                    if sig.get("response_body_class"):
                        route_ent["response_body_class"] = sig["response_body_class"]
                    entities.append(route_ent)
                    jaxrs_routes_emitted.add(m.start())
            # Root-level resource with no method paths
            if jaxrs_class_path and not jaxrs_routes_emitted:
                entities.append({
                    "type": "ROUTE",
                    "verb": "ANY",
                    "http_method": "ANY",
                    "name": jaxrs_class_path if jaxrs_class_path.startswith("/") else "/" + jaxrs_class_path,
                    "handler_class": jaxrs_class_name,
                    "handler_method": "",
                    "framework": "jaxrs",
                    "source": filename,
                    "is_outbound_client": is_outbound_client,
                })
        
        # Legacy fallback for Spring @*Mapping without @Controller annotation.
        if not jaxrs_routes_emitted:
            for m in JAVA_ANNOTATION_ROUTE_RE.finditer(content):
                entities.append({"type": "ROUTE",
                                 "verb": m.group(1).replace("Mapping", "").upper(),
                                 "http_method": m.group(1).replace("Mapping", "").upper(),
                                 "name": m.group(2), "source": filename,
                                 "is_outbound_client": is_outbound_client})
    
    # ─────────────────────────────────────────────────────────────────────────
    # iter-14.81 — CDI injection + MicroProfile config extraction.
    # These are common to Helidon, Quarkus, and MicroProfile-based stacks.
    # ─────────────────────────────────────────────────────────────────────────
    for inj in JAXRS_CDI_INJECT_RE.finditer(content):
        entities.append({
            "type": "CDI_INJECTION",
            "injected_type": inj.group("type").strip(),
            "field_name": inj.group("field"),
            "source": filename,
        })

    # iter-15.26 — Constructor-injection style dependencies (see regex
    # comment above). Dedupe against field-style @Inject matches on the
    # same field name so a class using BOTH styles doesn't double-count.
    _already_injected_fields = {
        e["field_name"] for e in entities if e.get("type") == "CDI_INJECTION"
    }
    for inj in JAVA_CONSTRUCTOR_INJECTED_FIELD_RE.finditer(content):
        field = inj.group("field")
        if field in _already_injected_fields:
            continue
        _already_injected_fields.add(field)
        entities.append({
            "type": "CDI_INJECTION",
            "injected_type": inj.group("type").strip(),
            "field_name": field,
            "source": filename,
            "via": "constructor",
        })

    for cfg in JAXRS_CONFIG_RE.finditer(content):
        entities.append({
            "type": "CONFIG_PROPERTY",
            "key": cfg.group("key"),
            "default_value": cfg.group("default") or None,
            "source": filename,
        })

    # JPA table mappings
    for m in JAVA_JPA_TABLE_RE.finditer(content):
        entities.append({"type": "TABLE_HINT", "name": m.group(1), "source": filename,
                         "via": "JPA @Table"})

    # Classes
    _FORM_BASES = ("actionform", "validatorform", "validatoractionform",
                   "dynaactionform", "dynavalidatorform")
    for cls_m in JAVA_CLASS_RE.finditer(content):
        cls_name = cls_m.group(1)
        extends = (cls_m.group(2) or "").strip()
        implements = [s.strip() for s in (cls_m.group(3) or "").split(",") if s.strip()]
        brace_start = cls_m.end() - 1
        # crude: take 8000 chars window after class brace for method scan
        cls_body = content[brace_start:brace_start + 8000]
        methods = []
        for mm in JAVA_METHOD_RE.finditer(cls_body):
            mname = mm.group(1)
            if mname in {"if", "for", "while", "switch", "catch"}:
                continue
            mstart = mm.start()
            window = cls_body[mstart:mstart + 1500]
            tables = set()
            # iter-14.30 — Use multiple SQL regexes to properly extract table names
            # from schema.table notation (e.g., FROM ceots_v3.users → users)
            for tm in JAVA_SQL_FROM_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JAVA_SQL_INTO_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JAVA_SQL_UPDATE_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JAVA_SQL_JOIN_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            # Fallback to legacy regex for edge cases
            for tm in JAVA_SQL_INLINE_RE.finditer(window):
                t = tm.group(1).strip("`\"")
                # Skip if it looks like a schema name (will be followed by .table)
                if t and not re.search(rf"\b{re.escape(t)}\.\w", window):
                    tables.add(t)
            methods.append({"name": mname, "params": mm.group(2).strip(),
                            "tables": sorted(tables), "sessions": []})
        # iter-14.23 — field-level shape for DTO / form resolution. Use a
        # wider body window than the method scan (fields may sit below a
        # long block of accessors).
        field_body = content[brace_start:brace_start + 20000]
        fields = _extract_java_fields(field_body)
        fq_name = f"{package}.{cls_name}" if package else cls_name
        entities.append({
            "type": "CLASS", "name": cls_name, "namespace": package,
            "extends": extends, "implements": implements,
            "is_jpa_entity": is_jpa, "methods": methods,
            "fields": fields, "source": filename,
        })
        # Struts ActionForm subclass → emit FORM_FIELD entities so the
        # aggregator can attach them to the ROUTE whose form-bean resolves
        # to this class.
        extends_simple = extends.split("<", 1)[0].rsplit(".", 1)[-1].strip().lower()
        if extends_simple in _FORM_BASES:
            for fld in fields:
                entities.append({
                    "type": "FORM_FIELD",
                    "name": fld["name"],
                    "owner_class": fq_name,
                    "owner_simple": cls_name,
                    "java_type": fld["java_type"],
                    "required": bool(fld.get("required")),
                    "framework": "struts1",
                    "source_file": filename,
                    "source": filename,
                })
    return entities


def extract_jsp(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    # Form actions → routes (UI ↔ controller wiring).
    # iter-14.22 — also emit a synthetic ROUTE for every form action so
    # arch.recommend can count JSP-form-driven flows as part of the
    # legacy API surface (Struts 1 / plain-JSP apps rarely expose
    # controller annotations, so JSP forms are the primary evidence).
    for m in JSP_FORM_ACTION_RE.finditer(content):
        action = m.group(1)
        entities.append({"type": "JSP_FORM", "action": action, "source": filename})
        # Normalise action into a route path. Skip empty, `#`, `javascript:`,
        # absolute URLs (http[s]://) and mailto: — those aren't legacy APIs.
        norm = (action or "").strip()
        low = norm.lower()
        if (norm and not norm.startswith("#")
                and not low.startswith(("javascript:", "mailto:", "http://", "https://"))):
            path = norm if norm.startswith("/") else "/" + norm
            # Strip query string — the path is what routes.
            path = path.split("?", 1)[0]
            entities.append({
                "type": "ROUTE",
                "verb": "POST",
                "http_method": "POST",
                "name": path,
                "framework": "jsp-form",
                "handler_class": "",
                "handler_method": "",
                "source": filename,
            })
    # Include directives → page composition
    for m in JSP_INCLUDE_RE.finditer(content):
        entities.append({"type": "JSP_INCLUDE", "file": m.group(1), "source": filename})
    # iter-14.23 — JSP model-attribute bindings. Top-level EL variables
    # (`${foo.bar}`) are the model the controller forwarded to this view;
    # a Struts action that forwards here exposes those as response_fields
    # when no explicit response DTO is declared. Filter out implicit JSP
    # scopes so we don't surface `param` / `pageContext` as domain fields.
    _EL_SKIP = {"param", "paramvalues", "header", "headervalues", "cookie",
                "initparam", "pagecontext", "request", "response", "session",
                "application", "requestscope", "sessionscope", "applicationscope",
                "pagescope"}
    bindings: List[str] = []
    seen_b: set = set()
    for m in JSP_EL_BINDING_RE.finditer(content):
        var = m.group(1)
        if var.lower() in _EL_SKIP or var in seen_b:
            continue
        seen_b.add(var)
        bindings.append(var)
    if bindings:
        entities.append({
            "type": "JSP_MODEL",
            "name": filename.rsplit("/", 1)[-1],
            "bindings": bindings[:40],
            "source": filename,
        })
    # Inline SQL hints (legacy scriptlet apps)
    tables = set()
    for tm in JSP_SCRIPTLET_TABLE_RE.finditer(content):
        tables.add(tm.group(1).strip())
    if tables:
        entities.append({"type": "JSP_TABLE_REFS", "tables": sorted(tables), "source": filename})
    return entities


# ---------- PHP ----------
PHP_CLASS_RE = re.compile(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:extends\s+([A-Za-z_][A-Za-z0-9_\\]*))?", re.MULTILINE)
PHP_NAMESPACE_RE = re.compile(r"namespace\s+([A-Za-z0-9_\\]+)\s*;")
PHP_METHOD_RE = re.compile(r"(public|private|protected)\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", re.MULTILINE)
PHP_SESSION_RE = re.compile(r"session\(\)?->(?:get\(['\"]([^'\"]+)['\"]\)|userdata\(['\"]([^'\"]+)['\"]\))")
PHP_DB_TABLE_RE = re.compile(r"->(?:table|from|join|get|insert|update|delete)\(\s*['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]")
PHP_ROUTE_RE = re.compile(r"\$routes->(?:get|post|put|delete|match)\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")


def extract_php(content: str, filename: str = "") -> List[Dict[str, Any]]:
    """Returns list of entity dicts."""
    entities: List[Dict[str, Any]] = []
    namespace_match = PHP_NAMESPACE_RE.search(content)
    namespace = namespace_match.group(1) if namespace_match else ""

    # Routes
    for m in PHP_ROUTE_RE.finditer(content):
        entities.append({
            "type": "ROUTE",
            "name": m.group(1),
            "handler": m.group(2),
            "source": filename,
        })

    # Classes
    for cls_match in PHP_CLASS_RE.finditer(content):
        cls_name = cls_match.group(1)
        extends = cls_match.group(2) or ""
        cls_start = cls_match.start()
        # find matching brace block
        brace_start = content.find("{", cls_start)
        if brace_start < 0:
            continue
        # naive: take until end of file
        cls_body = content[brace_start:]

        methods: List[Dict[str, Any]] = []
        for mm in PHP_METHOD_RE.finditer(cls_body):
            method_name = mm.group(2)
            params = mm.group(3).strip()
            # scan ~600 chars after method start for session/db refs
            mstart = mm.start()
            window = cls_body[mstart:mstart + 1500]
            sessions = set()
            tables = set()
            for sm in PHP_SESSION_RE.finditer(window):
                sessions.add(sm.group(1) or sm.group(2))
            for tm in PHP_DB_TABLE_RE.finditer(window):
                tables.add(tm.group(1))
            methods.append({
                "name": method_name,
                "params": params,
                "sessions": sorted([s for s in sessions if s]),
                "tables": sorted(tables),
            })

        # Sessions across the class
        sess_all = set()
        for sm in PHP_SESSION_RE.finditer(cls_body):
            sess_all.add(sm.group(1) or sm.group(2))

        entities.append({
            "type": "CLASS",
            "name": cls_name,
            "namespace": namespace,
            "extends": extends,
            "methods": methods,
            "session_fields": sorted([s for s in sess_all if s]),
            "source": filename,
        })

    return entities


# ---------- SQL ----------
# Permissively match CREATE TABLE … (...) <anything-up-to-;>
# The previous version required the closing paren to be immediately followed
# by ENGINE or ;, which silently dropped tables in MySQL (`) DEFAULT CHARSET=…;`),
# PostgreSQL (`) TABLESPACE …;`), MSSQL (`) ON [PRIMARY];`), SQLite
# (`) WITHOUT ROWID;`), and any dump with a newline between `)` and `;`.
# Using a balanced-paren capture so nested types like DECIMAL(10,2) don't trip us up.
SQL_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMPORARY\s+|TEMP\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[`\"\[]?[a-zA-Z_][\w]*[`\"\]]?\.)?"           # optional schema/db prefix
    r"[`\"\[]?([a-zA-Z_][\w]*)[`\"\]]?\s*"             # table name (captured)
    r"\((.*?)\)\s*"                                    # body (captured, lazy)
    r"(?:ENGINE|DEFAULT|CHARSET|COLLATE|COMMENT|TABLESPACE|WITHOUT|ON|WITH|AUTO_INCREMENT|PARTITION|;|$)",
    re.IGNORECASE | re.DOTALL,
)
SQL_PK_RE = re.compile(r"PRIMARY\s+KEY\s*\(\s*[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s*\)", re.IGNORECASE)
SQL_FK_RE = re.compile(
    r"FOREIGN\s+KEY\s*\(\s*[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s*\)\s*REFERENCES\s+[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s*\(\s*[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?",
    re.IGNORECASE,
)
SQL_COLUMN_RE = re.compile(
    r"^\s*[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s+([a-zA-Z]+(?:\([^)]*\))?)",
    re.MULTILINE,
)
SQL_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?\s*\([^)]*\)\s*VALUES\s*(.+?);",
    re.IGNORECASE | re.DOTALL,
)


def extract_sql(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []

    for tbl_match in SQL_CREATE_TABLE_RE.finditer(content):
        table_name = tbl_match.group(1)
        body = tbl_match.group(2)

        # PK
        pk_match = SQL_PK_RE.search(body)
        pk = pk_match.group(1) if pk_match else ""

        # FKs
        fks = []
        for fkm in SQL_FK_RE.finditer(body):
            fks.append({
                "column": fkm.group(1),
                "ref_table": fkm.group(2),
                "ref_column": fkm.group(3),
            })

        # Columns: parse line-by-line
        columns = []
        seen = set()
        for line in body.split(","):
            line = line.strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith(("PRIMARY", "KEY", "INDEX", "UNIQUE", "FOREIGN", "CONSTRAINT")):
                continue
            cm = SQL_COLUMN_RE.match(line)
            if cm:
                col_name = cm.group(1)
                col_type = cm.group(2)
                if col_name.upper() in ("PRIMARY", "KEY", "INDEX", "UNIQUE", "FOREIGN", "CONSTRAINT"):
                    continue
                if col_name in seen:
                    continue
                seen.add(col_name)
                columns.append({"name": col_name, "type": col_type})

        entities.append({
            "type": "TABLE",
            "name": table_name,
            "pk": pk,
            "columns": columns,
            "fks": fks,
            "source": filename,
        })

    # INSERT statements — extract roles / lookup individuals from common tables
    for ins in SQL_INSERT_RE.finditer(content):
        tbl = ins.group(1)
        vals = ins.group(2)
        if any(k in tbl.lower() for k in ["role", "stage", "circle", "agency"]):
            # Pull quoted strings
            names = re.findall(r"'([^']{2,80})'", vals)
            for nm in set(names):
                if not nm.replace("-", "").replace("_", "").replace(" ", "").isdigit() and len(nm) > 1:
                    entities.append({
                        "type": "INDIVIDUAL",
                        "name": nm,
                        "category": tbl,
                        "source": filename,
                    })

    return entities


# ─────────────────────────────────────────────────────────────────────────────
# Language-agnostic extractors — Python, .NET (C#/VB), JS/TS
# These keep the rest of the pipeline (TOON, business ontology, SRS prompts,
# Service Map) honest for non-PHP/Java legacy stacks. They are intentionally
# regex-only (no AST deps) so they work on partial / non-compiling files.
# Each emits the same shapes as extract_java/extract_php:
#   CLASS {name, namespace, methods[{name, params, tables, sessions}], source}
#   ROUTE {verb, name, handler?, source}
#   TABLE_HINT {name, source, via}
# ─────────────────────────────────────────────────────────────────────────────

# ---------- Python (Flask / Django / FastAPI / generic) ----------
PY_CLASS_RE = re.compile(
    r"^\s*class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*:",
    re.MULTILINE,
)
PY_DEF_RE = re.compile(
    r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->[^:]+)?\s*:",
    re.MULTILINE,
)
# Flask / FastAPI:  @app.get("/path")   @router.post("/users/{id}")
PY_DECORATOR_ROUTE_RE = re.compile(
    r"@(?:[A-Za-z_]\w*)\.(get|post|put|delete|patch|route)\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
# Django urls.py:  path("login/", views.login)   re_path(r"^api/", ...)
PY_DJANGO_URL_RE = re.compile(
    r"\b(?:path|re_path|url)\(\s*[r]?[\"']([^\"']+)[\"']\s*,\s*([\w\.]+)",
)
# Django models — class Foo(models.Model):  + Meta.db_table = "users"
PY_DJANGO_MODEL_RE = re.compile(
    r"class\s+([A-Za-z_]\w*)\s*\(\s*(?:models\.)?Model\s*\)\s*:",
)
PY_DB_TABLE_RE = re.compile(r"db_table\s*=\s*[\"']([a-zA-Z_]\w*)[\"']")
# SQL hints in cursor.execute("SELECT ... FROM users ...")
# iter-14.30 — Fixed: handle schema.table notation
PY_SQL_FROM_RE = re.compile(r"\bFROM\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
PY_SQL_INTO_RE = re.compile(r"\bINTO\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
PY_SQL_UPDATE_RE = re.compile(r"\bUPDATE\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
PY_SQL_JOIN_RE = re.compile(r"\bJOIN\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
# Legacy regex kept for backward compat
PY_SQL_INLINE_RE = re.compile(
    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:[\w.`\"]+\s+)*?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)
PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", re.MULTILINE)


def extract_python(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []

    # Module namespace = dotted path inferred from filename
    namespace = ""
    if filename:
        rel = filename.replace("\\", "/").rstrip(".py")
        namespace = rel.replace("/", ".").lstrip(".")

    # Routes — Flask/FastAPI decorators
    for m in PY_DECORATOR_ROUTE_RE.finditer(content):
        verb = m.group(1).upper()
        if verb == "ROUTE":
            verb = "GET"
        entities.append({"type": "ROUTE", "verb": verb, "name": m.group(2), "source": filename})

    # Routes — Django urls.py
    for m in PY_DJANGO_URL_RE.finditer(content):
        entities.append({
            "type": "ROUTE", "verb": "ANY",
            "name": "/" + m.group(1).lstrip("^/"),
            "handler": m.group(2), "source": filename,
        })

    # Django table hints from db_table = "..."
    for m in PY_DB_TABLE_RE.finditer(content):
        entities.append({"type": "TABLE_HINT", "name": m.group(1),
                         "source": filename, "via": "Django Meta.db_table"})

    # Build a quick map of class-line → indent to bucket methods by class
    class_matches = list(PY_CLASS_RE.finditer(content))
    # Cap pathological files to first ~30 classes to keep parsing cheap
    for idx, cls_m in enumerate(class_matches[:30]):
        cls_name = cls_m.group(1)
        bases = [b.strip() for b in (cls_m.group(2) or "").split(",") if b.strip()]
        cls_start = cls_m.end()
        cls_end = class_matches[idx + 1].start() if idx + 1 < len(class_matches) else len(content)
        body = content[cls_start:cls_end]

        methods: List[Dict[str, Any]] = []
        for mm in PY_DEF_RE.finditer(body):
            mname = mm.group(2)
            if mname.startswith("__") and mname.endswith("__") and mname != "__init__":
                continue
            window = body[mm.start():mm.start() + 1500]
            # iter-14.30 — Use multiple SQL regexes to properly extract table names
            tables: set = set()
            for tm in PY_SQL_FROM_RE.finditer(window):
                tables.add(tm.group(1))
            for tm in PY_SQL_INTO_RE.finditer(window):
                tables.add(tm.group(1))
            for tm in PY_SQL_UPDATE_RE.finditer(window):
                tables.add(tm.group(1))
            for tm in PY_SQL_JOIN_RE.finditer(window):
                tables.add(tm.group(1))
            methods.append({"name": mname, "params": mm.group(3).strip(),
                            "tables": sorted(tables), "sessions": []})

        is_django_model = any("Model" in b for b in bases) or bool(
            re.search(rf"class\s+{re.escape(cls_name)}\s*\(\s*(?:models\.)?Model\s*\)", content)
        )
        entities.append({
            "type": "CLASS", "name": cls_name, "namespace": namespace,
            "extends": bases[0] if bases else "",
            "implements": bases[1:],
            "is_django_model": is_django_model,
            "methods": methods, "source": filename,
        })

    return entities


# ---------- .NET (C# / VB.NET / ASP.NET) ----------
CS_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w\.]+)", re.MULTILINE)
CS_CLASS_RE = re.compile(
    r"(?:public\s+|internal\s+|abstract\s+|sealed\s+|partial\s+|static\s+)*"
    r"(?:class|interface|record)\s+([A-Za-z_]\w*)"
    r"(?:\s*<[^>]+>)?"
    r"(?:\s*:\s*([^\{]+))?\s*\{?",
    re.MULTILINE,
)
CS_METHOD_RE = re.compile(
    # Tolerate same-line attribute prefixes like `[HttpGet("")] public ...`.
    r"(?:^|\]|\;|\{|\})\s*(?:public|private|protected|internal)(?:\s+static)?(?:\s+async)?\s+"
    r"(?:[\w<>,\[\]\?\.]+(?:\s+[\w<>,\[\]\?\.]+)*?)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*\{",
    re.MULTILINE,
)
# ASP.NET MVC / Web API attribute routes
CS_ROUTE_ATTR_RE = re.compile(
    r"\[(Http(?:Get|Post|Put|Delete|Patch)|Route)\s*\(\s*[\"']([^\"']+)[\"']",
)
# EF Core / EF6 [Table("...")] hint
CS_TABLE_ATTR_RE = re.compile(r"\[Table\s*\(\s*[\"']([a-zA-Z_]\w*)[\"']")
# iter-14.30 — Fixed: handle schema.table notation
CS_SQL_FROM_RE = re.compile(r"\bFROM\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
CS_SQL_INTO_RE = re.compile(r"\bINTO\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
CS_SQL_UPDATE_RE = re.compile(r"\bUPDATE\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
CS_SQL_JOIN_RE = re.compile(r"\bJOIN\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
# Legacy regex kept for backward compat
CS_SQL_INLINE_RE = re.compile(
    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:[\w.`\"]+\s+)*?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)


def extract_dotnet(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    ns_m = CS_NAMESPACE_RE.search(content)
    namespace = ns_m.group(1) if ns_m else ""

    # Attribute routes
    for m in CS_ROUTE_ATTR_RE.finditer(content):
        attr = m.group(1)
        verb = attr.replace("Http", "").upper() if attr.startswith("Http") else "ANY"
        entities.append({"type": "ROUTE", "verb": verb, "name": m.group(2), "source": filename})

    # EF [Table("...")] hints
    for m in CS_TABLE_ATTR_RE.finditer(content):
        entities.append({"type": "TABLE_HINT", "name": m.group(1),
                         "source": filename, "via": "EF [Table]"})

    # Classes (cap pathological files)
    class_matches = list(CS_CLASS_RE.finditer(content))[:50]
    for idx, cls_m in enumerate(class_matches):
        cls_name = cls_m.group(1)
        if cls_name in {"if", "for", "while", "switch", "using", "var"}:
            continue
        bases = [b.strip() for b in (cls_m.group(2) or "").split(",") if b.strip()]
        cls_start = cls_m.end()
        # naive: 8000-char window or until next class — whichever is sooner
        next_start = class_matches[idx + 1].start() if idx + 1 < len(class_matches) else len(content)
        body = content[cls_start:min(next_start, cls_start + 8000)]

        methods: List[Dict[str, Any]] = []
        for mm in CS_METHOD_RE.finditer(body):
            mname = mm.group(1)
            if mname in {"if", "for", "while", "switch", "catch", "lock", "using"}:
                continue
            window = body[mm.start():mm.start() + 1500]
            # iter-14.30 — Use multiple SQL regexes to properly extract table names
            tables: set = set()
            for tm in CS_SQL_FROM_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in CS_SQL_INTO_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in CS_SQL_UPDATE_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in CS_SQL_JOIN_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            methods.append({"name": mname, "params": mm.group(2).strip(),
                            "tables": sorted(tables), "sessions": []})

        # Heuristic: ASP.NET Controller class name ending in `Controller`
        is_controller = cls_name.endswith("Controller")
        entities.append({
            "type": "CLASS", "name": cls_name, "namespace": namespace,
            "extends": bases[0] if bases else "",
            "implements": bases[1:],
            "is_controller": is_controller,
            "methods": methods, "source": filename,
        })

    return entities


# ---------- JavaScript / TypeScript (Express / NestJS / React) ----------
JS_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:[\w*\{\},\s]+)\s+from\s+|require\(\s*)[\"']([^\"']+)[\"']",
    re.MULTILINE,
)
JS_CLASS_RE = re.compile(
    r"(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_]\w*)"
    r"(?:\s*<[^>]+>)?"
    r"(?:\s+extends\s+([A-Za-z_][\w\.]*))?"
    r"(?:\s+implements\s+([\w\.<>,\s]+?))?\s*\{",
    re.MULTILINE,
)
JS_METHOD_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]\|\s,]+)?\s*\{",
    re.MULTILINE,
)
# Express / Fastify / Koa: app.get('/x', ...), router.post("/users", ...)
JS_EXPRESS_ROUTE_RE = re.compile(
    r"\b(?:app|router|api)\.(get|post|put|delete|patch|use|all)\(\s*[\"'`]([^\"'`]+)[\"'`]",
    re.IGNORECASE,
)
# NestJS decorators: @Get('/x')  @Post('users')
JS_NEST_ROUTE_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|All)\(\s*[\"'`]([^\"'`]*)[\"'`]\s*\)",
)
# Next.js App Router page/api file → contributes a route hint based on path
NEXT_API_PATH_RE = re.compile(r"/(?:pages|app)/api/(.+?)\.(?:[tj]sx?)$", re.IGNORECASE)
# iter-14.30 — Fixed: handle schema.table notation
JS_SQL_FROM_RE = re.compile(r"\bFROM\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
JS_SQL_INTO_RE = re.compile(r"\bINTO\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
JS_SQL_UPDATE_RE = re.compile(r"\bUPDATE\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
JS_SQL_JOIN_RE = re.compile(r"\bJOIN\s+(?:[\w`\"]+\.)?([a-zA-Z_]\w*)", re.IGNORECASE)
# Legacy regex kept for backward compat
JS_SQL_INLINE_RE = re.compile(
    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:[\w.`\"]+\s+)*?([a-zA-Z_]\w*)",
    re.IGNORECASE,
)


def extract_js(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []

    # Express / Fastify / Koa route registrations
    for m in JS_EXPRESS_ROUTE_RE.finditer(content):
        verb = m.group(1).upper()
        if verb in {"USE", "ALL"}:
            verb = "ANY"
        entities.append({"type": "ROUTE", "verb": verb, "name": m.group(2), "source": filename})

    # NestJS controller routes
    for m in JS_NEST_ROUTE_RE.finditer(content):
        verb = m.group(1).upper()
        if verb == "ALL":
            verb = "ANY"
        path = m.group(2) or "/"
        if not path.startswith("/"):
            path = "/" + path
        entities.append({"type": "ROUTE", "verb": verb, "name": path, "source": filename})

    # Next.js API route file → derive route name from path
    next_m = NEXT_API_PATH_RE.search((filename or "").replace("\\", "/"))
    if next_m:
        route_path = "/api/" + next_m.group(1).replace("/index", "")
        entities.append({"type": "ROUTE", "verb": "ANY", "name": route_path, "source": filename})

    # Classes
    class_matches = list(JS_CLASS_RE.finditer(content))[:40]
    for idx, cls_m in enumerate(class_matches):
        cls_name = cls_m.group(1)
        extends = (cls_m.group(2) or "").strip()
        implements = [s.strip() for s in (cls_m.group(3) or "").split(",") if s.strip()]
        cls_start = cls_m.end() - 1
        next_start = class_matches[idx + 1].start() if idx + 1 < len(class_matches) else len(content)
        body = content[cls_start:min(next_start, cls_start + 8000)]

        methods: List[Dict[str, Any]] = []
        for mm in JS_METHOD_RE.finditer(body):
            mname = mm.group(1)
            if mname in {"if", "for", "while", "switch", "catch", "return", "function",
                         "constructor"} and mname != "constructor":
                continue
            window = body[mm.start():mm.start() + 1500]
            # iter-14.30 — Use multiple SQL regexes to properly extract table names
            tables: set = set()
            for tm in JS_SQL_FROM_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JS_SQL_INTO_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JS_SQL_UPDATE_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            for tm in JS_SQL_JOIN_RE.finditer(window):
                tables.add(tm.group(1).strip("`\""))
            methods.append({"name": mname, "params": mm.group(2).strip(),
                            "tables": sorted(tables), "sessions": []})

        entities.append({
            "type": "CLASS", "name": cls_name, "namespace": "",
            "extends": extends, "implements": implements,
            "methods": methods, "source": filename,
        })

    return entities


# ---------- XML (Struts 1 / Spring / Hibernate / web.xml) ----------
# iter — Struts 1 was invisible to LAMA before this: `.xml` was classified
# as generic "config" and dropped by the extractor dispatch, so every
# `<action path="/foo" type="…Action"/>` mapping — the primary source of
# user-facing flows in Struts apps — never made it into the KB. That's
# what tanked the Workflow / Approval-flow / Actors sections in the
# ceots project (3 routes for 1,205 classes was the tell).
#
# This extractor is content-sniffing: any XML that carries Struts 1,
# Spring MVC / Spring bean, hibernate mapping, or web.xml patterns is
# lifted; everything else falls through with no emission (so we don't
# pollute the KB with generic project.xml / pom.xml / manifest.xml noise).
STRUTS_ACTION_RE = re.compile(
    r"<action\b[^>]*\bpath\s*=\s*[\"']([^\"']+)[\"']"
    r"(?:[^>]*\btype\s*=\s*[\"']([^\"']+)[\"'])?"
    r"(?:[^>]*\bname\s*=\s*[\"']([^\"']+)[\"'])?"
    r"(?:[^>]*\bscope\s*=\s*[\"']([^\"']+)[\"'])?"
    r"(?:[^>]*\bvalidate\s*=\s*[\"']([^\"']+)[\"'])?"
    r"(?:[^>]*\binput\s*=\s*[\"']([^\"']+)[\"'])?",
    re.IGNORECASE,
)
STRUTS_FORWARD_RE = re.compile(
    r"<forward\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"']"
    r"[^>]*\bpath\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
STRUTS_FORM_BEAN_RE = re.compile(
    r"<form-bean\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"']"
    r"[^>]*\btype\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
STRUTS_GLOBAL_FORWARD_RE = re.compile(
    r"<global-forwards\b[\s\S]*?</global-forwards>",
    re.IGNORECASE,
)
SPRING_MVC_MAPPING_RE = re.compile(
    r"<(?:mvc:|)\bview-controller\b[^>]*\bpath\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
SPRING_BEAN_RE = re.compile(
    r"<bean\b[^>]*\bid\s*=\s*[\"']([^\"']+)[\"']"
    r"(?:[^>]*\bclass\s*=\s*[\"']([^\"']+)[\"'])?",
    re.IGNORECASE,
)
WEBXML_SERVLET_MAPPING_RE = re.compile(
    r"<servlet-mapping\b[\s\S]*?<servlet-name>\s*([^<\s]+)\s*</servlet-name>"
    r"[\s\S]*?<url-pattern>\s*([^<\s]+)\s*</url-pattern>",
    re.IGNORECASE,
)
WEBXML_ROLE_RE = re.compile(
    r"<security-role\b[\s\S]*?<role-name>\s*([^<\s]+)\s*</role-name>",
    re.IGNORECASE,
)
# iter-14.23 — <security-constraint> maps url-patterns → allowed roles.
WEBXML_SECURITY_CONSTRAINT_RE = re.compile(
    r"<security-constraint\b([\s\S]*?)</security-constraint>",
    re.IGNORECASE,
)
_WEBXML_URL_PATTERN_RE = re.compile(
    r"<url-pattern>\s*([^<\s]+)\s*</url-pattern>", re.IGNORECASE,
)
_WEBXML_AUTH_ROLE_RE = re.compile(
    r"<auth-constraint\b[\s\S]*?</auth-constraint>", re.IGNORECASE,
)
_WEBXML_ROLE_NAME_RE = re.compile(
    r"<role-name>\s*([^<\s]+)\s*</role-name>", re.IGNORECASE,
)
HIBERNATE_CLASS_RE = re.compile(
    r"<class\b[^>]*\bname\s*=\s*[\"']([^\"']+)[\"']"
    r"(?:[^>]*\btable\s*=\s*[\"']([^\"']+)[\"'])?",
    re.IGNORECASE,
)


def extract_xml(content: str, filename: str = "") -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    if not content:
        return entities
    lname = filename.lower()
    lower = content.lower()

    # ---- Struts 1 -------------------------------------------------
    is_struts = (
        "struts-config" in lname
        or "<struts-config" in lower
        or ("<action-mappings" in lower and "<action" in lower)
        or ("<form-beans" in lower and "<form-bean" in lower)
    )
    if is_struts:
        # Actions → routes (verb=ANY since Struts 1 didn't distinguish
        # GET/POST at mapping level; forms typically POST, links typically GET).
        # iter-14.22 — enrich Struts ROUTE with handler_class / handler_method /
        # http_method / forwards so downstream arch.recommend has enough
        # metadata to enforce 1:1 API parity.
        for m in STRUTS_ACTION_RE.finditer(content):
            path, atype, form_name, scope, validate, inp = m.groups()
            # Extract per-action <forward> children if any (walk a short
            # window forward from the match — Struts config is line-oriented).
            fw_window = content[m.end(): m.end() + 4000]
            action_close = fw_window.find("</action>")
            fw_scope = fw_window[:action_close] if action_close > 0 else fw_window[:800]
            forwards = [{"name": fm.group(1), "path": fm.group(2)}
                        for fm in STRUTS_FORWARD_RE.finditer(fw_scope)]
            handler_cls = atype or ""
            entities.append({
                "type": "ROUTE",
                "verb": "ANY",
                "http_method": "GET+POST",
                "name": path,
                "handler": handler_cls,
                "handler_class": handler_cls,
                "handler_method": "execute",
                "form_bean": form_name or "",
                "form_name": form_name or "",
                "scope": scope or "",
                "validate": (validate or "").lower() == "true",
                "input": inp or "",
                "forwards": forwards,
                "framework": "struts1",
                "source": filename,
            })
            if atype:
                # Emit an ACTION entity so the class-count / handler graph
                # is aware even if the .java file wasn't scanned yet.
                entities.append({
                    "type": "ACTION",
                    "name": atype.rsplit(".", 1)[-1],
                    "namespace": atype.rsplit(".", 1)[0] if "." in atype else "",
                    "framework": "struts1",
                    "route_path": path,
                    "source": filename,
                })
        for m in STRUTS_FORM_BEAN_RE.finditer(content):
            fname, ftype = m.groups()
            entities.append({
                "type": "FORM_BEAN",
                "name": fname,
                "handler": ftype,
                "framework": "struts1",
                "source": filename,
            })
        for gf in STRUTS_GLOBAL_FORWARD_RE.finditer(content):
            for fm in STRUTS_FORWARD_RE.finditer(gf.group(0)):
                entities.append({
                    "type": "FORWARD",
                    "name": fm.group(1),
                    "target": fm.group(2),
                    "framework": "struts1",
                    "source": filename,
                })

    # ---- Spring MVC / DI ------------------------------------------
    if "<beans" in lower or "spring" in lname:
        for m in SPRING_BEAN_RE.finditer(content):
            bid, bclass = m.groups()
            entities.append({
                "type": "BEAN",
                "name": bid,
                "handler": bclass or "",
                "framework": "spring",
                "source": filename,
            })
        for m in SPRING_MVC_MAPPING_RE.finditer(content):
            entities.append({
                "type": "ROUTE",
                "verb": "GET",
                "name": m.group(1),
                "framework": "spring-mvc",
                "source": filename,
            })

    # ---- web.xml ---------------------------------------------------
    if ("web.xml" in lname or "<web-app" in lower
            or "<security-constraint" in lower or "<security-role" in lower):
        # iter-14.22 — build servlet-name → servlet-class map first so we
        # can attach handler_class to every emitted ROUTE.
        servlet_class_map: Dict[str, str] = {}
        _servlet_def_re = re.compile(
            r"<servlet\b[\s\S]*?<servlet-name>\s*([^<\s]+)\s*</servlet-name>"
            r"[\s\S]*?<servlet-class>\s*([^<\s]+)\s*</servlet-class>",
            re.IGNORECASE,
        )
        for sm in _servlet_def_re.finditer(content):
            servlet_class_map[sm.group(1)] = sm.group(2)
        for m in WEBXML_SERVLET_MAPPING_RE.finditer(content):
            servlet, pattern = m.groups()
            handler_cls = servlet_class_map.get(servlet, "")
            entities.append({
                "type": "ROUTE",
                "verb": "ANY",
                "http_method": "GET+POST",
                "name": pattern,
                "handler": servlet,
                "handler_class": handler_cls,
                "handler_method": "service",
                "servlet_name": servlet,
                "framework": "servlet",
                "source": filename,
            })
        for m in WEBXML_ROLE_RE.finditer(content):
            entities.append({
                "type": "ROLE",
                "name": m.group(1),
                "framework": "servlet",
                "source": filename,
            })
        # iter-14.23 — <security-constraint> → (url-patterns, roles). These
        # are cross-referenced against ROUTE paths by enrich_routes() so
        # every protected route carries its allowed role-name union.
        for sc in WEBXML_SECURITY_CONSTRAINT_RE.finditer(content):
            block = sc.group(1)
            url_patterns = [p for p in _WEBXML_URL_PATTERN_RE.findall(block)]
            roles: List[str] = []
            auth_m = _WEBXML_AUTH_ROLE_RE.search(block)
            if auth_m:
                roles = [r for r in _WEBXML_ROLE_NAME_RE.findall(auth_m.group(0))]
            if url_patterns:
                entities.append({
                    "type": "SECURITY_CONSTRAINT",
                    "name": (url_patterns[0] if url_patterns else ""),
                    "url_patterns": url_patterns,
                    "roles": roles,
                    "framework": "servlet",
                    "source": filename,
                })

    # ---- Hibernate .hbm.xml ---------------------------------------
    if lname.endswith(".hbm.xml") or "<hibernate-mapping" in lower:
        for m in HIBERNATE_CLASS_RE.finditer(content):
            cname, table = m.groups()
            entities.append({
                "type": "CLASS",
                "name": cname.rsplit(".", 1)[-1],
                "namespace": cname.rsplit(".", 1)[0] if "." in cname else "",
                "is_jpa_entity": True,
                "framework": "hibernate",
                "methods": [],
                "extends": "", "implements": [],
                "source": filename,
            })
            if table:
                entities.append({
                    "type": "TABLE_HINT",
                    "name": table,
                    "via": "hibernate mapping",
                    "source": filename,
                })

    return entities


def extract_zip(content: str, filename: str = "") -> List[Dict[str, Any]]:
    """ZIP content is concatenated as `===== FILE: path =====` blocks.
    Split it and route each block to the right extractor based on the inner filename's extension."""
    entities: List[Dict[str, Any]] = []
    # split on the marker; each piece is "path =====\n<body>"
    parts = content.split("===== FILE:")
    for part in parts:
        if not part.strip():
            continue
        # part starts with " <path> =====\n<body>"
        try:
            header, body = part.split("=====", 1)
        except ValueError:
            continue
        inner_name = header.strip()
        lname = inner_name.lower()
        src = f"{filename}::{inner_name}" if filename else inner_name
        if lname.endswith(".php"):
            entities.extend(extract_php(body, src))
        elif lname.endswith(".sql") or lname.endswith((".plsql", ".pls", ".pkb", ".pks")):
            entities.extend(extract_sql(body, src))
        elif lname.endswith(".java"):
            entities.extend(extract_java(body, src))
        elif lname.endswith((".jsp", ".jspx", ".jspf", ".tag", ".xhtml")):
            entities.extend(extract_jsp(body, src))
        elif lname.endswith(".py"):
            entities.extend(extract_python(body, src))
        elif lname.endswith((".cs", ".vb", ".aspx", ".cshtml", ".vbhtml")):
            entities.extend(extract_dotnet(body, src))
        elif lname.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
            entities.extend(extract_js(body, src))
        elif lname.endswith(".xml"):
            entities.extend(extract_xml(body, src))
    return entities


def extract(filetype: str, content: str, filename: str = "", global_constants: Dict[str, str] = None) -> List[Dict[str, Any]]:
    """Dispatch by parser-assigned filetype. Language-agnostic across PHP / SQL /
    Java / JSP / Python / .NET / JS-TS / ZIP. Returns [] for filetypes that
    carry no extractable ontology (txt, pdf, docx, csv, yaml, json, config, web)
    so the caller can safely call this on every file.

    `global_constants` (iter-15.20) is an optional CONST_NAME -> value map,
    built once across all uploaded Java files, used to resolve symbolic
    `@Path(SOME_CONSTANT)` / `@RequestMapping(SOME_CONSTANT)` class-level
    prefixes — only `extract_java` currently consumes it.
    """
    import logging
    _logger = logging.getLogger(__name__)
    
    result: List[Dict[str, Any]] = []
    if filetype == "php":
        result = extract_php(content, filename)
    elif filetype == "sql":
        result = extract_sql(content, filename)
    elif filetype == "java":
        result = extract_java(content, filename, global_constants=global_constants)
    elif filetype == "jsp":
        result = extract_jsp(content, filename)
    elif filetype == "python":
        result = extract_python(content, filename)
    elif filetype == "dotnet":
        result = extract_dotnet(content, filename)
    elif filetype == "js":
        result = extract_js(content, filename)
    elif filetype == "xml":
        result = extract_xml(content, filename)
    elif filetype == "zip":
        result = extract_zip(content, filename)
    
    # iter-14.30 — Detailed extraction logging for debugging route issues
    route_count = sum(1 for e in result if e.get("type") == "ROUTE")
    if route_count > 0:
        route_names = [e.get("name", "?") for e in result if e.get("type") == "ROUTE"][:5]
        _logger.info(
            "owl_extractor: file=%s type=%s → %d routes: %s",
            filename[:60], filetype, route_count, route_names,
        )
    return result


# ── iter-14.23 — Route field-evidence enrichment ─────────────────────────────
# Post-extraction pass that joins the fragmentary evidence pieces the
# per-file extractors emit into fully-populated ROUTE entities. This is the
# join that was missing in iter-14.22 (170 ROUTEs but no field-level detail),
# which caused CodeGen to emit `⚠ EVIDENCE GAP` markers per the anti-
# hallucination contract → empty DTO records → confidence collapse.
#
# Joins performed:
#   • Struts ROUTE.form_bean  →  FORM_BEAN.name  →  ActionForm FQN
#                             →  FORM_FIELD[owner_class]  →  request_fields
#   • Spring ROUTE.request_body_class  →  CLASS.fields   →  request_fields
#   •        ROUTE.response_body_class →  CLASS.fields   →  response_fields
#   • Struts ROUTE.forwards   →  target_view (success forward)
#                             →  JSP_MODEL.bindings      →  response_fields
#   • web.xml SECURITY_CONSTRAINT.url_patterns ∩ ROUTE.name → roles
# The function mutates and returns the same list so callers can persist it.
_STRUTS_VIEW_EXTS = (".do", ".action", ".htm", ".html", ".jsp")


def _strip_action_ext(path: str) -> str:
    p = (path or "").split("?", 1)[0]
    low = p.lower()
    for ext in _STRUTS_VIEW_EXTS:
        if low.endswith(ext):
            return p[: -len(ext)]
    return p


def _route_matches_pattern(route_path: str, pattern: str) -> bool:
    if not route_path or not pattern:
        return False
    rp = route_path.split("?", 1)[0]
    pat = pattern.strip()
    # Extension pattern e.g. *.do
    if pat.startswith("*."):
        return rp.lower().endswith(pat[1:].lower())
    # Prefix wildcard e.g. /admin/*
    if pat.endswith("/*"):
        prefix = pat[:-2]
        return rp == prefix or rp.startswith(prefix + "/") or _strip_action_ext(rp).startswith(prefix)
    # Exact (tolerant of the .do/.action extension on either side)
    return _strip_action_ext(rp) == _strip_action_ext(pat) or rp == pat


def _fields_as_dtolist(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for f in fields or []:
        out.append({
            "name": f.get("name", ""),
            "java_type": f.get("java_type", "String") or "String",
            "required": bool(f.get("required")),
        })
    return [f for f in out if f["name"]]


def enrich_routes(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not entities:
        return entities

    form_bean_map: Dict[str, str] = {}            # form-bean name → ActionForm FQN
    fields_by_owner: Dict[str, List[Dict[str, Any]]] = {}
    class_fields: Dict[str, List[Dict[str, Any]]] = {}
    security_constraints: List[Dict[str, Any]] = []
    jsp_models: Dict[str, List[str]] = {}         # basename(no ext) → bindings

    for e in entities:
        t = e.get("type")
        if t == "FORM_BEAN":
            nm = e.get("name") or ""
            ftype = e.get("handler") or ""
            if nm and ftype:
                form_bean_map[nm] = ftype
        elif t == "FORM_FIELD":
            owner = e.get("owner_class") or ""
            simple = e.get("owner_simple") or (owner.rsplit(".", 1)[-1] if owner else "")
            fld = {"name": e.get("name", ""), "java_type": e.get("java_type", "String"),
                   "required": bool(e.get("required"))}
            for key in {owner, simple}:
                if key:
                    fields_by_owner.setdefault(key, []).append(fld)
        elif t == "CLASS":
            flds = e.get("fields") or []
            if flds:
                simple = e.get("name") or ""
                fq = (f"{e.get('namespace')}.{simple}" if e.get("namespace") else simple)
                for key in {simple, fq}:
                    if key:
                        class_fields[key] = flds
        elif t == "SECURITY_CONSTRAINT":
            security_constraints.append(e)
        elif t == "JSP_MODEL":
            src = (e.get("source") or e.get("name") or "")
            base = _strip_action_ext(src.rsplit("/", 1)[-1])
            if base and e.get("bindings"):
                jsp_models[base] = e["bindings"]

    def _lookup_fields(class_ref: str) -> List[Dict[str, Any]]:
        if not class_ref:
            return []
        simple = class_ref.rsplit(".", 1)[-1]
        for key in (class_ref, simple):
            if key in fields_by_owner:
                return fields_by_owner[key]
            if key in class_fields:
                return class_fields[key]
        return []

    def _roles_for(route_path: str) -> List[str]:
        roles: List[str] = []
        for sc in security_constraints:
            for pat in (sc.get("url_patterns") or []):
                if _route_matches_pattern(route_path, pat):
                    for r in (sc.get("roles") or []):
                        if r and r not in roles:
                            roles.append(r)
                    break
        return roles

    for e in entities:
        if e.get("type") != "ROUTE":
            continue
        framework = (e.get("framework") or "").lower()
        route_path = e.get("name") or ""

        # ---- request_fields ------------------------------------------
        req_fields: List[Dict[str, Any]] = []
        if framework == "struts1":
            form_name = e.get("form_bean") or e.get("form_name") or ""
            fq = form_bean_map.get(form_name, "")
            if fq:
                e["form_bean_class"] = fq
                req_fields = _fields_as_dtolist(_lookup_fields(fq))
            elif form_name:
                # No form-bean row parsed — try resolving by class name.
                req_fields = _fields_as_dtolist(_lookup_fields(form_name))
        else:
            rb = e.get("request_body_class") or ""
            if rb:
                req_fields = _fields_as_dtolist(_lookup_fields(rb))
        if req_fields:
            e["request_fields"] = req_fields

        # ---- target_view (Struts forward) ----------------------------
        target_view = ""
        forwards = e.get("forwards") or []
        if forwards:
            success = next((f for f in forwards
                            if (f.get("name") or "").lower() in ("success", "ok", "view", "display")), None)
            target_view = (success or forwards[0]).get("path") or ""
            if target_view:
                e["target_view"] = target_view

        # ---- response_fields -----------------------------------------
        resp_fields: List[Dict[str, Any]] = []
        rb_resp = e.get("response_body_class") or ""
        if rb_resp:
            resp_fields = _fields_as_dtolist(_lookup_fields(rb_resp))
        if not resp_fields and target_view:
            base = _strip_action_ext(target_view.rsplit("/", 1)[-1])
            binds = jsp_models.get(base)
            if binds:
                resp_fields = [{"name": b, "java_type": "String", "required": False} for b in binds]
        if resp_fields:
            e["response_fields"] = resp_fields

        # ---- roles (web.xml security-constraint union) ---------------
        existing_roles = list(e.get("roles") or [])
        matched_roles = _roles_for(route_path)
        union = existing_roles + [r for r in matched_roles if r not in existing_roles]
        if union:
            e["roles"] = union

    return entities


def aggregate_stats(entities: List[Dict[str, Any]]) -> Dict[str, int]:
    stats = {
        "entities": len(entities),
        "classes": 0,
        "methods": 0,
        "tables": 0,
        "columns": 0,
        "roles": 0,
        "relationships": 0,
        "routes": 0,
    }
    # Use sets so a table referenced from SQL DDL *and* hinted from PHP/JSP/Java
    # is only counted once, and FK relationships are de-duplicated across files.
    table_names: set = set()
    relation_keys: set = set()
    for e in entities:
        t = e.get("type")
        if t == "CLASS":
            stats["classes"] += 1
            stats["methods"] += len(e.get("methods", []))
            # Tables hinted via method-body SQL parsing
            for m in (e.get("methods") or []):
                for tbl in (m.get("tables") or []):
                    if tbl:
                        table_names.add(tbl)
        elif t == "TABLE":
            if e.get("name"):
                table_names.add(e["name"])
            stats["columns"] += len(e.get("columns", []))
            for fk in (e.get("fks") or []):
                ref = fk.get("ref_table") or ""
                col = fk.get("column") or ""
                if ref:
                    table_names.add(ref)
                relation_keys.add((e.get("name", ""), ref, col))
            # FK columns also referenced via column.references
            for c in (e.get("columns") or []):
                if c.get("references"):
                    table_names.add(c["references"])
                    relation_keys.add((e.get("name", ""), c["references"], c.get("name", "")))
        elif t == "TABLE_HINT":
            if e.get("name"):
                table_names.add(e["name"])
        elif t == "JSP_TABLE_REFS":
            for tbl in (e.get("tables") or []):
                if tbl:
                    table_names.add(tbl)
        elif t == "RELATIONSHIP":
            a, b = e.get("from", ""), e.get("to", "")
            if a:
                table_names.add(a)
            if b:
                table_names.add(b)
            relation_keys.add((a, b, e.get("kind", "")))
        elif t == "INDIVIDUAL":
            if "role" in (e.get("category") or "").lower():
                stats["roles"] += 1
        elif t == "ROUTE":
            stats["routes"] += 1
        elif t == "ROLE":
            # iter — struts/web.xml <security-role> now surface as first-class
            # ROLE entities so the Approval/Actors sections have something
            # concrete to cite. Prior behaviour only counted INDIVIDUAL rows
            # with category="role", which no extractor emitted.
            stats["roles"] += 1
    stats["tables"] = len(table_names)
    stats["relationships"] = len(relation_keys)
    stats["modules"] = len([e for e in entities if e.get("type") == "MODULE"])
    stats["component_maps"] = len([e for e in entities if e.get("type") == "COMPONENT_MAP"])
    return stats

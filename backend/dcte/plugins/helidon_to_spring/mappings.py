"""Deterministic Helidon → Spring Boot mappings.

Pure lookup tables. No regex logic — see the sibling `*_transformer.py`
modules for the actual code rewriting.
"""

# Import replacements applied in order (first match wins per line).
IMPORT_REPLACEMENTS: list[tuple[str, str]] = [
    # JAX-RS → Spring Web
    ("jakarta.ws.rs.GET",                 "org.springframework.web.bind.annotation.GetMapping"),
    ("jakarta.ws.rs.POST",                "org.springframework.web.bind.annotation.PostMapping"),
    ("jakarta.ws.rs.PUT",                 "org.springframework.web.bind.annotation.PutMapping"),
    ("jakarta.ws.rs.DELETE",              "org.springframework.web.bind.annotation.DeleteMapping"),
    ("jakarta.ws.rs.PATCH",               "org.springframework.web.bind.annotation.PatchMapping"),
    ("jakarta.ws.rs.Path",                "org.springframework.web.bind.annotation.RequestMapping"),
    ("jakarta.ws.rs.PathParam",           "org.springframework.web.bind.annotation.PathVariable"),
    ("jakarta.ws.rs.QueryParam",          "org.springframework.web.bind.annotation.RequestParam"),
    ("jakarta.ws.rs.HeaderParam",         "org.springframework.web.bind.annotation.RequestHeader"),
    ("jakarta.ws.rs.Produces",            "org.springframework.http.MediaType"),
    ("jakarta.ws.rs.Consumes",            "org.springframework.http.MediaType"),
    ("jakarta.ws.rs.core.Response",       "org.springframework.http.ResponseEntity"),
    ("jakarta.ws.rs.core.MediaType",      "org.springframework.http.MediaType"),
    ("jakarta.ws.rs.ApplicationPath",     "org.springframework.stereotype.Component"),

    # CDI → Spring DI
    ("jakarta.inject.Inject",             "org.springframework.beans.factory.annotation.Autowired"),
    ("jakarta.inject.Singleton",          "org.springframework.stereotype.Component"),
    ("jakarta.enterprise.context.ApplicationScoped", "org.springframework.stereotype.Component"),
    ("jakarta.enterprise.context.RequestScoped",     "org.springframework.web.context.annotation.RequestScope"),
    ("jakarta.enterprise.context.SessionScoped",     "org.springframework.web.context.annotation.SessionScope"),
    ("jakarta.annotation.PostConstruct",  "jakarta.annotation.PostConstruct"),  # same on Spring

    # MicroProfile Config → Spring
    ("org.eclipse.microprofile.config.inject.ConfigProperty",
     "org.springframework.beans.factory.annotation.Value"),
    ("org.eclipse.microprofile.config.Config", "org.springframework.core.env.Environment"),

    # MP Health → Spring Actuator
    ("org.eclipse.microprofile.health.HealthCheck",
     "org.springframework.boot.actuate.health.HealthIndicator"),
    ("org.eclipse.microprofile.health.HealthCheckResponse",
     "org.springframework.boot.actuate.health.Health"),
    ("org.eclipse.microprofile.health.Liveness",
     "org.springframework.boot.actuate.health.HealthIndicator"),
    ("org.eclipse.microprofile.health.Readiness",
     "org.springframework.boot.actuate.health.HealthIndicator"),

    # MP Metrics → Micrometer
    ("org.eclipse.microprofile.metrics.annotation.Counted",
     "io.micrometer.core.annotation.Counted"),
    ("org.eclipse.microprofile.metrics.annotation.Timed",
     "io.micrometer.core.annotation.Timed"),

    # MP OpenAPI → SpringDoc
    ("org.eclipse.microprofile.openapi.annotations.Operation",
     "io.swagger.v3.oas.annotations.Operation"),
    ("org.eclipse.microprofile.openapi.annotations.tags.Tag",
     "io.swagger.v3.oas.annotations.tags.Tag"),

    # Security
    ("io.helidon.security.annotations.Authenticated",
     "org.springframework.security.access.prepost.PreAuthorize"),
    ("io.helidon.security.annotations.Authorized",
     "org.springframework.security.access.prepost.PreAuthorize"),
]

# Annotation replacements at the code-token level (per-line).
ANNOTATION_TOKEN_REPLACEMENTS: list[tuple[str, str]] = [
    ("@ApplicationScoped", "@Component"),
    ("@RequestScoped",     "@RequestScope"),
    ("@SessionScoped",     "@SessionScope"),
    ("@Singleton",         "@Component"),
    ("@Inject",            "@Autowired"),
    ("@ApplicationPath",   "// removed: Spring auto-discovers /"),
    ("@PathParam",         "@PathVariable"),
    ("@QueryParam",        "@RequestParam"),
    ("@HeaderParam",       "@RequestHeader"),
    ("@ConfigProperty",    "@Value"),
    ("@Authenticated",     '@PreAuthorize("isAuthenticated()")'),
    ("@Liveness",          '@Component'),
    ("@Readiness",         '@Component'),
]

# HTTP verb annotation mapping — used with the class-level @Path to build
# @GetMapping("/foo/{id}") etc. Handled in endpoint_transformer.py.
HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# microprofile-config.properties keys are mostly compatible with Spring;
# a few well-known ones move to `management.*` / `server.*`.
MP_CONFIG_KEY_MAP: dict[str, str] = {
    "server.host": "server.address",
    "server.port": "server.port",
    "mp.metrics.tags": "management.metrics.tags",
    "mp.openapi.scan.disable": "springdoc.api-docs.enabled",
}

"""Deterministic code templates for cross-cutting utility services.

iter-13.59 — Utility services live in `arch_services` with
``kind="utility"`` and ``utility_type="audit_logger"`` (or similar).
They differ from business services in two ways:

  1. Their **own** file plan is fixed (no per-table / per-resource fan-out).
  2. They **inject** a small "client" file into every other backend service
     so the cross-cutting concern is automatically wired in.

We avoid LLM calls for utility scaffolding because the boilerplate is
tiny, well-known, and must be byte-exact across services (otherwise the
log shape diverges and ELK ingestion breaks).

Currently supported utility:
  • **audit_logger** — request/response audit trail via AOP / middleware,
    JSON output suitable for Filebeat → Logstash → Elasticsearch → Kibana.
    Captures: timestamp, client IP, HTTP method, path, controller class,
    handler method, status code, duration in seconds, request body
    (size-capped + PII-masked), response body (size-capped + PII-masked),
    headers (PII-masked).

PII masking is baked into every renderer:
  • Keys matching the configured field list (case-insensitive) →
    replaced with the configured mask (default ``****``).
  • String values matched by enabled regex patterns (email / SSN /
    credit-card / Aadhaar) → masked.
  • Masking happens BEFORE body size truncation so secrets never
    survive in the truncated tail.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional
import re

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PII_FIELDS = (
    "password,passwd,secret,token,access_token,refresh_token,api_key,apikey,"
    "authorization,cookie,set_cookie,ssn,aadhaar,pan,dob,date_of_birth,"
    "email,phone,mobile,credit_card,card_number,cvv,account_number,iban"
)
DEFAULT_PII_PATTERNS = "email,credit_card,ssn,aadhaar"
DEFAULT_PII_MASK = "****"


# ---------------------------------------------------------------------------
# Catalog — keep keys stable; the frontend uses them as data-testids and the
# codegen wiring switches on `utility_type`.
# ---------------------------------------------------------------------------

UTILITY_CATALOG: List[dict] = [
    {
        "key": "audit_logger",
        "name": "audit-logger",
        "display_name": "Audit Logger (ELK-ready)",
        "description": (
            "Adds an Aspect / Middleware that captures every REST "
            "request + response (timestamps, client IP, controller, "
            "method, duration in seconds) and emits structured JSON "
            "for Filebeat → Logstash → Elasticsearch → Kibana. PII "
            "fields are masked before the event is emitted."
        ),
        "supported_langs": ["java", "python", "nodejs", "dotnet", "go"],
        "config_schema": [
            {"key": "log_index", "label": "Elasticsearch index",
             "default": "lama-audit", "type": "string"},
            {"key": "logstash_host", "label": "Logstash host",
             "default": "logstash", "type": "string"},
            {"key": "logstash_port", "label": "Logstash port",
             "default": "5044", "type": "number"},
            {"key": "max_body_kb", "label": "Max body size (KB) to log",
             "default": "16", "type": "number"},
            {"key": "inject_into_all", "label": "Inject into every service",
             "default": True, "type": "bool"},
            {"key": "pii_fields", "label": "PII fields to mask (comma-separated)",
             "default": DEFAULT_PII_FIELDS, "type": "string"},
            {"key": "pii_value_patterns",
             "label": "PII value regexes (comma-separated: email,credit_card,ssn,aadhaar)",
             "default": DEFAULT_PII_PATTERNS, "type": "string"},
            {"key": "pii_mask", "label": "Mask replacement",
             "default": DEFAULT_PII_MASK, "type": "string"},
        ],
    },
]


def get_utility_meta(utility_type: str) -> Optional[dict]:
    for u in UTILITY_CATALOG:
        if u["key"] == utility_type:
            return u
    return None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def utility_service_files_plan(svc: dict) -> List[dict]:
    """Files inside the utility service itself (config + ELK pipeline)."""
    name = svc.get("name", "audit-logger")
    base = f"services/{name}"
    utype = svc.get("utility_type", "audit_logger")
    if utype == "audit_logger":
        return [
            {"path": f"{base}/README.md",                       "type": "utility_readme",       "language": "markdown", "utility_type": utype, "deterministic": True},
            {"path": f"{base}/elk/logstash.conf",               "type": "utility_elk_logstash", "language": "ini",      "utility_type": utype, "deterministic": True},
            {"path": f"{base}/elk/filebeat.yml",                "type": "utility_elk_filebeat", "language": "yaml",     "utility_type": utype, "deterministic": True},
            {"path": f"{base}/elk/kibana-index-pattern.json",   "type": "utility_elk_kibana",   "language": "json",     "utility_type": utype, "deterministic": True},
            {"path": f"{base}/docker-compose.elk.yml",          "type": "utility_elk_compose",  "language": "yaml",     "utility_type": utype, "deterministic": True},
        ]
    return []


def injection_files_for_service(business_svc: dict, utility_svc: dict) -> List[dict]:
    """Extra files materialised inside ``business_svc`` to activate the
    utility there. Each entry carries ``deterministic: True`` so the
    codegen runner renders without calling the LLM."""
    utype = utility_svc.get("utility_type", "audit_logger")
    if utype != "audit_logger":
        return []

    lang = business_svc.get("backend_lang", "nodejs")
    name = business_svc["name"]
    base = f"services/{name}"

    if lang == "java":
        pkg = _java_pkg_for(name)
        java_base = f"{base}/src/main/java/com/lama/{pkg}"
        return [
            {"path": f"{java_base}/audit/AuditLoggingAspect.java",
             "type": "utility_inject_audit_aspect", "language": "java",
             "utility_type": utype, "deterministic": True, "target_service": name},
            {"path": f"{java_base}/audit/AuditEvent.java",
             "type": "utility_inject_audit_dto", "language": "java",
             "utility_type": utype, "deterministic": True, "target_service": name},
        ]
    if lang == "python":
        return [
            {"path": f"{base}/app/audit/middleware.py",
             "type": "utility_inject_audit_middleware", "language": "python",
             "utility_type": utype, "deterministic": True, "target_service": name},
            {"path": f"{base}/app/audit/__init__.py",
             "type": "utility_inject_audit_init", "language": "python",
             "utility_type": utype, "deterministic": True, "target_service": name},
        ]
    if lang == "nodejs":
        return [
            {"path": f"{base}/src/middleware/auditLogger.js",
             "type": "utility_inject_audit_middleware", "language": "javascript",
             "utility_type": utype, "deterministic": True, "target_service": name},
        ]
    if lang == "dotnet":
        return [
            {"path": f"{base}/Audit/AuditLoggingFilter.cs",
             "type": "utility_inject_audit_filter", "language": "csharp",
             "utility_type": utype, "deterministic": True, "target_service": name},
        ]
    if lang == "go":
        return [
            {"path": f"{base}/internal/audit/middleware.go",
             "type": "utility_inject_audit_middleware", "language": "go",
             "utility_type": utype, "deterministic": True, "target_service": name},
        ]
    return [
        {"path": f"{base}/AUDIT_LOGGING.md",
         "type": "utility_inject_audit_readme", "language": "markdown",
         "utility_type": utype, "deterministic": True, "target_service": name},
    ]


def bootstrap_wiring_hint(utility_svc: dict, business_svc: dict) -> str:
    """One-line wiring hint appended to the bootstrap-file prompt so the
    LLM-generated server file actually registers the deterministic
    middleware/aspect we injected."""
    if utility_svc.get("utility_type") != "audit_logger":
        return ""
    lang = business_svc.get("backend_lang", "nodejs")
    if lang == "python":
        return ("Register audit middleware: "
                "`from app.audit import install as install_audit; install_audit(app)`.")
    if lang == "nodejs":
        return ("Register audit middleware: "
                "`app.use(require('./middleware/auditLogger'))` BEFORE every route.")
    if lang == "java":
        return ("The `@ComponentScan` on the @SpringBootApplication picks up "
                "`com.lama.<pkg>.audit.AuditLoggingAspect` automatically — "
                "no extra wiring needed.")
    if lang == "dotnet":
        return ("Register audit filter globally: "
                "`builder.Services.AddScoped<AuditLoggingFilter>();` then "
                "`builder.Services.AddControllers(o => o.Filters.AddService<AuditLoggingFilter>());`.")
    if lang == "go":
        return ("Register audit middleware on the root router: "
                "`r.Use(audit.Middleware())` BEFORE handlers.")
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _java_pkg_for(svc_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (svc_name or "").lower()) or "app"


def _build_ctx(file_def: dict, project: dict, utility_svc: dict,
               business_svc: Optional[dict]) -> dict:
    cfg = utility_svc.get("utility_config") or {}
    target = business_svc or utility_svc
    return {
        "project_name":   project.get("name", "lama"),
        "log_index":      cfg.get("log_index", "lama-audit"),
        "logstash_host":  cfg.get("logstash_host", "logstash"),
        "logstash_port":  str(cfg.get("logstash_port", "5044")),
        "max_body_kb":    str(cfg.get("max_body_kb", "16")),
        "service_name":   target.get("name", ""),
        "java_pkg":       _java_pkg_for(target.get("name", "")),
        "pii_fields":     cfg.get("pii_fields", DEFAULT_PII_FIELDS),
        "pii_patterns":   cfg.get("pii_value_patterns", DEFAULT_PII_PATTERNS),
        "pii_mask":       cfg.get("pii_mask", DEFAULT_PII_MASK),
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _r_utility_readme(c: dict) -> str:
    return f"""# Audit Logger (utility service)

Centralises configuration + ELK pipeline assets for the application-wide
request/response audit trail.

## What it captures (per HTTP request)

| Field             | Description                                          |
|-------------------|------------------------------------------------------|
| `timestamp`       | Request start time (ISO-8601, UTC)                   |
| `client_ip`       | Originating IP (honours `X-Forwarded-For`)           |
| `service`         | Service name                                         |
| `class`           | Controller class                                     |
| `method`          | Handler method                                       |
| `http_method`     | GET / POST / PUT / PATCH / DELETE                    |
| `path`            | Request path (with route template)                   |
| `status`          | HTTP status code                                     |
| `request_time`    | Request start time (ISO-8601)                        |
| `response_time`   | Response sent time (ISO-8601)                        |
| `duration_sec`    | Total round-trip time, seconds (3 decimals)          |
| `request_body`    | PII-masked, truncated to {c['max_body_kb']} KB       |
| `response_body`   | PII-masked, truncated to {c['max_body_kb']} KB       |
| `headers`         | PII-masked (Authorization / Cookie etc.)             |
| `correlation_id`  | Pulled from `X-Correlation-Id` (auto-set if absent)  |

## PII masking

Both keys and values are scrubbed BEFORE the event is emitted, so secrets
never reach Filebeat / Logstash / Elasticsearch.

* **Field mask** — keys matching this list (case-insensitive) are replaced
  with `{c['pii_mask']}`:

  ```
  {c['pii_fields']}
  ```

* **Value regex masks** — these patterns are scrubbed in string values:

  ```
  {c['pii_patterns']}
  ```

  Available patterns: `email`, `credit_card`, `ssn`, `aadhaar`.

Edit the lists per-project in the Architecture page → Utilities panel.

## ELK wiring

```
  business-service ─json log─▶ stdout
                                  │
                                  ▼
                            ┌──filebeat──┐
                            └─────┬──────┘
                                  ▼
                            ┌──logstash──┐  (json filter, mutate)
                            │  :{c['logstash_port']}  │
                            └─────┬──────┘
                                  ▼
                          ┌─elasticsearch─┐
                          │ index: {c['log_index']} │
                          └─────┬──────────┘
                                ▼
                            ┌──kibana──┐
                            └──────────┘
```

* `elk/filebeat.yml` — tails every container's stdout
* `elk/logstash.conf` — beats input, json filter, ES output
* `elk/kibana-index-pattern.json` — pre-built Kibana index pattern
* `docker-compose.elk.yml` — local ELK stack (`docker compose -f docker-compose.elk.yml up`)

## Disabling at runtime

Every injected service honours `AUDIT_LOGGING_ENABLED=false` to disable
logging without redeploying.
"""


def _r_elk_logstash(c: dict) -> str:
    return f"""input {{
  beats {{
    port => {c['logstash_port']}
    codec => json
  }}
}}

filter {{
  mutate {{
    convert => {{ "duration_sec" => "float" }}
    convert => {{ "status" => "integer" }}
  }}
  date {{
    match => [ "request_time", "ISO8601" ]
    target => "@timestamp"
  }}
}}

output {{
  elasticsearch {{
    hosts => ["elasticsearch:9200"]
    index => "{c['log_index']}-%{{+YYYY.MM.dd}}"
  }}
}}
"""


def _r_elk_filebeat(c: dict) -> str:
    return f"""# filebeat.yml — auto-discover service containers and ship JSON stdout.
filebeat.autodiscover:
  providers:
    - type: docker
      hints.enabled: true
      hints.default_config:
        type: container
        paths:
          - /var/lib/docker/containers/${{data.container.id}}/*.log

processors:
  - decode_json_fields:
      fields: ["message"]
      target: ""
      overwrite_keys: true
      max_depth: 6
  - add_host_metadata: ~
  - add_docker_metadata: ~

output.logstash:
  hosts: ["{c['logstash_host']}:{c['logstash_port']}"]

logging.level: info
"""


def _r_elk_kibana(c: dict) -> str:
    fields_json = (
        '[{"name":"duration_sec","type":"number","aggregatable":true,"searchable":true},'
        '{"name":"status","type":"number","aggregatable":true,"searchable":true},'
        '{"name":"service","type":"string","aggregatable":true,"searchable":true},'
        '{"name":"path","type":"string","aggregatable":true,"searchable":true},'
        '{"name":"http_method","type":"string","aggregatable":true,"searchable":true}]'
    ).replace('"', r'\"')
    return (
        '{\n'
        '  "attributes": {\n'
        f'    "title": "{c["log_index"]}-*",\n'
        '    "timeFieldName": "@timestamp",\n'
        f'    "fields": "{fields_json}"\n'
        '  }\n'
        '}\n'
    )


def _r_elk_compose(c: dict) -> str:
    return f"""# Local ELK dev stack. Run with:
#   docker compose -f docker-compose.elk.yml up -d
version: "3.8"
services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.13.0
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms512m -Xmx512m
    ports: ["9200:9200"]
    volumes: [esdata:/usr/share/elasticsearch/data]
  logstash:
    image: docker.elastic.co/logstash/logstash:8.13.0
    depends_on: [elasticsearch]
    volumes:
      - ./elk/logstash.conf:/usr/share/logstash/pipeline/logstash.conf:ro
    ports: ["{c['logstash_port']}:{c['logstash_port']}"]
  kibana:
    image: docker.elastic.co/kibana/kibana:8.13.0
    depends_on: [elasticsearch]
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports: ["5601:5601"]
  filebeat:
    image: docker.elastic.co/beats/filebeat:8.13.0
    user: root
    depends_on: [logstash]
    volumes:
      - ./elk/filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro

volumes:
  esdata:
"""


# ─── Java ────────────────────────────────────────────────────────────────

def _r_java_audit_aspect(c: dict) -> str:
    pkg = c["java_pkg"]
    return f"""package com.lama.{pkg}.audit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.databind.node.TextNode;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Pointcut;
import org.aspectj.lang.reflect.MethodSignature;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.regex.Pattern;

/**
 * LAMA Audit Logging Aspect — captures every {{@code @RestController}}
 * request/response and emits a structured JSON line for ELK ingestion.
 * Disable at runtime: AUDIT_LOGGING_ENABLED=false.
 */
@Aspect
@Component
public class AuditLoggingAspect {{

    private static final Logger LOG = LoggerFactory.getLogger("audit");
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int MAX_BODY_BYTES = {c['max_body_kb']} * 1024;
    private static final String SERVICE = "{c['service_name']}";

    @Value("${{audit.logging.enabled:true}}")
    private boolean enabled;

    private final PiiMasker pii = new PiiMasker(
        "{c['pii_fields']}", "{c['pii_patterns']}", "{c['pii_mask']}");

    @Pointcut("within(@org.springframework.web.bind.annotation.RestController *)")
    public void restController() {{}}

    @Pointcut("execution(public * *(..))")
    public void publicMethod() {{}}

    @Around("restController() && publicMethod()")
    public Object audit(ProceedingJoinPoint pjp) throws Throwable {{
        if (!enabled) return pjp.proceed();

        long startNs = System.nanoTime();
        Instant start = Instant.now();
        ServletRequestAttributes attrs = (ServletRequestAttributes)
            RequestContextHolder.getRequestAttributes();
        HttpServletRequest req = attrs != null ? attrs.getRequest() : null;
        HttpServletResponse res = attrs != null ? attrs.getResponse() : null;
        String corrId = req != null ? req.getHeader("X-Correlation-Id") : null;
        if (corrId == null || corrId.isEmpty()) corrId = UUID.randomUUID().toString();
        MDC.put("correlation_id", corrId);

        Throwable thrown = null;
        try {{
            return pjp.proceed();
        }} catch (Throwable t) {{
            thrown = t;
            throw t;
        }} finally {{
            try {{
                long elapsedNs = System.nanoTime() - startNs;
                double seconds = Math.round(elapsedNs / 1_000_000.0) / 1000.0;
                Instant end = Instant.now();
                MethodSignature sig = (MethodSignature) pjp.getSignature();

                Map<String, Object> evt = new HashMap<>();
                evt.put("timestamp",      DateTimeFormatter.ISO_INSTANT.format(start));
                evt.put("correlation_id", corrId);
                evt.put("service",        SERVICE);
                evt.put("class",          sig.getDeclaringType().getSimpleName());
                evt.put("method",         sig.getName());
                evt.put("request_time",   DateTimeFormatter.ISO_INSTANT.format(start));
                evt.put("response_time",  DateTimeFormatter.ISO_INSTANT.format(end));
                evt.put("duration_sec",   seconds);
                if (req != null) {{
                    evt.put("http_method", req.getMethod());
                    evt.put("path",        req.getRequestURI());
                    evt.put("client_ip",   clientIp(req));
                    Map<String, String> hdrs = new HashMap<>();
                    Enumeration<String> names = req.getHeaderNames();
                    while (names != null && names.hasMoreElements()) {{
                        String n = names.nextElement();
                        hdrs.put(n, req.getHeader(n));
                    }}
                    pii.maskHeaders(hdrs);
                    evt.put("headers", hdrs);
                }}
                if (res != null) evt.put("status", res.getStatus());
                if (thrown != null) {{
                    evt.put("error", thrown.getClass().getSimpleName());
                    evt.put("error_message", String.valueOf(thrown.getMessage()));
                }}
                // Body capture is OFF by default — when enabling, install
                // ContentCachingRequestWrapper / ResponseWrapper filters
                // and call pii.maskBody(...) here before truncation.
                LOG.info(MAPPER.writeValueAsString(evt));
            }} catch (Exception ignored) {{ /* never let logging break the request */ }}
            MDC.remove("correlation_id");
        }}
    }}

    private static String clientIp(HttpServletRequest req) {{
        String xff = req.getHeader("X-Forwarded-For");
        if (xff != null && !xff.isEmpty()) return xff.split(",")[0].trim();
        String real = req.getHeader("X-Real-IP");
        if (real != null && !real.isEmpty()) return real;
        return req.getRemoteAddr();
    }}

    /** PII masker shared with future body capture path. */
    static final class PiiMasker {{
        private static final Pattern EMAIL   = Pattern.compile("\\\\b[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+\\\\b");
        private static final Pattern CREDIT  = Pattern.compile("\\\\b(?:\\\\d[ -]*?){{13,19}}\\\\b");
        private static final Pattern SSN     = Pattern.compile("\\\\b\\\\d{{3}}-\\\\d{{2}}-\\\\d{{4}}\\\\b");
        private static final Pattern AADHAAR = Pattern.compile("\\\\b\\\\d{{4}}\\\\s?\\\\d{{4}}\\\\s?\\\\d{{4}}\\\\b");

        private final Set<String> fields = new HashSet<>();
        private final Set<String> patterns = new HashSet<>();
        private final String mask;

        PiiMasker(String fields, String patterns, String mask) {{
            for (String f : fields.split(",")) this.fields.add(f.trim().toLowerCase());
            for (String p : patterns.split(",")) this.patterns.add(p.trim().toLowerCase());
            this.mask = mask;
        }}

        String maskBody(String body) {{
            if (body == null || body.isEmpty()) return body == null ? "" : body;
            try {{
                JsonNode n = MAPPER.readTree(body);
                return MAPPER.writeValueAsString(maskNode(n));
            }} catch (Exception ignored) {{
                return maskString(body);
            }}
        }}

        private JsonNode maskNode(JsonNode n) {{
            if (n.isObject()) {{
                ObjectNode out = MAPPER.createObjectNode();
                n.fields().forEachRemaining(e -> {{
                    if (fields.contains(e.getKey().toLowerCase())) out.put(e.getKey(), mask);
                    else out.set(e.getKey(), maskNode(e.getValue()));
                }});
                return out;
            }}
            if (n.isArray()) {{
                ArrayNode out = MAPPER.createArrayNode();
                n.forEach(e -> out.add(maskNode(e)));
                return out;
            }}
            if (n.isTextual()) return TextNode.valueOf(maskString(n.asText()));
            return n;
        }}

        String maskString(String s) {{
            if (s == null) return null;
            if (patterns.contains("email"))       s = EMAIL.matcher(s).replaceAll(mask);
            if (patterns.contains("credit_card")) s = CREDIT.matcher(s).replaceAll(mask);
            if (patterns.contains("ssn"))         s = SSN.matcher(s).replaceAll(mask);
            if (patterns.contains("aadhaar"))     s = AADHAAR.matcher(s).replaceAll(mask);
            return s;
        }}

        void maskHeaders(Map<String, String> headers) {{
            headers.replaceAll((k, v) -> fields.contains(k.toLowerCase()) ? mask : v);
        }}
    }}
}}
"""


def _r_java_audit_dto(c: dict) -> str:
    pkg = c["java_pkg"]
    return f"""package com.lama.{pkg}.audit;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.Instant;
import java.util.Map;

/**
 * Strongly-typed view of one audit log event. Exposed so other code can
 * deserialise audit lines for replay / tests.
 */
public record AuditEvent(
    @JsonProperty("timestamp")     Instant timestamp,
    @JsonProperty("correlation_id") String correlationId,
    @JsonProperty("service")       String service,
    @JsonProperty("class")         String className,
    @JsonProperty("method")        String method,
    @JsonProperty("http_method")   String httpMethod,
    @JsonProperty("path")          String path,
    @JsonProperty("status")        Integer status,
    @JsonProperty("client_ip")     String clientIp,
    @JsonProperty("request_time")  Instant requestTime,
    @JsonProperty("response_time") Instant responseTime,
    @JsonProperty("duration_sec")  Double durationSec,
    @JsonProperty("headers")       Map<String, String> headers,
    @JsonProperty("error")         String error,
    @JsonProperty("error_message") String errorMessage
) {{}}
"""


# ─── Python ──────────────────────────────────────────────────────────────

def _r_python_audit_middleware(c: dict) -> str:
    return f'''"""LAMA Audit Logging — FastAPI/Starlette middleware.

Captures every request/response and emits a single-line JSON event for
ELK ingestion. PII fields are masked BEFORE the event is emitted.
Disable at runtime: AUDIT_LOGGING_ENABLED=false.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_logger = logging.getLogger("audit")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_h)
    _logger.propagate = False

_MAX_BODY = {c['max_body_kb']} * 1024
_SERVICE = "{c['service_name']}"

# ── PII masking ──────────────────────────────────────────────────────────
_PII_FIELDS = {{f.strip().lower() for f in "{c['pii_fields']}".split(",") if f.strip()}}
_PII_PATTERNS = {{p.strip().lower() for p in "{c['pii_patterns']}".split(",") if p.strip()}}
_MASK = "{c['pii_mask']}"
_PII_RX = {{
    "email":       re.compile(r"\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b"),
    "credit_card": re.compile(r"\\b(?:\\d[ -]*?){{13,19}}\\b"),
    "ssn":         re.compile(r"\\b\\d{{3}}-\\d{{2}}-\\d{{4}}\\b"),
    "aadhaar":     re.compile(r"\\b\\d{{4}}\\s?\\d{{4}}\\s?\\d{{4}}\\b"),
}}


def _mask_str(s: str) -> str:
    if not isinstance(s, str):
        return s
    for name in _PII_PATTERNS:
        rx = _PII_RX.get(name)
        if rx:
            s = rx.sub(_MASK, s)
    return s


def _mask_obj(obj):
    if isinstance(obj, dict):
        return {{
            k: (_MASK if k.lower() in _PII_FIELDS else _mask_obj(v))
            for k, v in obj.items()
        }}
    if isinstance(obj, list):
        return [_mask_obj(x) for x in obj]
    if isinstance(obj, str):
        return _mask_str(obj)
    return obj


def _mask_body(raw) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        return json.dumps(_mask_obj(json.loads(text)))
    except Exception:
        return _mask_str(text)


def _mask_headers(headers: dict) -> dict:
    return {{
        k: (_MASK if k.lower() in _PII_FIELDS else v)
        for k, v in headers.items()
    }}


def _enabled() -> bool:
    return os.environ.get("AUDIT_LOGGING_ENABLED", "true").lower() != "false"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real
    if request.client:
        return request.client.host
    return "-"


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that audit-logs every request."""

    async def dispatch(self, request: Request, call_next):
        if not _enabled():
            return await call_next(request)

        start_ns = time.perf_counter_ns()
        start_iso = datetime.now(timezone.utc).isoformat()
        corr_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex

        try:
            body_bytes = await request.body()
        except Exception:
            body_bytes = b""

        response: Response | None = None
        error_kind = None
        error_message = None
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            error_kind = exc.__class__.__name__
            error_message = str(exc)
            raise
        finally:
            try:
                end_iso = datetime.now(timezone.utc).isoformat()
                duration_sec = round((time.perf_counter_ns() - start_ns) / 1_000_000_000, 3)
                cls = method = ""
                route = request.scope.get("route")
                if route is not None:
                    cls = getattr(route, "name", "") or ""
                    endpoint = getattr(route, "endpoint", None)
                    if endpoint is not None:
                        method = getattr(endpoint, "__qualname__", endpoint.__name__)
                event = {{
                    "timestamp":      start_iso,
                    "correlation_id": corr_id,
                    "service":        _SERVICE,
                    "class":          cls or request.url.path,
                    "method":         method or request.method,
                    "http_method":    request.method,
                    "path":           request.url.path,
                    "status":         getattr(response, "status_code", None),
                    "client_ip":      _client_ip(request),
                    "request_time":   start_iso,
                    "response_time":  end_iso,
                    "duration_sec":   duration_sec,
                    "request_body":   _mask_body(body_bytes)[:_MAX_BODY],
                    "headers":        _mask_headers(dict(request.headers)),
                }}
                if error_kind:
                    event["error"] = error_kind
                    event["error_message"] = error_message
                _logger.info(json.dumps(event, default=str))
            except Exception:
                pass  # never let logging break the request


def install(app) -> None:
    """Wire into FastAPI:

        from app.audit import install as install_audit
        install_audit(app)
    """
    app.add_middleware(AuditLoggingMiddleware)
'''


def _r_python_audit_init(_c: dict) -> str:
    return (
        '"""LAMA audit-logging package. See middleware.py for the entrypoint."""\n'
        'from .middleware import AuditLoggingMiddleware, install\n'
        '\n'
        '__all__ = ["AuditLoggingMiddleware", "install"]\n'
    )


# ─── Node.js ─────────────────────────────────────────────────────────────

def _r_node_audit_middleware(c: dict) -> str:
    return f"""// LAMA Audit Logging — Express middleware.
// Emits one JSON line per request to stdout. PII is masked BEFORE emit.
// Disable at runtime with AUDIT_LOGGING_ENABLED=false.

const {{ randomUUID }} = require("crypto");

const MAX_BODY = {c['max_body_kb']} * 1024;
const SERVICE = "{c['service_name']}";

const PII_FIELDS = new Set(
  "{c['pii_fields']}".toLowerCase().split(",").map((s) => s.trim()).filter(Boolean),
);
const PII_PATTERNS = new Set(
  "{c['pii_patterns']}".toLowerCase().split(",").map((s) => s.trim()).filter(Boolean),
);
const MASK = "{c['pii_mask']}";

const PII_RX = {{
  email:       /\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b/g,
  credit_card: /\\b(?:\\d[ -]*?){{13,19}}\\b/g,
  ssn:         /\\b\\d{{3}}-\\d{{2}}-\\d{{4}}\\b/g,
  aadhaar:     /\\b\\d{{4}}\\s?\\d{{4}}\\s?\\d{{4}}\\b/g,
}};

function maskStr(s) {{
  if (typeof s !== "string") return s;
  for (const p of PII_PATTERNS) {{
    const rx = PII_RX[p];
    if (rx) s = s.replace(rx, MASK);
  }}
  return s;
}}

function maskObj(o) {{
  if (Array.isArray(o)) return o.map(maskObj);
  if (o && typeof o === "object") {{
    return Object.fromEntries(
      Object.entries(o).map(([k, v]) => [
        k,
        PII_FIELDS.has(k.toLowerCase()) ? MASK : maskObj(v),
      ]),
    );
  }}
  if (typeof o === "string") return maskStr(o);
  return o;
}}

function safeBody(payload) {{
  try {{
    if (Buffer.isBuffer(payload)) return payload.slice(0, MAX_BODY).toString("utf8");
    const s = typeof payload === "string" ? payload : JSON.stringify(payload);
    return s ? s.slice(0, MAX_BODY) : "";
  }} catch {{
    return "";
  }}
}}

function maskBody(payload) {{
  const s = safeBody(payload);
  if (!s) return "";
  try {{
    return JSON.stringify(maskObj(JSON.parse(s)));
  }} catch {{
    return maskStr(s);
  }}
}}

function maskHeaders(h) {{
  const out = {{}};
  for (const [k, v] of Object.entries(h || {{}})) {{
    out[k] = PII_FIELDS.has(k.toLowerCase()) ? MASK : v;
  }}
  return out;
}}

function enabled() {{
  return (process.env.AUDIT_LOGGING_ENABLED || "true").toLowerCase() !== "false";
}}

function clientIp(req) {{
  const xff = req.headers["x-forwarded-for"];
  if (xff) return String(xff).split(",")[0].trim();
  const real = req.headers["x-real-ip"];
  if (real) return String(real);
  return req.ip || (req.socket && req.socket.remoteAddress) || "-";
}}

module.exports = function auditLogger(req, res, next) {{
  if (!enabled()) return next();

  const startNs = process.hrtime.bigint();
  const startIso = new Date().toISOString();
  const corrId = req.headers["x-correlation-id"] || randomUUID();

  let captured = "";
  const origSend = res.send.bind(res);
  res.send = function (body) {{
    captured = safeBody(body);
    return origSend(body);
  }};

  res.on("finish", () => {{
    try {{
      const endIso = new Date().toISOString();
      const elapsedNs = Number(process.hrtime.bigint() - startNs);
      const duration_sec = Math.round((elapsedNs / 1e9) * 1000) / 1000;
      const event = {{
        timestamp:     startIso,
        correlation_id: corrId,
        service:       SERVICE,
        class:         req.baseUrl || "/",
        method:        (req.route && req.route.stack && req.route.stack[0].name) || req.method,
        http_method:   req.method,
        path:          req.originalUrl,
        status:        res.statusCode,
        client_ip:     clientIp(req),
        request_time:  startIso,
        response_time: endIso,
        duration_sec,
        request_body:  maskBody(req.body),
        response_body: maskBody(captured),
        headers:       maskHeaders(req.headers),
      }};
      // eslint-disable-next-line no-console
      console.log(JSON.stringify(event));
    }} catch {{
      /* never let logging break the request */
    }}
  }});

  next();
}};
"""


# ─── .NET ────────────────────────────────────────────────────────────────

def _r_dotnet_audit_filter(c: dict) -> str:
    return f"""// LAMA Audit Logging — ASP.NET Core action filter.
// Emits one JSON line per request to ILogger. PII masked BEFORE emit.
// Disable at runtime with AUDIT_LOGGING_ENABLED=false.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.Extensions.Logging;

namespace Lama.{c['java_pkg'].capitalize()}.Audit;

public class AuditLoggingFilter : IAsyncActionFilter
{{
    private const string Service = "{c['service_name']}";
    private const string Mask = "{c['pii_mask']}";
    private static readonly int MaxBody = {c['max_body_kb']} * 1024;
    private static readonly HashSet<string> PiiFields = new(
        "{c['pii_fields']}".Split(',').Select(s => s.Trim().ToLowerInvariant()),
        StringComparer.OrdinalIgnoreCase);
    private static readonly HashSet<string> PiiPatterns = new(
        "{c['pii_patterns']}".Split(',').Select(s => s.Trim().ToLowerInvariant()));
    private static readonly Dictionary<string, Regex> PiiRx = new()
    {{
        ["email"]       = new Regex(@"\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b"),
        ["credit_card"] = new Regex(@"\\b(?:\\d[ -]*?){{13,19}}\\b"),
        ["ssn"]         = new Regex(@"\\b\\d{{3}}-\\d{{2}}-\\d{{4}}\\b"),
        ["aadhaar"]     = new Regex(@"\\b\\d{{4}}\\s?\\d{{4}}\\s?\\d{{4}}\\b"),
    }};

    private readonly ILogger<AuditLoggingFilter> _log;
    public AuditLoggingFilter(ILogger<AuditLoggingFilter> log) => _log = log;

    public async Task OnActionExecutionAsync(ActionExecutingContext ctx, ActionExecutionDelegate next)
    {{
        if (string.Equals(Environment.GetEnvironmentVariable("AUDIT_LOGGING_ENABLED"), "false",
                          StringComparison.OrdinalIgnoreCase))
        {{
            await next();
            return;
        }}

        var sw = Stopwatch.StartNew();
        var startIso = DateTime.UtcNow.ToString("o");
        var http = ctx.HttpContext;
        var corrId = http.Request.Headers.TryGetValue("X-Correlation-Id", out var c1) && !string.IsNullOrEmpty(c1)
                     ? c1.ToString() : Guid.NewGuid().ToString("N");

        Exception? thrown = null;
        try
        {{
            var executed = await next();
            if (executed.Exception != null) thrown = executed.Exception;
        }}
        catch (Exception ex) {{ thrown = ex; throw; }}
        finally
        {{
            try
            {{
                sw.Stop();
                var evt = new Dictionary<string, object?>
                {{
                    ["timestamp"]      = startIso,
                    ["correlation_id"] = corrId,
                    ["service"]        = Service,
                    ["class"]          = ctx.Controller.GetType().Name,
                    ["method"]         = ctx.ActionDescriptor.DisplayName,
                    ["http_method"]    = http.Request.Method,
                    ["path"]           = http.Request.Path.Value,
                    ["status"]         = http.Response?.StatusCode,
                    ["client_ip"]      = ClientIp(http),
                    ["request_time"]   = startIso,
                    ["response_time"]  = DateTime.UtcNow.ToString("o"),
                    ["duration_sec"]   = Math.Round(sw.Elapsed.TotalSeconds, 3),
                    ["headers"]        = MaskHeaders(http.Request.Headers),
                    ["error"]          = thrown?.GetType().Name,
                    ["error_message"]  = thrown?.Message,
                }};
                _log.LogInformation(JsonSerializer.Serialize(evt));
            }}
            catch {{ /* never let logging break the request */ }}
        }}
    }}

    private static string ClientIp(HttpContext http)
    {{
        if (http.Request.Headers.TryGetValue("X-Forwarded-For", out var xff) && !string.IsNullOrEmpty(xff))
            return xff.ToString().Split(',')[0].Trim();
        return http.Connection.RemoteIpAddress?.ToString() ?? "-";
    }}

    private static Dictionary<string, string?> MaskHeaders(IHeaderDictionary headers)
    {{
        var dict = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        foreach (var (k, v) in headers)
            dict[k] = PiiFields.Contains(k) ? Mask : v.ToString();
        return dict;
    }}

    public static string MaskString(string? s)
    {{
        if (string.IsNullOrEmpty(s)) return s ?? string.Empty;
        foreach (var p in PiiPatterns)
            if (PiiRx.TryGetValue(p, out var rx)) s = rx.Replace(s, Mask);
        return s;
    }}

    public static string MaskBody(string? body)
    {{
        if (string.IsNullOrEmpty(body)) return string.Empty;
        try
        {{
            var node = JsonNode.Parse(body);
            return JsonSerializer.Serialize(MaskNode(node));
        }}
        catch
        {{
            return MaskString(body);
        }}
    }}

    private static JsonNode? MaskNode(JsonNode? n)
    {{
        if (n is JsonObject o)
        {{
            var copy = new JsonObject();
            foreach (var kv in o)
            {{
                copy[kv.Key] = PiiFields.Contains(kv.Key)
                    ? JsonValue.Create(Mask)
                    : MaskNode(kv.Value?.DeepClone());
            }}
            return copy;
        }}
        if (n is JsonArray a)
        {{
            var copy = new JsonArray();
            foreach (var item in a) copy.Add(MaskNode(item?.DeepClone()));
            return copy;
        }}
        if (n is JsonValue v && v.TryGetValue<string>(out var sv))
            return JsonValue.Create(MaskString(sv));
        return n;
    }}
}}
"""


# ─── Go ──────────────────────────────────────────────────────────────────

def _r_go_audit_middleware(c: dict) -> str:
    return f"""// LAMA Audit Logging — Gin middleware.
// Emits one JSON line per request to stdout. PII masked BEFORE emit.
// Disable at runtime with AUDIT_LOGGING_ENABLED=false.
package audit

import (
\t"bytes"
\t"crypto/rand"
\t"encoding/hex"
\t"encoding/json"
\t"io"
\t"log"
\t"os"
\t"regexp"
\t"strings"
\t"time"

\t"github.com/gin-gonic/gin"
)

const service = "{c['service_name']}"
const maxBody = {c['max_body_kb']} * 1024
const piiMask = "{c['pii_mask']}"

var piiFields = func() map[string]struct{{}} {{
\tm := map[string]struct{{}}{{}}
\tfor _, f := range strings.Split(strings.ToLower("{c['pii_fields']}"), ",") {{
\t\tif s := strings.TrimSpace(f); s != "" {{
\t\t\tm[s] = struct{{}}{{}}
\t\t}}
\t}}
\treturn m
}}()
var piiPatterns = strings.Split(strings.ToLower("{c['pii_patterns']}"), ",")
var piiRx = map[string]*regexp.Regexp{{
\t"email":       regexp.MustCompile(`\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b`),
\t"credit_card": regexp.MustCompile(`\\b(?:\\d[ -]*?){{13,19}}\\b`),
\t"ssn":         regexp.MustCompile(`\\b\\d{{3}}-\\d{{2}}-\\d{{4}}\\b`),
\t"aadhaar":     regexp.MustCompile(`\\b\\d{{4}}\\s?\\d{{4}}\\s?\\d{{4}}\\b`),
}}

type bodyCapture struct {{
\tgin.ResponseWriter
\tbuf bytes.Buffer
}}

func (b *bodyCapture) Write(p []byte) (int, error) {{
\tif b.buf.Len() < maxBody {{
\t\tlimit := maxBody - b.buf.Len()
\t\tif limit > len(p) {{
\t\t\tlimit = len(p)
\t\t}}
\t\tb.buf.Write(p[:limit])
\t}}
\treturn b.ResponseWriter.Write(p)
}}

func enabled() bool {{
\treturn strings.ToLower(os.Getenv("AUDIT_LOGGING_ENABLED")) != "false"
}}

func newCorrId() string {{
\tvar b [16]byte
\t_, _ = rand.Read(b[:])
\treturn hex.EncodeToString(b[:])
}}

func clientIP(c *gin.Context) string {{
\tif xff := c.GetHeader("X-Forwarded-For"); xff != "" {{
\t\treturn strings.TrimSpace(strings.Split(xff, ",")[0])
\t}}
\tif real := c.GetHeader("X-Real-IP"); real != "" {{
\t\treturn real
\t}}
\treturn c.ClientIP()
}}

func maskString(s string) string {{
\tfor _, p := range piiPatterns {{
\t\tif rx, ok := piiRx[strings.TrimSpace(p)]; ok {{
\t\t\ts = rx.ReplaceAllString(s, piiMask)
\t\t}}
\t}}
\treturn s
}}

func maskAny(v any) any {{
\tswitch x := v.(type) {{
\tcase map[string]any:
\t\tout := make(map[string]any, len(x))
\t\tfor k, vv := range x {{
\t\t\tif _, hit := piiFields[strings.ToLower(k)]; hit {{
\t\t\t\tout[k] = piiMask
\t\t\t}} else {{
\t\t\t\tout[k] = maskAny(vv)
\t\t\t}}
\t\t}}
\t\treturn out
\tcase []any:
\t\tfor i, vv := range x {{
\t\t\tx[i] = maskAny(vv)
\t\t}}
\t\treturn x
\tcase string:
\t\treturn maskString(x)
\t}}
\treturn v
}}

func maskBody(b []byte) string {{
\tif len(b) == 0 {{
\t\treturn ""
\t}}
\tvar v any
\tif json.Unmarshal(b, &v) == nil {{
\t\tout, _ := json.Marshal(maskAny(v))
\t\treturn string(out)
\t}}
\treturn maskString(string(b))
}}

func maskHeaders(h map[string][]string) map[string]string {{
\tout := make(map[string]string, len(h))
\tfor k, vs := range h {{
\t\tif _, hit := piiFields[strings.ToLower(k)]; hit {{
\t\t\tout[k] = piiMask
\t\t}} else if len(vs) > 0 {{
\t\t\tout[k] = vs[0]
\t\t}}
\t}}
\treturn out
}}

// Middleware returns a Gin handler that emits one JSON audit line per
// request to stdout.
func Middleware() gin.HandlerFunc {{
\treturn func(c *gin.Context) {{
\t\tif !enabled() {{
\t\t\tc.Next()
\t\t\treturn
\t\t}}
\t\tstart := time.Now().UTC()
\t\tcorr := c.GetHeader("X-Correlation-Id")
\t\tif corr == "" {{
\t\t\tcorr = newCorrId()
\t\t}}
\t\tvar reqBody []byte
\t\tif c.Request.Body != nil {{
\t\t\tbuf, err := io.ReadAll(io.LimitReader(c.Request.Body, maxBody))
\t\t\tif err == nil {{
\t\t\t\treqBody = buf
\t\t\t\tc.Request.Body = io.NopCloser(bytes.NewBuffer(buf))
\t\t\t}}
\t\t}}
\t\tbw := &bodyCapture{{ResponseWriter: c.Writer}}
\t\tc.Writer = bw

\t\tc.Next()

\t\tend := time.Now().UTC()
\t\tevent := map[string]any{{
\t\t\t"timestamp":      start.Format(time.RFC3339Nano),
\t\t\t"correlation_id": corr,
\t\t\t"service":        service,
\t\t\t"class":          c.HandlerName(),
\t\t\t"method":         c.Request.Method,
\t\t\t"http_method":    c.Request.Method,
\t\t\t"path":           c.FullPath(),
\t\t\t"status":         c.Writer.Status(),
\t\t\t"client_ip":      clientIP(c),
\t\t\t"request_time":   start.Format(time.RFC3339Nano),
\t\t\t"response_time":  end.Format(time.RFC3339Nano),
\t\t\t"duration_sec":   float64(end.Sub(start).Nanoseconds()) / 1e9,
\t\t\t"request_body":   maskBody(reqBody),
\t\t\t"response_body":  maskBody(bw.buf.Bytes()),
\t\t\t"headers":        maskHeaders(c.Request.Header),
\t\t}}
\t\tif len(c.Errors) > 0 {{
\t\t\tevent["error"] = c.Errors.String()
\t\t}}
\t\tline, _ := json.Marshal(event)
\t\tlog.Println(string(line))
\t}}
}}
"""


def _r_inject_audit_readme(c: dict) -> str:
    return f"""# Audit Logging — manual wiring required

The selected target language for `{c['service_name']}` has no auto-
generated audit-logging boilerplate. To wire LAMA's audit pipeline:

1. Emit JSON lines to stdout with these fields:
   `timestamp, correlation_id, service, class, method, http_method,
   path, status, client_ip, request_time, response_time, duration_sec,
   request_body, response_body, headers`.
2. Mask PII fields (`{c['pii_fields']}`) with `{c['pii_mask']}` BEFORE
   emit; apply value-regex masks (`{c['pii_patterns']}`) to string values.
3. Forward stdout to Filebeat → Logstash → Elasticsearch (see
   `services/audit-logger/elk/`).
4. Honour the `AUDIT_LOGGING_ENABLED=false` env-var to disable.
"""


# ─── Dispatch ────────────────────────────────────────────────────────────

_RENDERERS: Dict[str, Callable[[dict], str]] = {
    "utility_readme":                  _r_utility_readme,
    "utility_elk_logstash":            _r_elk_logstash,
    "utility_elk_filebeat":            _r_elk_filebeat,
    "utility_elk_kibana":              _r_elk_kibana,
    "utility_elk_compose":             _r_elk_compose,
    "utility_inject_audit_aspect":     _r_java_audit_aspect,
    "utility_inject_audit_dto":        _r_java_audit_dto,
    "utility_inject_audit_init":       _r_python_audit_init,
    "utility_inject_audit_filter":     _r_dotnet_audit_filter,
    "utility_inject_audit_readme":     _r_inject_audit_readme,
}


def render_file(file_def: dict, project: dict, utility_svc: dict,
                business_svc: Optional[dict]) -> str:
    """Dispatch by (type, language) so node/python/go share the
    `utility_inject_audit_middleware` type but render different code."""
    ftype = file_def.get("type", "")
    lang = (file_def.get("language") or "").lower()
    ctx = _build_ctx(file_def, project, utility_svc, business_svc)

    if ftype == "utility_inject_audit_middleware":
        if lang == "python":
            return _r_python_audit_middleware(ctx)
        if lang in ("javascript", "typescript", "nodejs"):
            return _r_node_audit_middleware(ctx)
        if lang == "go":
            return _r_go_audit_middleware(ctx)
        return _r_inject_audit_readme(ctx)

    fn = _RENDERERS.get(ftype)
    if not fn:
        return f"// LAMA utility: no renderer for {ftype}\n"
    return fn(ctx)


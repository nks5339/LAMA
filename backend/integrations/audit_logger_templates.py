"""LAMA — Audit-Trail Logger templates (iter-13.62).

A *utility* integration (kind="middleware") that injects an ELK-friendly
request/response audit-trail aspect into every generated micro-service:

  * Python (FastAPI) -> Starlette `BaseHTTPMiddleware` mounted via
    `app.add_middleware(...)`.
  * Java (Spring Boot 3) -> AspectJ `@Aspect @Around("within(@RestController *)")`
    auto-discovered by component scan (needs `spring-boot-starter-aop`).
  * Node.js (Express) -> a `RequestHandler` mounted via
    `app.use(auditTrailMiddleware)`.

Every target logs the same envelope (single-line JSON when
`AUDIT_TRAIL_LOG_FORMAT=json`, default — ready for Filebeat / Fluent Bit ->
Logstash -> Elasticsearch):

    {
      "@timestamp": "2026-06-12T12:34:56.123Z",
      "service": "<service-name>",
      "request_id": "<uuid12>",
      "ip": "<client ip>",
      "method": "POST",
      "path": "/api/...",
      "class": "<fully-qualified class / module>",
      "handler": "<function / handler name>",
      "status": 200,
      "request_time":  "...",
      "response_time": "...",
      "duration_sec":  0.123456,
      "request_body":  {...PII-masked...},   // only if AUDIT_TRAIL_LOG_BODY=1
      "response_body": {...PII-masked...},   // only if AUDIT_TRAIL_LOG_BODY=1
    }

PII fields (case-insensitive key names) and PII regex patterns are masked
with `****`. Defaults cover password / OTP / token / Aadhaar / PAN / card /
email / mobile etc., and are env-overridable.

Modes (env `AUDIT_TRAIL_MODE`):
  * `off`  - no logging at all (zero overhead path).
  * `mock` - log to stdout/stderr only (no remote push). Default.
  * `live` - log to stdout AND POST every record to AUDIT_TRAIL_LOGSTASH_URL
            (if configured). No code change — env-flip only.
"""

# ---------------------------------------------------------------------------
# Python — FastAPI / Starlette middleware
# ---------------------------------------------------------------------------

AUDIT_LOG_PYTHON = '''"""LAMA-injected utility: {label} ({integration_id}).

ELK-friendly request/response audit-trail middleware.

{env_doc}

Mount in your FastAPI app:

    from app.integrations.{integration_id}.middleware import AuditTrailMiddleware
    app.add_middleware(AuditTrailMiddleware)

Modes (env {gate_env}): off | mock | live.
PII fields masked with ****. Add fields/regexes via env vars below.
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, List, Set

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger("audit.trail")


# --- helpers ---------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v if v is not None else default


def _mode() -> str:
    return (_env("{gate_env}", "mock").strip().lower() or "mock")


def _ssl_verify():
    if _env("LAMA_DISABLE_SSL_VERIFY", "").strip() in ("1", "true", "yes"):
        return False
    bundle = _env("LAMA_CA_BUNDLE", "").strip()
    return bundle or True


_DEFAULT_PII_FIELDS = (
    "password,pwd,passwd,pan,aadhaar,aadhar,otp,token,authorization,api_key,"
    "apikey,secret,client_secret,ssn,email,mobile,phone,card,card_no,cvv,pin,"
    "session,cookie"
)
_DEFAULT_PII_PATTERNS = (
    # PAN (ABCDE1234F), Aadhaar (12 digits starting 2-9), credit-card (12-19 digits),
    # email, IPv4
    r"[A-Z]{{5}}[0-9]{{4}}[A-Z],"
    r"\\b[2-9][0-9]{{11}}\\b,"
    r"\\b[0-9]{{12,19}}\\b,"
    r"\\b[\\w.+-]+@[\\w-]+\\.[\\w.-]+\\b"
)


def _pii_fields() -> Set[str]:
    raw = _env("AUDIT_TRAIL_PII_FIELDS", _DEFAULT_PII_FIELDS)
    return {{f.strip().lower() for f in raw.split(",") if f.strip()}}


def _pii_patterns() -> List[re.Pattern]:
    raw = _env("AUDIT_TRAIL_PII_PATTERNS", _DEFAULT_PII_PATTERNS)
    out: List[re.Pattern] = []
    for p in raw.split(","):
        t = p.strip()
        if not t:
            continue
        try:
            out.append(re.compile(t))
        except re.error:
            continue
    return out


def _scrub_string(s: str, patterns: Iterable[re.Pattern]) -> str:
    out = s
    for p in patterns:
        out = p.sub("****", out)
    return out


def _scrub(obj: Any, fields: Set[str], patterns: List[re.Pattern]) -> Any:
    if isinstance(obj, dict):
        return {{
            k: ("****" if k.lower() in fields else _scrub(v, fields, patterns))
            for k, v in obj.items()
        }}
    if isinstance(obj, list):
        return [_scrub(x, fields, patterns) for x in obj]
    if isinstance(obj, str):
        return _scrub_string(obj, patterns)
    return obj


# --- middleware ------------------------------------------------------------

class AuditTrailMiddleware(BaseHTTPMiddleware):
    """LAMA aspect-equivalent for FastAPI.

    Logs per-request: ip, method, path, class, handler, status, request_time,
    response_time, duration_sec. Optionally captures req/resp body (PII-masked)
    when AUDIT_TRAIL_LOG_BODY=1.
    """

    async def dispatch(self, request: Request, call_next):
        mode = _mode()
        if mode == "off":
            return await call_next(request)

        excluded = {{
            p.strip() for p in _env(
                "AUDIT_TRAIL_EXCLUDE_PATHS",
                "/health,/metrics,/docs,/redoc,/openapi.json",
            ).split(",") if p.strip()
        }}
        path = request.url.path
        if path in excluded:
            return await call_next(request)

        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request_time_iso = datetime.now(timezone.utc).isoformat()
        started_perf = time.perf_counter()

        fwd = request.headers.get("x-forwarded-for", "")
        client_ip = fwd.split(",")[0].strip() if fwd else (
            request.client.host if request.client else ""
        )

        fields = _pii_fields()
        patterns = _pii_patterns()
        capture_body = _env("AUDIT_TRAIL_LOG_BODY", "0").strip() == "1"
        try:
            max_bytes = int(_env("AUDIT_TRAIL_MAX_BODY_BYTES", "2048"))
        except ValueError:
            max_bytes = 2048

        req_body_text = ""
        if capture_body:
            raw = await request.body()
            req_body_text = raw[:max_bytes].decode("utf-8", errors="replace")
            # Re-wrap the body so downstream handlers can still read it.
            async def _receive():
                return {{"type": "http.request", "body": raw, "more_body": False}}

            request = Request(request.scope, _receive)

        exception_text = ""
        response: Response
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            exception_text = repr(exc)
            response = Response(content=b"Internal Server Error", status_code=500,
                                media_type="text/plain")
            duration = round(time.perf_counter() - started_perf, 6)
            self._emit(
                request, request_id, request_time_iso, duration, client_ip,
                response.status_code, req_body_text, "",
                fields, patterns, mode, exception_text,
            )
            raise

        resp_body_text = ""
        if capture_body:
            if isinstance(response, StreamingResponse):
                chunks: List[bytes] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                raw = b"".join(chunks)
                resp_body_text = raw[:max_bytes].decode("utf-8", errors="replace")
                response = Response(
                    content=raw, status_code=response.status_code,
                    headers=dict(response.headers), media_type=response.media_type,
                )
            elif hasattr(response, "body"):
                resp_body_text = (response.body or b"")[:max_bytes].decode(
                    "utf-8", errors="replace")

        duration = round(time.perf_counter() - started_perf, 6)
        self._emit(
            request, request_id, request_time_iso, duration, client_ip,
            response.status_code, req_body_text, resp_body_text,
            fields, patterns, mode, exception_text,
        )
        return response

    # ---- helpers ----------------------------------------------------------

    def _emit(self, request, request_id, request_time_iso, duration, client_ip,
              status, req_body_text, resp_body_text, fields, patterns, mode,
              exception_text):
        response_time_iso = datetime.now(timezone.utc).isoformat()
        endpoint = request.scope.get("endpoint")
        handler = getattr(endpoint, "__name__", "") if endpoint else ""
        qual = getattr(endpoint, "__qualname__", "") if endpoint else ""
        cls = qual.rsplit(".", 1)[0] if (qual and "." in qual) else (
            getattr(endpoint, "__module__", "") if endpoint else ""
        )

        record = {{
            "@timestamp": response_time_iso,
            "service": _env("AUDIT_TRAIL_SERVICE_NAME", "integrations-service"),
            "request_id": request_id,
            "ip": client_ip or "unknown",
            "method": request.method,
            "path": str(request.url.path),
            "class": cls or "",
            "handler": handler or "",
            "status": int(status),
            "request_time": request_time_iso,
            "response_time": response_time_iso,
            "duration_sec": float(duration),
        }}
        if exception_text:
            record["exception"] = _scrub_string(exception_text, patterns)
        if req_body_text:
            try:
                record["request_body"] = _scrub(json.loads(req_body_text), fields, patterns)
            except (ValueError, TypeError):
                record["request_body"] = _scrub_string(req_body_text, patterns)
        if resp_body_text:
            try:
                record["response_body"] = _scrub(json.loads(resp_body_text), fields, patterns)
            except (ValueError, TypeError):
                record["response_body"] = _scrub_string(resp_body_text, patterns)

        fmt = _env("AUDIT_TRAIL_LOG_FORMAT", "json").strip().lower()
        if fmt == "text":
            logger.info(
                "audit ip=%s method=%s path=%s status=%s duration_sec=%s "
                "request_id=%s class=%s handler=%s",
                record["ip"], record["method"], record["path"], record["status"],
                record["duration_sec"], record["request_id"],
                record["class"], record["handler"],
            )
        else:
            try:
                logger.info(json.dumps(record, default=str, separators=(",", ":")))
            except (TypeError, ValueError):
                logger.info(str(record))

        if mode == "live":
            url = _env("AUDIT_TRAIL_LOGSTASH_URL", "").strip()
            if url:
                try:
                    with httpx.Client(timeout=2.0, verify=_ssl_verify()) as client:
                        client.post(url, json=record)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "audit.trail.logstash.push.failed url=%s request_id=%s",
                        url, request_id,
                    )
'''


# ---------------------------------------------------------------------------
# Java — Spring Boot 3 / AspectJ
# ---------------------------------------------------------------------------

AUDIT_LOG_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.http.HttpServletRequest;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Pointcut;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import java.util.regex.Pattern;

/**
 * LAMA-injected audit-trail aspect: intercepts every &#64;RestController
 * method, emits an ELK-ready JSON audit record per request (ip, class,
 * method, request_time, response_time, duration_sec, status, optional
 * request/response bodies), and masks PII (configurable field names + regex
 * patterns) with "****".
 *
 * <p>Mode env: {gate_env}=off|mock|live. live additionally POSTs every
 * record to AUDIT_TRAIL_LOGSTASH_URL if configured.</p>
 *
 * <p>Picks up automatically through component scan — no wiring needed; the
 * generated pom.xml pulls in spring-boot-starter-aop when this aspect is
 * enabled.</p>
 */
@Aspect
@Component
public class AuditTrailAspect {{

    private static final Logger LOG = LoggerFactory.getLogger("audit.trail");
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final HttpClient httpClient = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(2)).build();

    private static String env(String key, String def) {{
        String v = System.getenv(key);
        return (v == null || v.isEmpty()) ? def : v;
    }}

    @Pointcut("within(@org.springframework.web.bind.annotation.RestController *)")
    public void restControllerScope() {{}}

    @Around("restControllerScope()")
    public Object audit(ProceedingJoinPoint pjp) throws Throwable {{
        String mode = env("{gate_env}", "mock").toLowerCase();
        if ("off".equals(mode)) {{
            return pjp.proceed();
        }}

        String requestId = UUID.randomUUID().toString().substring(0, 12);
        Instant requestTime = Instant.now();
        long startedNs = System.nanoTime();
        MDC.put("request_id", requestId);

        HttpServletRequest req = currentRequest();
        String path = req != null ? req.getRequestURI() : "";
        String method = req != null ? req.getMethod() : "";
        String ip = clientIp(req);

        Set<String> excluded = csv(env(
            "AUDIT_TRAIL_EXCLUDE_PATHS",
            "/health,/metrics,/actuator,/actuator/health"));
        if (path != null && excluded.contains(path)) {{
            try {{ return pjp.proceed(); }} finally {{ MDC.remove("request_id"); }}
        }}

        String cls = pjp.getSignature().getDeclaringTypeName();
        String handler = pjp.getSignature().getName();
        boolean captureBody = "1".equals(env("AUDIT_TRAIL_LOG_BODY", "0"));
        int maxBytes;
        try {{ maxBytes = Integer.parseInt(env("AUDIT_TRAIL_MAX_BODY_BYTES", "2048")); }}
        catch (NumberFormatException nfe) {{ maxBytes = 2048; }}
        Set<String> piiFields = csvLower(env(
            "AUDIT_TRAIL_PII_FIELDS",
            "password,pwd,passwd,pan,aadhaar,aadhar,otp,token,authorization," +
            "api_key,apikey,secret,client_secret,ssn,email,mobile,phone," +
            "card,card_no,cvv,pin,session,cookie"));
        List<Pattern> piiPatterns = compilePatterns(env(
            "AUDIT_TRAIL_PII_PATTERNS",
            "[A-Z]{{5}}[0-9]{{4}}[A-Z]," +
            "\\\\b[2-9][0-9]{{11}}\\\\b," +
            "\\\\b[0-9]{{12,19}}\\\\b," +
            "\\\\b[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+\\\\b"));

        Object result = null;
        Throwable thrown = null;
        int status = 200;
        try {{
            result = pjp.proceed();
            return result;
        }} catch (Throwable t) {{
            thrown = t;
            status = 500;
            throw t;
        }} finally {{
            Instant responseTime = Instant.now();
            double duration = (System.nanoTime() - startedNs) / 1_000_000_000.0;

            Map<String, Object> record = new LinkedHashMap<>();
            record.put("@timestamp", responseTime.toString());
            record.put("service", env("AUDIT_TRAIL_SERVICE_NAME", "integrations-service"));
            record.put("request_id", requestId);
            record.put("ip", ip);
            record.put("method", method);
            record.put("path", path);
            record.put("class", cls);
            record.put("handler", handler);
            record.put("status", status);
            record.put("request_time", requestTime.toString());
            record.put("response_time", responseTime.toString());
            record.put("duration_sec", duration);

            if (captureBody && result != null) {{
                record.put("response_body", scrub(result, piiFields, piiPatterns, maxBytes));
            }}
            if (captureBody && req != null) {{
                // Spring's default request stream is consumed by the handler;
                // best-effort capture of query string + form params here.
                String qs = req.getQueryString();
                if (qs != null && !qs.isEmpty()) {{
                    record.put("query_string", scrubString(
                        qs.length() > maxBytes ? qs.substring(0, maxBytes) : qs,
                        piiPatterns));
                }}
            }}
            if (thrown != null) {{
                record.put("exception", scrubString(thrown.toString(), piiPatterns));
            }}

            try {{
                LOG.info(MAPPER.writeValueAsString(record));
            }} catch (Exception jsonErr) {{
                LOG.info(
                    "audit ip={{}} method={{}} path={{}} status={{}} duration_sec={{}} " +
                    "request_id={{}} class={{}} handler={{}}",
                    ip, method, path, status, duration, requestId, cls, handler);
            }}

            if ("live".equals(mode)) {{
                String url = env("AUDIT_TRAIL_LOGSTASH_URL", "");
                if (!url.isEmpty()) {{
                    try {{
                        String body = MAPPER.writeValueAsString(record);
                        HttpRequest r = HttpRequest.newBuilder(URI.create(url))
                            .timeout(Duration.ofSeconds(2))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(body))
                            .build();
                        httpClient.sendAsync(r, HttpResponse.BodyHandlers.discarding());
                    }} catch (Exception ignored) {{
                        // Audit must never break the request path.
                    }}
                }}
            }}
            MDC.remove("request_id");
        }}
    }}

    private static HttpServletRequest currentRequest() {{
        try {{
            ServletRequestAttributes attrs =
                (ServletRequestAttributes) RequestContextHolder.getRequestAttributes();
            return attrs != null ? attrs.getRequest() : null;
        }} catch (Exception e) {{
            return null;
        }}
    }}

    private static String clientIp(HttpServletRequest req) {{
        if (req == null) return "";
        String fwd = req.getHeader("X-Forwarded-For");
        if (fwd != null && !fwd.isEmpty()) return fwd.split(",")[0].trim();
        String addr = req.getRemoteAddr();
        return addr == null ? "" : addr;
    }}

    private static Set<String> csv(String s) {{
        Set<String> out = new HashSet<>();
        for (String p : s.split(",")) {{
            String t = p.trim();
            if (!t.isEmpty()) out.add(t);
        }}
        return out;
    }}

    private static Set<String> csvLower(String s) {{
        Set<String> out = new HashSet<>();
        for (String p : s.split(",")) {{
            String t = p.trim().toLowerCase();
            if (!t.isEmpty()) out.add(t);
        }}
        return out;
    }}

    private static List<Pattern> compilePatterns(String s) {{
        List<Pattern> out = new ArrayList<>();
        for (String p : s.split(",")) {{
            String t = p.trim();
            if (t.isEmpty()) continue;
            try {{ out.add(Pattern.compile(t)); }} catch (Exception ignored) {{}}
        }}
        return out;
    }}

    @SuppressWarnings("unchecked")
    private static Object scrub(Object o, Set<String> fields, List<Pattern> patterns, int maxLen) {{
        if (o == null) return null;
        if (o instanceof Map) {{
            Map<String, Object> in = (Map<String, Object>) o;
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : in.entrySet()) {{
                String k = String.valueOf(e.getKey());
                out.put(k, fields.contains(k.toLowerCase())
                    ? "****"
                    : scrub(e.getValue(), fields, patterns, maxLen));
            }}
            return out;
        }}
        if (o instanceof Collection) {{
            List<Object> out = new ArrayList<>();
            for (Object x : (Collection<?>) o) out.add(scrub(x, fields, patterns, maxLen));
            return out;
        }}
        if (o instanceof String) {{
            String s = (String) o;
            if (s.length() > maxLen) s = s.substring(0, maxLen);
            return scrubString(s, patterns);
        }}
        return o;
    }}

    private static String scrubString(String s, List<Pattern> patterns) {{
        if (s == null) return "";
        String out = s;
        for (Pattern p : patterns) out = p.matcher(out).replaceAll("****");
        return out;
    }}
}}
'''


# ---------------------------------------------------------------------------
# Node.js — Express RequestHandler
# ---------------------------------------------------------------------------

AUDIT_LOG_NODE = '''/**
 * LAMA-injected utility: {label} ({integration_id}).
 *
 * ELK-friendly audit-trail middleware for Express. Mount BEFORE your routers
 * (and AFTER express.json so req.body is populated):
 *
 *     import auditTrailMiddleware from "./integrations/{integration_id}/router";
 *     app.use(express.json());
 *     app.use(auditTrailMiddleware);
 *
 * Mode env: {gate_env} = off | mock | live.
 *  - off  : zero overhead, middleware short-circuits.
 *  - mock : log JSON to stdout (default).
 *  - live : log JSON + best-effort POST to AUDIT_TRAIL_LOGSTASH_URL.
 *
 * PII (field names + regexes) is masked with "****".
 */
import {{ Request, Response, NextFunction, RequestHandler }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

function env(k: string, def: string): string {{
  const v = process.env[k];
  return v === undefined || v === "" ? def : v;
}}

function csv(s: string): Set<string> {{
  const out = new Set<string>();
  for (const p of s.split(",")) {{
    const t = p.trim();
    if (t) out.add(t);
  }}
  return out;
}}

function csvLower(s: string): Set<string> {{
  const out = new Set<string>();
  for (const p of s.split(",")) {{
    const t = p.trim().toLowerCase();
    if (t) out.add(t);
  }}
  return out;
}}

function compilePatterns(s: string): RegExp[] {{
  const out: RegExp[] = [];
  for (const p of s.split(",")) {{
    const t = p.trim();
    if (!t) continue;
    try {{
      out.push(new RegExp(t, "g"));
    }} catch {{
      // skip invalid pattern
    }}
  }}
  return out;
}}

function scrubString(s: string, patterns: RegExp[]): string {{
  let out = s;
  for (const p of patterns) out = out.replace(p, "****");
  return out;
}}

function scrub(o: any, fields: Set<string>, patterns: RegExp[], maxLen: number): any {{
  if (o === null || o === undefined) return o;
  if (Array.isArray(o)) return o.map((x) => scrub(x, fields, patterns, maxLen));
  if (typeof o === "object") {{
    const out: Record<string, any> = {{}};
    for (const k of Object.keys(o)) {{
      out[k] = fields.has(k.toLowerCase()) ? "****" : scrub((o as any)[k], fields, patterns, maxLen);
    }}
    return out;
  }}
  if (typeof o === "string") {{
    const s = o.length > maxLen ? o.substring(0, maxLen) : o;
    return scrubString(s, patterns);
  }}
  return o;
}}

const DEFAULT_PII_FIELDS =
  "password,pwd,passwd,pan,aadhaar,aadhar,otp,token,authorization,api_key," +
  "apikey,secret,client_secret,ssn,email,mobile,phone,card,card_no,cvv,pin," +
  "session,cookie";

const DEFAULT_PII_PATTERNS =
  "[A-Z]{{5}}[0-9]{{4}}[A-Z]," +
  "\\\\b[2-9][0-9]{{11}}\\\\b," +
  "\\\\b[0-9]{{12,19}}\\\\b," +
  "[\\\\w.+-]+@[\\\\w-]+\\\\.[\\\\w.-]+";

export const auditTrailMiddleware: RequestHandler = (req: Request, res: Response, next: NextFunction) => {{
  const mode = env("{gate_env}", "mock").toLowerCase();
  if (mode === "off") return next();

  const excluded = csv(env("AUDIT_TRAIL_EXCLUDE_PATHS", "/health,/metrics"));
  if (excluded.has(req.path)) return next();

  const requestId = (req.header("x-request-id") || randomUUID()).substring(0, 36);
  const requestTime = new Date().toISOString();
  const startedNs = process.hrtime.bigint();
  const fwd = req.header("x-forwarded-for");
  const ip = fwd ? fwd.split(",")[0].trim() : (req.ip || req.socket?.remoteAddress || "");
  const captureBody = env("AUDIT_TRAIL_LOG_BODY", "0") === "1";
  const maxBytes = parseInt(env("AUDIT_TRAIL_MAX_BODY_BYTES", "2048"), 10) || 2048;
  const piiFields = csvLower(env("AUDIT_TRAIL_PII_FIELDS", DEFAULT_PII_FIELDS));
  const piiPatterns = compilePatterns(env("AUDIT_TRAIL_PII_PATTERNS", DEFAULT_PII_PATTERNS));

  let responseBody = "";
  if (captureBody) {{
    const origSend = res.send.bind(res);
    res.send = ((body?: any) => {{
      try {{
        const txt = typeof body === "string" ? body : JSON.stringify(body);
        responseBody = txt ? txt.substring(0, maxBytes) : "";
      }} catch {{
        responseBody = "";
      }}
      return origSend(body);
    }}) as any;
  }}

  res.on("finish", () => {{
    const responseTime = new Date().toISOString();
    const durationSec = Number(process.hrtime.bigint() - startedNs) / 1e9;
    const handlerPath = (req.route && (req.route.path as string)) || "";
    const record: Record<string, any> = {{
      "@timestamp": responseTime,
      service: env("AUDIT_TRAIL_SERVICE_NAME", "integrations-service"),
      request_id: requestId,
      ip: ip || "unknown",
      method: req.method,
      path: req.originalUrl || req.path,
      class: "ExpressRouter",
      handler: handlerPath,
      status: res.statusCode,
      request_time: requestTime,
      response_time: responseTime,
      duration_sec: durationSec,
    }};
    if (captureBody && req.body && Object.keys(req.body || {{}}).length > 0) {{
      record.request_body = scrub(req.body, piiFields, piiPatterns, maxBytes);
    }}
    if (captureBody && responseBody) {{
      try {{
        record.response_body = scrub(JSON.parse(responseBody), piiFields, piiPatterns, maxBytes);
      }} catch {{
        record.response_body = scrub(responseBody, piiFields, piiPatterns, maxBytes);
      }}
    }}

    const fmt = env("AUDIT_TRAIL_LOG_FORMAT", "json").toLowerCase();
    if (fmt === "text") {{
      // eslint-disable-next-line no-console
      console.log(
        `audit ip=${{record.ip}} method=${{record.method}} path=${{record.path}} ` +
          `status=${{record.status}} duration_sec=${{record.duration_sec}} ` +
          `request_id=${{record.request_id}}`,
      );
    }} else {{
      // eslint-disable-next-line no-console
      console.log(JSON.stringify(record));
    }}

    if (mode === "live") {{
      const url = env("AUDIT_TRAIL_LOGSTASH_URL", "");
      if (url) {{
        axios.post(url, record, {{ timeout: 2000 }}).catch(() => {{
          // Audit must never break the request path.
        }});
      }}
    }}
  }});

  next();
}};

export default auditTrailMiddleware;
'''


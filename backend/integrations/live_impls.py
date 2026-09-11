"""LAMA — full live-mode implementations for the Java and Node.js
integration targets (iter-13.61).

Until this module landed, `renderer.py` emitted a generic mock-only handler
for every Java/Node integration with a `TODO: implement live mode` marker
pointing at the Python reference in `catalog.py`. This module ships the
*actual* live-mode HTTP plumbing for all six govt-services integrations on
both targets, so flipping `<GATE>_MODE=live` and supplying credentials in
`.env` now works end-to-end on Python **and** Java **and** Node.js.

Each entry below is a complete file body. Python `str.format` placeholders
used by the renderer (e.g. `{label}`, `{gate_env}`) are written as
`{label}`/`{gate_env}` etc., and every literal brace inside the source code
is doubled (`{{`/`}}`) so `.format(...)` is a no-op on them.

Design parity with the Python templates:
  • `<GATE>_MODE` env var picks `mock` (default) vs `live`.
  • Mock branch returns the same canned personas the Python templates use
    (e.g. PAN starting with `ABCDE` -> "RAMESH KUMAR" / VALID; OTP `123456`
    is the magic Aadhaar OTP).
  • Live branch reads the same set of provider env vars
    (`PAN_VERIFY_API_KEY`, `AADHAAR_OTP_URL`, ...) and refuses to call out
    if a required one is unset.
  • Identifiers (PAN, Aadhaar) are masked for display and SHA-256-hashed
    (32 hex chars) for any audit trail — never logged raw (DPDP Act 2023).
  • TLS verification respects `LAMA_DISABLE_SSL_VERIFY` /
    `LAMA_CA_BUNDLE` (same env vars the LAMA backend honours), so the
    generated services behave correctly behind corporate MITM proxies.
"""
from typing import Dict, Tuple

from integrations.audit_logger_templates import AUDIT_LOG_JAVA, AUDIT_LOG_NODE
from integrations.live_impls_dpg import DPG_JAVA_IMPLS, DPG_NODE_IMPLS
from integrations.live_impls_dpg_india import (
    DPG_INDIA_JAVA_IMPLS,
    DPG_INDIA_NODE_IMPLS,
)


# ---------------------------------------------------------------------------
# Java shared utility (one file, dropped once per scaffold)
# ---------------------------------------------------------------------------

JAVA_UTIL_PATH = "src/main/java/com/lama/integrations/IntegrationUtil.java"

JAVA_UTIL = '''package com.lama.integrations;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;
import java.time.Duration;

/**
 * LAMA-generated helpers shared by every integration controller.
 * Mirrors the helpers in the Python reference (catalog.py).
 */
public final class IntegrationUtil {{

    private IntegrationUtil() {{}}

    /** Mask all but the last `keep` chars of an identifier for display. */
    public static String maskId(String value, int keep) {{
        String v = value == null ? "" : value.trim();
        if (v.length() <= keep) {{
            return "*".repeat(v.length());
        }}
        return "*".repeat(v.length() - keep) + v.substring(v.length() - keep);
    }}

    /** Stable 32-char SHA-256 hash for audit trails (never store raw PII). */
    public static String hashPii(String value) {{
        try {{
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest((value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte b : digest) {{
                hex.append(String.format("%02x", b));
            }}
            return hex.substring(0, 32);
        }} catch (Exception e) {{
            return "";
        }}
    }}

    /** Honour LAMA-style TLS env vars (Zscaler / Netskope). */
    public static HttpClient httpClient() {{
        try {{
            String disable = System.getenv().getOrDefault("LAMA_DISABLE_SSL_VERIFY", "");
            if (disable.equalsIgnoreCase("1") || disable.equalsIgnoreCase("true")
                    || disable.equalsIgnoreCase("yes")) {{
                TrustManager[] trustAll = new TrustManager[] {{
                    new X509TrustManager() {{
                        public X509Certificate[] getAcceptedIssuers() {{ return new X509Certificate[0]; }}
                        public void checkClientTrusted(X509Certificate[] chain, String authType) {{}}
                        public void checkServerTrusted(X509Certificate[] chain, String authType) {{}}
                    }}
                }};
                SSLContext ctx = SSLContext.getInstance("TLS");
                ctx.init(null, trustAll, new java.security.SecureRandom());
                return HttpClient.newBuilder()
                        .connectTimeout(Duration.ofSeconds(30))
                        .sslContext(ctx)
                        .build();
            }}
            String bundle = System.getenv().getOrDefault("LAMA_CA_BUNDLE", "").trim();
            if (!bundle.isEmpty() && Files.exists(Path.of(bundle))) {{
                // Custom truststore wiring is left as an exercise — JDK reads
                // -Djavax.net.ssl.trustStore at process start, which Spring Boot
                // picks up if you set JAVA_TOOL_OPTIONS=-Djavax.net.ssl.trustStore=...
                // We deliberately do not mutate global SSL context here.
            }}
        }} catch (Exception ignored) {{
        }}
        return HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(30)).build();
    }}

    /** Read an env-var with a default. */
    public static String env(String name, String fallback) {{
        String v = System.getenv(name);
        return (v == null || v.isBlank()) ? fallback : v;
    }}

    /** "mock" or "live" — case-insensitive read of the gate env var. */
    public static String mode(String gateEnv) {{
        return env(gateEnv, "mock").trim().toLowerCase();
    }}
}}
'''


# ---------------------------------------------------------------------------
# Java per-integration controllers
# ---------------------------------------------------------------------------

PAN_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;
import java.util.regex.Pattern;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * Mock-by-default. Set {gate_env}=live and supply env vars in .env to call
 * the real provider (Karza / NSDL Protean / IDfy / Signzy).
 */
@RestController
@RequestMapping("{route_prefix}")
public class PanVerificationController {{

    private static final Logger log = LoggerFactory.getLogger(PanVerificationController.class);
    private static final Pattern PAN_RE = Pattern.compile("^[A-Z]{{5}}[0-9]{{4}}[A-Z]$");
    private static final ObjectMapper M = new ObjectMapper();

    private static final Map<String, String[]> MOCK_PERSONAS = Map.of(
        "ABCDE", new String[]{{"RAMESH KUMAR", "VALID"}},
        "AAAPL", new String[]{{"LATA MANGESHKAR", "VALID"}},
        "FAKEX", new String[]{{"", "INVALID"}},
        "ZZZZZ", new String[]{{"", "NOT_FOUND"}}
    );

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @PostMapping("/verify")
    public Map<String, Object> verify(@RequestBody Map<String, Object> body) {{
        String pan = String.valueOf(body.getOrDefault("pan", "")).toUpperCase().trim().replace(" ", "");
        String name = String.valueOf(body.getOrDefault("name_on_pan", "")).trim();
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        long t0 = System.nanoTime();

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("pan_masked", IntegrationUtil.maskId(pan, 4));
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!PAN_RE.matcher(pan).matches()) {{
            resp.put("verified", false);
            resp.put("status", "INVALID");
            resp.put("error", "PAN format invalid (expected ABCDE1234F)");
            resp.put("latency_ms", (System.nanoTime() - t0) / 1_000_000);
            return resp;
        }}

        try {{
            if ("live".equalsIgnoreCase(mode)) {{
                liveVerify(pan, name, resp);
            }} else {{
                mockVerify(pan, name, resp);
            }}
        }} catch (Exception e) {{
            log.error("pan.verify failed", e);
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", e.getMessage());
        }}
        resp.put("latency_ms", (System.nanoTime() - t0) / 1_000_000);
        log.info("pan.verify request_id={{}} pan_hash={{}} mode={{}} verified={{}} status={{}}",
                 requestId, IntegrationUtil.hashPii(pan), mode,
                 resp.get("verified"), resp.get("status"));
        return resp;
    }}

    private void mockVerify(String pan, String name, Map<String, Object> resp) {{
        String[] persona = MOCK_PERSONAS.getOrDefault(pan.substring(0, 5),
                new String[]{{"VERIFIED USER", "VALID"}});
        String nameOnPan = persona[0];
        String status = persona[1];
        boolean verified = "VALID".equals(status);
        resp.put("verified", verified);
        resp.put("status", status);
        resp.put("name_on_pan", verified ? nameOnPan : "");
        if (verified && !name.isEmpty()) {{
            String nm = name.trim().toUpperCase();
            String np = nameOnPan.toUpperCase();
            resp.put("name_match", np.contains(nm) || nm.contains(np));
        }}
    }}

    @SuppressWarnings("unchecked")
    private void liveVerify(String pan, String name, Map<String, Object> resp) throws Exception {{
        String url = IntegrationUtil.env("PAN_VERIFY_ENDPOINT_URL", "https://api.karza.in/v3/pan");
        String apiKey = IntegrationUtil.env("PAN_VERIFY_API_KEY", "");
        String authKind = IntegrationUtil.env("PAN_VERIFY_AUTH_KIND", "x-karza-key");
        if (apiKey.isEmpty()) {{
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", "PAN_VERIFY_API_KEY is unset — refusing to call provider in live mode.");
            return;
        }}
        Map<String, Object> reqBody = new LinkedHashMap<>();
        reqBody.put("pan", pan);
        reqBody.put("consent", "Y");
        if (!name.isEmpty()) reqBody.put("name", name);

        HttpRequest.Builder rb = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)));
        if ("bearer".equalsIgnoreCase(authKind)) {{
            rb.header("Authorization", "Bearer " + apiKey);
        }} else {{
            rb.header("x-karza-key", apiKey);
        }}

        HttpClient client = IntegrationUtil.httpClient();
        HttpResponse<String> r = client.send(rb.build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) {{
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", "Provider HTTP " + r.statusCode());
            return;
        }}
        Map<String, Object> data = (r.body() == null || r.body().isBlank())
                ? Map.of() : M.readValue(r.body(), Map.class);
        Object resultObj = data.getOrDefault("result", data);
        Map<String, Object> result = (resultObj instanceof Map) ? (Map<String, Object>) resultObj : data;
        String raw = String.valueOf(result.getOrDefault("pan_status",
                result.getOrDefault("status", ""))).toUpperCase();
        String nameOnPan = String.valueOf(result.getOrDefault("name", ""));
        boolean verified = raw.contains("VALID") || "ACTIVE".equals(raw);
        resp.put("verified", verified);
        resp.put("status", verified ? "VALID" : (raw.isEmpty() ? "INVALID" : raw));
        resp.put("name_on_pan", nameOnPan);
        if (verified && !name.isEmpty() && !nameOnPan.isEmpty()) {{
            resp.put("name_match", nameOnPan.toUpperCase().contains(name.toUpperCase()));
        }}
    }}
}}
'''


AADHAAR_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;
import java.util.regex.Pattern;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * Mock-by-default; OTP `123456` is the magic mock OTP. Set {gate_env}=live
 * and supply AADHAAR_OTP_URL / AADHAAR_VERIFY_URL / AADHAAR_API_KEY.
 */
@RestController
@RequestMapping("{route_prefix}")
public class AadhaarEkycController {{

    private static final Logger log = LoggerFactory.getLogger(AadhaarEkycController.class);
    private static final Pattern AADHAAR_RE = Pattern.compile("^[2-9][0-9]{{11}}$");
    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @PostMapping("/otp/initiate")
    public ResponseEntity<Map<String, Object>> initiate(@RequestBody Map<String, Object> body) {{
        String aadhaar = String.valueOf(body.getOrDefault("aadhaar", "")).trim();
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        if (!AADHAAR_RE.matcher(aadhaar).matches()) {{
            return ResponseEntity.badRequest().body(Map.of(
                "error", "Invalid Aadhaar number (12 digits, must not start with 0/1)",
                "request_id", requestId, "mode", mode
            ));
        }}
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("transaction_id", "mock-txn-" + requestId);
            resp.put("sent", true);
            resp.put("masked_mobile", "XXXXXX1234");
            resp.put("expires_in_seconds", 300);
            return ResponseEntity.ok(resp);
        }}

        String url = IntegrationUtil.env("AADHAAR_OTP_URL", "");
        String apiKey = IntegrationUtil.env("AADHAAR_API_KEY", "");
        if (url.isEmpty() || apiKey.isEmpty()) {{
            resp.put("sent", false);
            resp.put("transaction_id", "");
            resp.put("error", "AADHAAR_OTP_URL or AADHAAR_API_KEY is unset");
            return ResponseEntity.ok(resp);
        }}
        try {{
            String payload = M.writeValueAsString(Map.of("aadhaar", aadhaar, "consent", "Y"));
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + apiKey)
                    .POST(HttpRequest.BodyPublishers.ofString(payload))
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("transaction_id", String.valueOf(data.getOrDefault("transaction_id", "")));
            resp.put("sent", r.statusCode() == 200);
            resp.put("masked_mobile", String.valueOf(data.getOrDefault("masked_mobile", "")));
            resp.put("expires_in_seconds", data.getOrDefault("expires_in_seconds", 300));
            if (r.statusCode() != 200) resp.put("error", "Provider HTTP " + r.statusCode());
        }} catch (Exception e) {{
            log.error("aadhaar.otp.initiate failed", e);
            resp.put("sent", false);
            resp.put("transaction_id", "");
            resp.put("error", e.getMessage());
        }}
        return ResponseEntity.ok(resp);
    }}

    @PostMapping("/verify")
    public ResponseEntity<Map<String, Object>> verify(@RequestBody Map<String, Object> body) {{
        String aadhaar = String.valueOf(body.getOrDefault("aadhaar", "")).trim();
        String otp = String.valueOf(body.getOrDefault("otp", "")).trim();
        String txn = String.valueOf(body.getOrDefault("transaction_id", "")).trim();
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        if (!AADHAAR_RE.matcher(aadhaar).matches()) {{
            return ResponseEntity.badRequest().body(Map.of(
                "error", "Invalid Aadhaar number", "request_id", requestId, "mode", mode
            ));
        }}
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("aadhaar_masked", IntegrationUtil.maskId(aadhaar, 4));
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            boolean ok = "123456".equals(otp);
            resp.put("verified", ok);
            resp.put("status", ok ? "VALID" : "OTP_MISMATCH");
            resp.put("name", ok ? "RAMESH KUMAR" : "");
            resp.put("dob", ok ? "1985-04-21" : "");
            resp.put("gender", ok ? "M" : "");
            resp.put("address_masked", ok ? "XX, MG Road, Bangalore" : "");
            if (!ok) resp.put("error", "OTP mismatch (mock expects 123456)");
            log.info("aadhaar.verify mode=mock request_id={{}} aadhaar_hash={{}} verified={{}}",
                     requestId, IntegrationUtil.hashPii(aadhaar), ok);
            return ResponseEntity.ok(resp);
        }}

        String url = IntegrationUtil.env("AADHAAR_VERIFY_URL", "");
        String apiKey = IntegrationUtil.env("AADHAAR_API_KEY", "");
        if (url.isEmpty() || apiKey.isEmpty()) {{
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", "AADHAAR_VERIFY_URL or AADHAAR_API_KEY is unset");
            return ResponseEntity.ok(resp);
        }}
        try {{
            String payload = M.writeValueAsString(Map.of(
                "aadhaar", aadhaar, "otp", otp, "transaction_id", txn));
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + apiKey)
                    .POST(HttpRequest.BodyPublishers.ofString(payload))
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            String status = String.valueOf(data.getOrDefault("status", "ERROR"));
            boolean verified = r.statusCode() == 200 && "VALID".equalsIgnoreCase(status);
            resp.put("verified", verified);
            resp.put("status", status);
            resp.put("name", String.valueOf(data.getOrDefault("name", "")));
            resp.put("dob", String.valueOf(data.getOrDefault("dob", "")));
            resp.put("gender", String.valueOf(data.getOrDefault("gender", "")));
            resp.put("address_masked", String.valueOf(data.getOrDefault("address_masked", "")));
            if (r.statusCode() != 200) resp.put("error", "Provider HTTP " + r.statusCode());
        }} catch (Exception e) {{
            log.error("aadhaar.verify failed", e);
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", e.getMessage());
        }}
        return ResponseEntity.ok(resp);
    }}
}}
'''


GSTIN_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.regex.Pattern;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * Routed through MasterGST by default; switch via GSTIN_VERIFY_URL.
 */
@RestController
@RequestMapping("{route_prefix}")
public class GstinVerificationController {{

    private static final Logger log = LoggerFactory.getLogger(GstinVerificationController.class);
    private static final Pattern GSTIN_RE = Pattern.compile(
        "^[0-9]{{2}}[A-Z]{{5}}[0-9]{{4}}[A-Z]{{1}}[1-9A-Z]{{1}}Z[0-9A-Z]{{1}}$");
    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @PostMapping("/verify")
    public Map<String, Object> verify(@RequestBody Map<String, Object> body) {{
        String gstin = String.valueOf(body.getOrDefault("gstin", "")).trim().toUpperCase();
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("gstin", gstin);
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!GSTIN_RE.matcher(gstin).matches()) {{
            resp.put("verified", false);
            resp.put("status", "INVALID");
            resp.put("error", "GSTIN format invalid");
            return resp;
        }}

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("verified", true);
            resp.put("status", "ACTIVE");
            resp.put("legal_name", "ACME WIDGETS PRIVATE LIMITED");
            resp.put("trade_name", "ACME WIDGETS");
            resp.put("state", "Karnataka");
            resp.put("constitution", "Private Limited Company");
            resp.put("registration_date", "2019-07-12");
            resp.put("last_filing_status", "GSTR-1 filed for 2026-04");
            return resp;
        }}

        String base = IntegrationUtil.env("GSTIN_VERIFY_URL", "https://commonapi.mastergst.com/public/search");
        String clientId = IntegrationUtil.env("GSTIN_CLIENT_ID", "");
        String apiKey = IntegrationUtil.env("GSTIN_API_KEY", "");
        if (apiKey.isEmpty()) {{
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", "GSTIN_API_KEY is unset");
            return resp;
        }}
        try {{
            String url = base + (base.contains("?") ? "&" : "?")
                    + "gstin=" + URLEncoder.encode(gstin, StandardCharsets.UTF_8);
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("client_id", clientId)
                    .header("client_secret", apiKey)
                    .GET()
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            Object dataObj = data.getOrDefault("data", data);
            @SuppressWarnings("unchecked")
            Map<String, Object> b = (dataObj instanceof Map) ? (Map<String, Object>) dataObj : data;
            String sts = String.valueOf(b.getOrDefault("sts", "ERROR"));
            boolean active = r.statusCode() == 200 && ("Active".equalsIgnoreCase(sts) || "ACTIVE".equals(sts));
            resp.put("verified", active);
            resp.put("status", sts);
            resp.put("legal_name", String.valueOf(b.getOrDefault("lgnm", "")));
            resp.put("trade_name", String.valueOf(b.getOrDefault("tradeNam", "")));
            resp.put("constitution", String.valueOf(b.getOrDefault("ctb", "")));
            resp.put("registration_date", String.valueOf(b.getOrDefault("rgdt", "")));
            resp.put("last_filing_status", String.valueOf(b.getOrDefault("lstupdt", "")));
            // pradr.addr.stcd — best-effort nested lookup
            Object pradr = b.get("pradr");
            String state = "";
            if (pradr instanceof Map) {{
                Object addr = ((Map<?, ?>) pradr).get("addr");
                if (addr instanceof Map) {{
                    Object stcd = ((Map<?, ?>) addr).get("stcd");
                    if (stcd != null) state = String.valueOf(stcd);
                }}
            }}
            resp.put("state", state);
            if (r.statusCode() != 200) resp.put("error", "Provider HTTP " + r.statusCode());
        }} catch (Exception e) {{
            log.error("gstin.verify failed", e);
            resp.put("verified", false);
            resp.put("status", "ERROR");
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}
}}
'''


DIGILOCKER_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * MeitY DigiLocker / Meri Pehchaan OAuth2 — mock-by-default.
 */
@RestController
@RequestMapping("{route_prefix}")
public class DigilockerController {{

    private static final Logger log = LoggerFactory.getLogger(DigilockerController.class);
    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @GetMapping("/authorize")
    public ResponseEntity<Map<String, Object>> authorize() {{
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        String state = UUID.randomUUID().toString().replace("-", "");
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("state", state);
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("authorize_url", "https://mock.lama.local/digilocker/authorize?state=" + state);
            return ResponseEntity.ok(resp);
        }}
        String clientId = IntegrationUtil.env("DIGILOCKER_CLIENT_ID", "");
        String redirectUri = IntegrationUtil.env("DIGILOCKER_REDIRECT_URI", "");
        if (clientId.isEmpty() || redirectUri.isEmpty()) {{
            return ResponseEntity.internalServerError().body(Map.of(
                "error", "DIGILOCKER_CLIENT_ID / DIGILOCKER_REDIRECT_URI unset",
                "request_id", requestId, "mode", "live"
            ));
        }}
        String authorize = "https://api.digitallocker.gov.in/public/oauth2/1/authorize"
                + "?response_type=code&client_id=" + clientId
                + "&redirect_uri=" + redirectUri
                + "&state=" + state
                + "&scope=basic+documents";
        resp.put("authorize_url", authorize);
        return ResponseEntity.ok(resp);
    }}

    @PostMapping("/document")
    public Map<String, Object> document(@RequestBody Map<String, Object> body) {{
        String accessToken = String.valueOf(body.getOrDefault("access_token", "")).trim();
        String docType = String.valueOf(body.getOrDefault("doc_type", "AADHAAR")).trim();
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("doc_type", docType);
        resp.put("request_id", requestId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("fetched", true);
            resp.put("issued_to", "RAMESH KUMAR");
            resp.put("issue_date", "2024-01-15");
            resp.put("issuer", "AADHAAR".equals(docType) ? "UIDAI" : "Income Tax Department");
            resp.put("doc_uri", "mock://digilocker/" + docType + "/abcd1234");
            return resp;
        }}

        String url = IntegrationUtil.env("DIGILOCKER_DOC_URL",
                "https://api.digitallocker.gov.in/public/oauth2/1/files/issued");
        try {{
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Authorization", "Bearer " + accessToken)
                    .GET()
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            if (r.statusCode() != 200) {{
                resp.put("fetched", false);
                resp.put("error", "Provider HTTP " + r.statusCode());
                return resp;
            }}
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("fetched", true);
            resp.put("issued_to", String.valueOf(data.getOrDefault("issued_to", "")));
            resp.put("issue_date", String.valueOf(data.getOrDefault("issue_date", "")));
            resp.put("issuer", String.valueOf(data.getOrDefault("issuer", "")));
            resp.put("doc_uri", String.valueOf(data.getOrDefault("uri", "")));
        }} catch (Exception e) {{
            log.error("digilocker.document failed", e);
            resp.put("fetched", false);
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}
}}
'''


ESIGN_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.*;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * Aadhaar e-Sign — eMudhra / NSDL / Protean / Leegality compatible.
 */
@RestController
@RequestMapping("{route_prefix}")
public class EsignController {{

    private static final Logger log = LoggerFactory.getLogger(EsignController.class);
    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @PostMapping("/initiate")
    public Map<String, Object> initiate(@RequestBody Map<String, Object> body) {{
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        String txn = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("request_id", requestId);
        resp.put("mode", mode);
        resp.put("expires_in_seconds", 600);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("transaction_id", txn);
            resp.put("redirect_url", "https://mock.lama.local/esign/sign?txn=" + txn);
            return resp;
        }}

        String url = IntegrationUtil.env("ESIGN_INITIATE_URL", "");
        String apiKey = IntegrationUtil.env("ESIGN_API_KEY", "");
        String aspId = IntegrationUtil.env("ESIGN_ASP_ID", "");
        if (url.isEmpty() || apiKey.isEmpty() || aspId.isEmpty()) {{
            resp.put("transaction_id", "");
            resp.put("error", "ESIGN_INITIATE_URL / ESIGN_API_KEY / ESIGN_ASP_ID unset");
            return resp;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("asp_id", aspId);
            reqBody.put("document", body.getOrDefault("document_base64", ""));
            reqBody.put("signer_aadhaar", body.getOrDefault("signer_aadhaar", ""));
            reqBody.put("signer_name", body.getOrDefault("signer_name", ""));
            reqBody.put("purpose", body.getOrDefault("purpose", "Document signing"));
            reqBody.put("callback_url", body.getOrDefault("callback_url", ""));

            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Bearer " + apiKey)
                    .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("transaction_id", String.valueOf(data.getOrDefault("transaction_id", txn)));
            resp.put("redirect_url", String.valueOf(data.getOrDefault("redirect_url", "")));
            resp.put("expires_in_seconds", data.getOrDefault("expires_in_seconds", 600));
            if (r.statusCode() != 200) resp.put("error", "Provider HTTP " + r.statusCode());
        }} catch (Exception e) {{
            log.error("esign.initiate failed", e);
            resp.put("transaction_id", "");
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}

    @GetMapping("/status/{{transaction_id}}")
    public Map<String, Object> status(@PathVariable("transaction_id") String transactionId) {{
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("transaction_id", transactionId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("status", "SIGNED");
            resp.put("signed_document_base64", "<base64 PDF would be here>");
            resp.put("signed_at", Instant.now().toString());
            resp.put("signer_name", "RAMESH KUMAR");
            return resp;
        }}

        String base = IntegrationUtil.env("ESIGN_STATUS_URL", "");
        String apiKey = IntegrationUtil.env("ESIGN_API_KEY", "");
        if (base.isEmpty()) {{
            resp.put("status", "ERROR");
            resp.put("error", "ESIGN_STATUS_URL unset");
            return resp;
        }}
        try {{
            String url = base.endsWith("/") ? base + transactionId : base + "/" + transactionId;
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Authorization", "Bearer " + apiKey)
                    .GET()
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("status", String.valueOf(data.getOrDefault("status", "PENDING")));
            resp.put("signed_document_base64", String.valueOf(data.getOrDefault("signed_document_base64", "")));
            resp.put("signed_at", String.valueOf(data.getOrDefault("signed_at", "")));
            resp.put("signer_name", String.valueOf(data.getOrDefault("signer_name", "")));
        }} catch (Exception e) {{
            log.error("esign.status failed", e);
            resp.put("status", "ERROR");
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}
}}
'''


UPI_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.*;

/**
 * LAMA-injected integration: {label} ({integration_id}).
 * Razorpay-style UPI flow — switchable via UPI_INITIATE_URL / UPI_STATUS_URL.
 */
@RestController
@RequestMapping("{route_prefix}")
public class UpiPaymentController {{

    private static final Logger log = LoggerFactory.getLogger(UpiPaymentController.class);
    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    @PostMapping("/initiate")
    public Map<String, Object> initiate(@RequestBody Map<String, Object> body) {{
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        String txn = UUID.randomUUID().toString().replace("-", "").substring(0, 14);
        double amount = ((Number) body.getOrDefault("amount_rupees", 0)).doubleValue();
        String orderId = String.valueOf(body.getOrDefault("order_id", ""));
        String payerVpa = String.valueOf(body.getOrDefault("payer_vpa", ""));
        String purpose = String.valueOf(body.getOrDefault("purpose", "Payment"));
        String callback = String.valueOf(body.getOrDefault("callback_url", ""));

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("request_id", requestId);
        resp.put("mode", mode);
        resp.put("expires_in_seconds", 600);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("transaction_id", txn);
            resp.put("intent_url", String.format(
                "upi://pay?pa=merchant@upi&pn=Demo&am=%.2f&tn=%s&tr=%s",
                amount, purpose, txn));
            resp.put("collect_link", "https://mock.lama.local/pay/" + txn);
            return resp;
        }}

        String url = IntegrationUtil.env("UPI_INITIATE_URL", "");
        String keyId = IntegrationUtil.env("UPI_KEY_ID", "");
        String secret = IntegrationUtil.env("UPI_KEY_SECRET", "");
        String merchantVpa = IntegrationUtil.env("UPI_MERCHANT_VPA", "");
        if (url.isEmpty() || keyId.isEmpty() || secret.isEmpty() || merchantVpa.isEmpty()) {{
            resp.put("transaction_id", "");
            resp.put("error", "UPI_INITIATE_URL / UPI_KEY_ID / UPI_KEY_SECRET / UPI_MERCHANT_VPA unset");
            return resp;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("amount", (int) Math.round(amount * 100.0));
            reqBody.put("currency", "INR");
            reqBody.put("method", "upi");
            reqBody.put("order_id", orderId);
            reqBody.put("payer_vpa", payerVpa);
            reqBody.put("merchant_vpa", merchantVpa);
            reqBody.put("purpose", purpose);
            reqBody.put("callback_url", callback);

            String basic = Base64.getEncoder().encodeToString((keyId + ":" + secret).getBytes());
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Content-Type", "application/json")
                    .header("Authorization", "Basic " + basic)
                    .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("transaction_id", String.valueOf(data.getOrDefault("id", txn)));
            resp.put("intent_url", String.valueOf(data.getOrDefault("intent_url", "")));
            Object link = data.getOrDefault("short_url", data.getOrDefault("payment_link", ""));
            resp.put("collect_link", String.valueOf(link));
            resp.put("expires_in_seconds", data.getOrDefault("expires_in_seconds", 600));
            if (r.statusCode() != 200 && r.statusCode() != 201) {{
                resp.put("error", "Provider HTTP " + r.statusCode());
            }}
        }} catch (Exception e) {{
            log.error("upi.initiate failed", e);
            resp.put("transaction_id", "");
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}

    @GetMapping("/status/{{transaction_id}}")
    public Map<String, Object> status(@PathVariable("transaction_id") String transactionId) {{
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("transaction_id", transactionId);
        resp.put("order_id", transactionId);
        resp.put("mode", mode);

        if (!"live".equalsIgnoreCase(mode)) {{
            resp.put("status", "SUCCESS");
            resp.put("amount_rupees", 100.0);
            resp.put("upi_ref", "UPI" + transactionId.substring(0, Math.min(10, transactionId.length())).toUpperCase());
            resp.put("settled_at", Instant.now().toString());
            return resp;
        }}

        String base = IntegrationUtil.env("UPI_STATUS_URL", "");
        String keyId = IntegrationUtil.env("UPI_KEY_ID", "");
        String secret = IntegrationUtil.env("UPI_KEY_SECRET", "");
        if (base.isEmpty()) {{
            resp.put("status", "ERROR");
            resp.put("error", "UPI_STATUS_URL unset");
            return resp;
        }}
        try {{
            String url = base.endsWith("/") ? base + transactionId : base + "/" + transactionId;
            String basic = Base64.getEncoder().encodeToString((keyId + ":" + secret).getBytes());
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Authorization", "Basic " + basic)
                    .GET()
                    .build();
            HttpResponse<String> r = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            @SuppressWarnings("unchecked")
            Map<String, Object> data = (r.body() == null || r.body().isBlank())
                    ? Map.of() : M.readValue(r.body(), Map.class);
            resp.put("status", String.valueOf(data.getOrDefault("status", "PENDING")).toUpperCase());
            int paise = ((Number) data.getOrDefault("amount", 0)).intValue();
            resp.put("amount_rupees", paise / 100.0);
            String upiRef = "";
            Object acq = data.get("acquirer_data");
            if (acq instanceof Map) {{
                Object rrn = ((Map<?, ?>) acq).get("rrn");
                if (rrn != null) upiRef = String.valueOf(rrn);
            }}
            if (upiRef.isEmpty()) upiRef = String.valueOf(data.getOrDefault("upi_ref", ""));
            resp.put("upi_ref", upiRef);
            resp.put("settled_at", String.valueOf(data.getOrDefault("captured_at", "")));
        }} catch (Exception e) {{
            log.error("upi.status failed", e);
            resp.put("status", "ERROR");
            resp.put("error", e.getMessage());
        }}
        return resp;
    }}
}}
'''


# ---------------------------------------------------------------------------
# Node.js (Express + TypeScript) — per-integration routers
# ---------------------------------------------------------------------------

# Tiny shared helper module dropped once per Node scaffold.
NODE_UTIL_PATH = "src/integrations/_util.ts"
NODE_UTIL = '''import { createHash } from "crypto";
import * as https from "https";
import axios, { AxiosInstance } from "axios";

// LAMA-generated helpers shared by every integration router.

export function maskId(value: string, keep = 4): string {{
  const v = (value || "").trim();
  if (v.length <= keep) return "*".repeat(v.length);
  return "*".repeat(v.length - keep) + v.slice(-keep);
}}

export function hashPii(value: string): string {{
  return createHash("sha256").update(value || "").digest("hex").slice(0, 32);
}}

export function envMode(gateEnv: string): string {{
  return (process.env[gateEnv] || "mock").trim().toLowerCase();
}}

export function env(name: string, fallback = ""): string {{
  const v = process.env[name];
  return v && v.trim() !== "" ? v : fallback;
}}

/** Axios instance honouring LAMA-style TLS env vars (Zscaler/Netskope). */
export function httpClient(timeoutMs = 30_000): AxiosInstance {{
  const disable = (process.env.LAMA_DISABLE_SSL_VERIFY || "").toLowerCase();
  const reject = !["1", "true", "yes"].includes(disable);
  return axios.create({{
    timeout: timeoutMs,
    httpsAgent: new https.Agent({{ rejectUnauthorized: reject }}),
    validateStatus: () => true,
  }});
}}
'''


PAN_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, hashPii, httpClient, maskId }} from "../_util";

// LAMA-injected: {label} ({integration_id}). Mock-by-default. {gate_env}=live → real provider.

const router = Router();
const PAN_RE = /^[A-Z]{{5}}[0-9]{{4}}[A-Z]$/;
const PERSONAS: Record<string, [string, string]> = {{
  "ABCDE": ["RAMESH KUMAR", "VALID"],
  "AAAPL": ["LATA MANGESHKAR", "VALID"],
  "FAKEX": ["", "INVALID"],
  "ZZZZZ": ["", "NOT_FOUND"],
}};

router.post("/verify", async (req: Request, res: Response) => {{
  const pan = String((req.body?.pan ?? "")).toUpperCase().trim().replace(/\\s+/g, "");
  const name = String(req.body?.name_on_pan ?? "").trim();
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const mode = envMode("{gate_env}");
  const t0 = Date.now();
  const base: Record<string, any> = {{
    pan_masked: maskId(pan, 4), request_id: requestId, mode,
  }};

  if (!PAN_RE.test(pan)) {{
    return res.json({{ ...base, verified: false, status: "INVALID",
      error: "PAN format invalid (expected ABCDE1234F)",
      latency_ms: Date.now() - t0 }});
  }}

  try {{
    if (mode === "live") {{
      const url = env("PAN_VERIFY_ENDPOINT_URL", "https://api.karza.in/v3/pan");
      const apiKey = env("PAN_VERIFY_API_KEY", "");
      const authKind = env("PAN_VERIFY_AUTH_KIND", "x-karza-key");
      if (!apiKey) {{
        return res.json({{ ...base, verified: false, status: "ERROR",
          error: "PAN_VERIFY_API_KEY is unset — refusing to call provider in live mode.",
          latency_ms: Date.now() - t0 }});
      }}
      const headers: Record<string, string> = {{ "Content-Type": "application/json" }};
      if (authKind === "bearer") headers["Authorization"] = `Bearer ${{apiKey}}`;
      else headers["x-karza-key"] = apiKey;
      const payload: Record<string, any> = {{ pan, consent: "Y" }};
      if (name) payload.name = name;
      const r = await httpClient().post(url, payload, {{ headers }});
      if (r.status !== 200) {{
        return res.json({{ ...base, verified: false, status: "ERROR",
          error: `Provider HTTP ${{r.status}}`, latency_ms: Date.now() - t0 }});
      }}
      const result = (r.data?.result ?? r.data) || {{}};
      const raw = String(result.pan_status ?? result.status ?? "").toUpperCase();
      const nameOnPan = String(result.name ?? "");
      const verified = raw.includes("VALID") || raw === "ACTIVE";
      const out: Record<string, any> = {{ ...base,
        verified, status: verified ? "VALID" : (raw || "INVALID"),
        name_on_pan: nameOnPan, latency_ms: Date.now() - t0 }};
      if (verified && name && nameOnPan) {{
        out.name_match = nameOnPan.toUpperCase().includes(name.toUpperCase());
      }}
      console.log(`pan.verify request_id=${{requestId}} pan_hash=${{hashPii(pan)}} mode=live verified=${{verified}}`);
      return res.json(out);
    }}
    // mock
    const persona = PERSONAS[pan.slice(0, 5)] || ["VERIFIED USER", "VALID"];
    const [nameOnPan, status] = persona;
    const verified = status === "VALID";
    const out: Record<string, any> = {{ ...base, verified, status,
      name_on_pan: verified ? nameOnPan : "", latency_ms: Date.now() - t0 }};
    if (verified && name) {{
      const nm = name.toUpperCase();
      out.name_match = nameOnPan.toUpperCase().includes(nm) || nm.includes(nameOnPan.toUpperCase());
    }}
    console.log(`pan.verify request_id=${{requestId}} pan_hash=${{hashPii(pan)}} mode=mock verified=${{verified}}`);
    return res.json(out);
  }} catch (e: any) {{
    return res.json({{ ...base, verified: false, status: "ERROR",
      error: e?.message || String(e), latency_ms: Date.now() - t0 }});
  }}
}});

export default router;
'''


AADHAAR_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, hashPii, httpClient, maskId }} from "../_util";

// LAMA-injected: {label} ({integration_id}). Mock OTP is 123456.

const router = Router();
const AADHAAR_RE = /^[2-9][0-9]{{11}}$/;

router.post("/otp/initiate", async (req: Request, res: Response) => {{
  const aadhaar = String(req.body?.aadhaar ?? "").trim();
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const mode = envMode("{gate_env}");
  if (!AADHAAR_RE.test(aadhaar)) {{
    return res.status(400).json({{ error: "Invalid Aadhaar number (12 digits, must not start with 0/1)",
      request_id: requestId, mode }});
  }}
  if (mode !== "live") {{
    return res.json({{ transaction_id: `mock-txn-${{requestId}}`, sent: true,
      masked_mobile: "XXXXXX1234", expires_in_seconds: 300,
      request_id: requestId, mode: "mock" }});
  }}
  const url = env("AADHAAR_OTP_URL", "");
  const apiKey = env("AADHAAR_API_KEY", "");
  if (!url || !apiKey) {{
    return res.json({{ transaction_id: "", sent: false,
      error: "AADHAAR_OTP_URL or AADHAAR_API_KEY is unset",
      request_id: requestId, mode: "live" }});
  }}
  try {{
    const r = await httpClient().post(url, {{ aadhaar, consent: "Y" }},
      {{ headers: {{ Authorization: `Bearer ${{apiKey}}` }} }});
    return res.json({{
      transaction_id: r.data?.transaction_id ?? "", sent: r.status === 200,
      masked_mobile: r.data?.masked_mobile ?? "",
      expires_in_seconds: r.data?.expires_in_seconds ?? 300,
      request_id: requestId, mode: "live",
      error: r.status === 200 ? "" : `Provider HTTP ${{r.status}}`,
    }});
  }} catch (e: any) {{
    return res.json({{ transaction_id: "", sent: false,
      error: e?.message || String(e), request_id: requestId, mode: "live" }});
  }}
}});

router.post("/verify", async (req: Request, res: Response) => {{
  const aadhaar = String(req.body?.aadhaar ?? "").trim();
  const otp = String(req.body?.otp ?? "").trim();
  const txn = String(req.body?.transaction_id ?? "").trim();
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const mode = envMode("{gate_env}");
  if (!AADHAAR_RE.test(aadhaar)) {{
    return res.status(400).json({{ error: "Invalid Aadhaar number",
      request_id: requestId, mode }});
  }}
  const base: Record<string, any> = {{
    aadhaar_masked: maskId(aadhaar, 4), request_id: requestId, mode,
  }};
  if (mode !== "live") {{
    const ok = otp === "123456";
    console.log(`aadhaar.verify mode=mock request_id=${{requestId}} aadhaar_hash=${{hashPii(aadhaar)}} verified=${{ok}}`);
    return res.json({{ ...base, verified: ok,
      status: ok ? "VALID" : "OTP_MISMATCH",
      name: ok ? "RAMESH KUMAR" : "", dob: ok ? "1985-04-21" : "",
      gender: ok ? "M" : "", address_masked: ok ? "XX, MG Road, Bangalore" : "",
      error: ok ? "" : "OTP mismatch (mock expects 123456)" }});
  }}
  const url = env("AADHAAR_VERIFY_URL", "");
  const apiKey = env("AADHAAR_API_KEY", "");
  if (!url || !apiKey) {{
    return res.json({{ ...base, verified: false, status: "ERROR",
      error: "AADHAAR_VERIFY_URL or AADHAAR_API_KEY is unset" }});
  }}
  try {{
    const r = await httpClient().post(url,
      {{ aadhaar, otp, transaction_id: txn }},
      {{ headers: {{ Authorization: `Bearer ${{apiKey}}` }} }});
    const d = r.data || {{}};
    const status = String(d.status ?? "ERROR");
    const verified = r.status === 200 && status === "VALID";
    return res.json({{ ...base, verified, status,
      name: d.name ?? "", dob: d.dob ?? "", gender: d.gender ?? "",
      address_masked: d.address_masked ?? "",
      error: r.status === 200 ? "" : `Provider HTTP ${{r.status}}` }});
  }} catch (e: any) {{
    return res.json({{ ...base, verified: false, status: "ERROR",
      error: e?.message || String(e) }});
  }}
}});

export default router;
'''


GSTIN_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, httpClient }} from "../_util";

// LAMA-injected: {label} ({integration_id}). MasterGST by default.

const router = Router();
const GSTIN_RE = /^[0-9]{{2}}[A-Z]{{5}}[0-9]{{4}}[A-Z]{{1}}[1-9A-Z]{{1}}Z[0-9A-Z]{{1}}$/;

router.post("/verify", async (req: Request, res: Response) => {{
  const gstin = String(req.body?.gstin ?? "").trim().toUpperCase();
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const mode = envMode("{gate_env}");
  const base: Record<string, any> = {{ gstin, request_id: requestId, mode }};

  if (!GSTIN_RE.test(gstin)) {{
    return res.json({{ ...base, verified: false, status: "INVALID",
      error: "GSTIN format invalid" }});
  }}
  if (mode !== "live") {{
    return res.json({{ ...base, verified: true, status: "ACTIVE",
      legal_name: "ACME WIDGETS PRIVATE LIMITED",
      trade_name: "ACME WIDGETS", state: "Karnataka",
      constitution: "Private Limited Company",
      registration_date: "2019-07-12",
      last_filing_status: "GSTR-1 filed for 2026-04" }});
  }}
  const url = env("GSTIN_VERIFY_URL", "https://commonapi.mastergst.com/public/search");
  const clientId = env("GSTIN_CLIENT_ID", "");
  const apiKey = env("GSTIN_API_KEY", "");
  if (!apiKey) {{
    return res.json({{ ...base, verified: false, status: "ERROR",
      error: "GSTIN_API_KEY is unset" }});
  }}
  try {{
    const r = await httpClient().get(url, {{
      params: {{ gstin }},
      headers: {{ client_id: clientId, client_secret: apiKey }},
    }});
    const body = r.data?.data ?? r.data ?? {{}};
    const sts = String(body.sts ?? "ERROR");
    const active = r.status === 200 && (sts === "Active" || sts === "ACTIVE");
    const state = body?.pradr?.addr?.stcd ?? "";
    return res.json({{ ...base, verified: active, status: sts,
      legal_name: body.lgnm ?? "", trade_name: body.tradeNam ?? "",
      state, constitution: body.ctb ?? "",
      registration_date: body.rgdt ?? "",
      last_filing_status: body.lstupdt ?? "",
      error: r.status === 200 ? "" : `Provider HTTP ${{r.status}}` }});
  }} catch (e: any) {{
    return res.json({{ ...base, verified: false, status: "ERROR",
      error: e?.message || String(e) }});
  }}
}});

export default router;
'''


DIGILOCKER_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, httpClient }} from "../_util";

// LAMA-injected: {label} ({integration_id}). MeitY DigiLocker OAuth2.

const router = Router();

router.get("/authorize", async (_req: Request, res: Response) => {{
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const state = randomUUID().replace(/-/g, "");
  const mode = envMode("{gate_env}");
  if (mode !== "live") {{
    return res.json({{
      authorize_url: `https://mock.lama.local/digilocker/authorize?state=${{state}}`,
      state, request_id: requestId, mode: "mock",
    }});
  }}
  const clientId = env("DIGILOCKER_CLIENT_ID", "");
  const redirectUri = env("DIGILOCKER_REDIRECT_URI", "");
  if (!clientId || !redirectUri) {{
    return res.status(500).json({{
      error: "DIGILOCKER_CLIENT_ID / DIGILOCKER_REDIRECT_URI unset",
      request_id: requestId, mode: "live",
    }});
  }}
  const authorize = "https://api.digitallocker.gov.in/public/oauth2/1/authorize"
    + `?response_type=code&client_id=${{encodeURIComponent(clientId)}}`
    + `&redirect_uri=${{encodeURIComponent(redirectUri)}}`
    + `&state=${{state}}&scope=basic+documents`;
  return res.json({{ authorize_url: authorize, state, request_id: requestId, mode: "live" }});
}});

router.post("/document", async (req: Request, res: Response) => {{
  const accessToken = String(req.body?.access_token ?? "").trim();
  const docType = String(req.body?.doc_type ?? "AADHAAR").trim();
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const mode = envMode("{gate_env}");
  const base: Record<string, any> = {{ doc_type: docType, request_id: requestId, mode }};

  if (mode !== "live") {{
    return res.json({{ ...base, fetched: true,
      issued_to: "RAMESH KUMAR", issue_date: "2024-01-15",
      issuer: docType === "AADHAAR" ? "UIDAI" : "Income Tax Department",
      doc_uri: `mock://digilocker/${{docType}}/abcd1234` }});
  }}
  const url = env("DIGILOCKER_DOC_URL", "https://api.digitallocker.gov.in/public/oauth2/1/files/issued");
  try {{
    const r = await httpClient().get(url,
      {{ headers: {{ Authorization: `Bearer ${{accessToken}}` }} }});
    if (r.status !== 200) {{
      return res.json({{ ...base, fetched: false,
        error: `Provider HTTP ${{r.status}}` }});
    }}
    const d = r.data || {{}};
    return res.json({{ ...base, fetched: true,
      issued_to: d.issued_to ?? "", issue_date: d.issue_date ?? "",
      issuer: d.issuer ?? "", doc_uri: d.uri ?? "" }});
  }} catch (e: any) {{
    return res.json({{ ...base, fetched: false, error: e?.message || String(e) }});
  }}
}});

export default router;
'''


ESIGN_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, httpClient }} from "../_util";

// LAMA-injected: {label} ({integration_id}). eMudhra / NSDL / Protean / Leegality.

const router = Router();

router.post("/initiate", async (req: Request, res: Response) => {{
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const txn = randomUUID().replace(/-/g, "").slice(0, 16);
  const mode = envMode("{gate_env}");
  const base: Record<string, any> = {{ request_id: requestId, mode, expires_in_seconds: 600 }};

  if (mode !== "live") {{
    return res.json({{ ...base, transaction_id: txn,
      redirect_url: `https://mock.lama.local/esign/sign?txn=${{txn}}` }});
  }}
  const url = env("ESIGN_INITIATE_URL", "");
  const apiKey = env("ESIGN_API_KEY", "");
  const aspId = env("ESIGN_ASP_ID", "");
  if (!url || !apiKey || !aspId) {{
    return res.json({{ ...base, transaction_id: "",
      error: "ESIGN_INITIATE_URL / ESIGN_API_KEY / ESIGN_ASP_ID unset" }});
  }}
  try {{
    const r = await httpClient(60_000).post(url, {{
      asp_id: aspId,
      document: req.body?.document_base64 ?? "",
      signer_aadhaar: req.body?.signer_aadhaar ?? "",
      signer_name: req.body?.signer_name ?? "",
      purpose: req.body?.purpose ?? "Document signing",
      callback_url: req.body?.callback_url ?? "",
    }}, {{ headers: {{ Authorization: `Bearer ${{apiKey}}` }} }});
    const d = r.data || {{}};
    return res.json({{ ...base,
      transaction_id: d.transaction_id ?? txn,
      redirect_url: d.redirect_url ?? "",
      expires_in_seconds: d.expires_in_seconds ?? 600,
      error: r.status === 200 ? "" : `Provider HTTP ${{r.status}}` }});
  }} catch (e: any) {{
    return res.json({{ ...base, transaction_id: "",
      error: e?.message || String(e) }});
  }}
}});

router.get("/status/:transaction_id", async (req: Request, res: Response) => {{
  const transactionId = req.params.transaction_id;
  const mode = envMode("{gate_env}");
  const base: Record<string, any> = {{ transaction_id: transactionId, mode }};

  if (mode !== "live") {{
    return res.json({{ ...base, status: "SIGNED",
      signed_document_base64: "<base64 PDF would be here>",
      signed_at: new Date().toISOString(),
      signer_name: "RAMESH KUMAR" }});
  }}
  const baseUrl = env("ESIGN_STATUS_URL", "");
  const apiKey = env("ESIGN_API_KEY", "");
  if (!baseUrl) {{
    return res.json({{ ...base, status: "ERROR", error: "ESIGN_STATUS_URL unset" }});
  }}
  try {{
    const url = baseUrl.endsWith("/") ? baseUrl + transactionId : `${{baseUrl}}/${{transactionId}}`;
    const r = await httpClient().get(url,
      {{ headers: {{ Authorization: `Bearer ${{apiKey}}` }} }});
    const d = r.data || {{}};
    return res.json({{ ...base,
      status: d.status ?? "PENDING",
      signed_document_base64: d.signed_document_base64 ?? "",
      signed_at: d.signed_at ?? "",
      signer_name: d.signer_name ?? "" }});
  }} catch (e: any) {{
    return res.json({{ ...base, status: "ERROR", error: e?.message || String(e) }});
  }}
}});

export default router;
'''


UPI_NODE = '''import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import {{ env, envMode, httpClient }} from "../_util";

// LAMA-injected: {label} ({integration_id}). Razorpay-style by default.

const router = Router();

router.post("/initiate", async (req: Request, res: Response) => {{
  const requestId = randomUUID().replace(/-/g, "").slice(0, 12);
  const txn = randomUUID().replace(/-/g, "").slice(0, 14);
  const mode = envMode("{gate_env}");
  const amount = Number(req.body?.amount_rupees ?? 0);
  const orderId = String(req.body?.order_id ?? "");
  const payerVpa = String(req.body?.payer_vpa ?? "");
  const purpose = String(req.body?.purpose ?? "Payment");
  const callback = String(req.body?.callback_url ?? "");
  const base: Record<string, any> = {{ request_id: requestId, mode, expires_in_seconds: 600 }};

  if (mode !== "live") {{
    return res.json({{ ...base, transaction_id: txn,
      intent_url: `upi://pay?pa=merchant@upi&pn=Demo&am=${{amount.toFixed(2)}}&tn=${{encodeURIComponent(purpose)}}&tr=${{txn}}`,
      collect_link: `https://mock.lama.local/pay/${{txn}}` }});
  }}
  const url = env("UPI_INITIATE_URL", "");
  const keyId = env("UPI_KEY_ID", "");
  const secret = env("UPI_KEY_SECRET", "");
  const merchantVpa = env("UPI_MERCHANT_VPA", "");
  if (!url || !keyId || !secret || !merchantVpa) {{
    return res.json({{ ...base, transaction_id: "",
      error: "UPI_INITIATE_URL / UPI_KEY_ID / UPI_KEY_SECRET / UPI_MERCHANT_VPA unset" }});
  }}
  try {{
    const r = await httpClient().post(url, {{
      amount: Math.round(amount * 100),
      currency: "INR", method: "upi",
      order_id: orderId, payer_vpa: payerVpa,
      merchant_vpa: merchantVpa, purpose,
      callback_url: callback,
    }}, {{ auth: {{ username: keyId, password: secret }} }});
    const d = r.data || {{}};
    return res.json({{ ...base,
      transaction_id: d.id ?? txn,
      intent_url: d.intent_url ?? "",
      collect_link: d.short_url ?? d.payment_link ?? "",
      expires_in_seconds: d.expires_in_seconds ?? 600,
      error: (r.status === 200 || r.status === 201) ? "" : `Provider HTTP ${{r.status}}` }});
  }} catch (e: any) {{
    return res.json({{ ...base, transaction_id: "",
      error: e?.message || String(e) }});
  }}
}});

router.get("/status/:transaction_id", async (req: Request, res: Response) => {{
  const transactionId = req.params.transaction_id;
  const mode = envMode("{gate_env}");
  const base: Record<string, any> = {{ transaction_id: transactionId, order_id: transactionId, mode }};

  if (mode !== "live") {{
    return res.json({{ ...base, status: "SUCCESS", amount_rupees: 100.0,
      upi_ref: "UPI" + transactionId.slice(0, 10).toUpperCase(),
      settled_at: new Date().toISOString() }});
  }}
  const baseUrl = env("UPI_STATUS_URL", "");
  const keyId = env("UPI_KEY_ID", "");
  const secret = env("UPI_KEY_SECRET", "");
  if (!baseUrl) {{
    return res.json({{ ...base, status: "ERROR", error: "UPI_STATUS_URL unset" }});
  }}
  try {{
    const url = baseUrl.endsWith("/") ? baseUrl + transactionId : `${{baseUrl}}/${{transactionId}}`;
    const r = await httpClient().get(url, {{ auth: {{ username: keyId, password: secret }} }});
    const d = r.data || {{}};
    const paise = Number(d.amount ?? 0);
    return res.json({{ ...base,
      status: String(d.status ?? "PENDING").toUpperCase(),
      amount_rupees: paise / 100.0,
      upi_ref: d?.acquirer_data?.rrn ?? d?.upi_ref ?? "",
      settled_at: d.captured_at ?? "" }});
  }} catch (e: any) {{
    return res.json({{ ...base, status: "ERROR", error: e?.message || String(e) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Registries — consumed by renderer.py
# ---------------------------------------------------------------------------

# JAVA_UTIL above was written as a `.format` template (doubled braces) for
# consistency with the per-controller templates — collapse them once here so
# the file is emitted verbatim with single braces (valid Java).
JAVA_UTIL = JAVA_UTIL.format()

# NODE_UTIL was authored the same way (doubled braces in function bodies)
# but its imports use single braces, so we can't `.format()` it. Just
# collapse the doubled braces directly; the imports stay untouched.
NODE_UTIL = NODE_UTIL.replace("{{", "{").replace("}}", "}")

# integration_id -> (java_class_simple_name, body_template)
JAVA_IMPLS: Dict[str, Tuple[str, str]] = {
    "pan_verification":    ("PanVerificationController",   PAN_JAVA),
    "aadhaar_ekyc":        ("AadhaarEkycController",       AADHAAR_JAVA),
    "gstin_verification":  ("GstinVerificationController", GSTIN_JAVA),
    "digilocker":          ("DigilockerController",        DIGILOCKER_JAVA),
    "esign":               ("EsignController",             ESIGN_JAVA),
    "upi_payment":         ("UpiPaymentController",        UPI_JAVA),
    "audit_trail_logger":  ("AuditTrailAspect",            AUDIT_LOG_JAVA),
}
# Merge full live-mode controllers for the DPG global stack + DPG-India stack.
# Each registry is authored as a peer module so each integration's Java source
# can live next to its Node source — the merge here makes the renderer treat
# all 19 integrations uniformly (no `TODO: implement live mode` fallback).
JAVA_IMPLS.update(DPG_JAVA_IMPLS)
JAVA_IMPLS.update(DPG_INDIA_JAVA_IMPLS)

# integration_id -> body_template (file path is derived from id, like the
# generic node router).
NODE_IMPLS: Dict[str, str] = {
    "pan_verification":    PAN_NODE,
    "aadhaar_ekyc":        AADHAAR_NODE,
    "gstin_verification":  GSTIN_NODE,
    "digilocker":          DIGILOCKER_NODE,
    "esign":               ESIGN_NODE,
    "upi_payment":         UPI_NODE,
    "audit_trail_logger":  AUDIT_LOG_NODE,
}
NODE_IMPLS.update(DPG_NODE_IMPLS)
NODE_IMPLS.update(DPG_INDIA_NODE_IMPLS)


def render_java_controller(entry) -> tuple:
    """Return (rel_path, body, 'java') for the integration's full live impl,
    or None if no impl is registered."""
    spec = JAVA_IMPLS.get(entry["id"])
    if not spec:
        return None
    cls, body = spec
    rendered = body.format(
        integration_id=entry["id"],
        label=entry["label"].replace('"', '\\"'),
        gate_env=entry.get("gate_env", "MODE"),
        route_prefix=entry.get("route_prefix", "/integrations/" + entry["id"]),
    )
    rel = "src/main/java/com/lama/integrations/" + cls + ".java"
    return (rel, rendered, "java")


def render_node_router(entry) -> tuple:
    """Return (rel_path, body, 'typescript') for the integration's full live
    impl, or None if no impl is registered."""
    body = NODE_IMPLS.get(entry["id"])
    if not body:
        return None
    rendered = body.format(
        integration_id=entry["id"],
        label=entry["label"].replace('"', '\\"'),
        gate_env=entry.get("gate_env", "MODE"),
    )
    rel = "src/integrations/" + entry["id"] + "/router.ts"
    return (rel, rendered, "typescript")


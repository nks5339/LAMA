"""LAMA — full Java + Node live-mode implementations for the platform-style
DPG / DPI integrations (iter-13.65).

Until this module landed, eSignet / Inji / MOSIP Auth / DIVOC / Sunbird RC /
OpenG2P fell through to the generic mock-only controllers in renderer.py
when generated on Java or Node targets. This file ships the full live
plumbing for all six, mirroring the contract the Python templates use:
  * mode env var (e.g. ESIGNET_MODE = mock | live)
  * mock branch returns canned data; live branch reads env creds and
    calls the provider via java.net.http.HttpClient / axios
  * if a required env var is unset in live mode → structured error
    response, never a stacktrace.

Templates use the same {{integration_id}} / {{label}} / {{gate_env}} /
{{route_prefix}} placeholder surface as live_impls.py — every literal `{` /
`}` is doubled.
"""

# ---------------------------------------------------------------------------
# eSignet (MOSIP) — OIDC three-leg flow
# ---------------------------------------------------------------------------

ESIGNET_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class EsignetOidcController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}
    private static String enc(String s) {{
        return URLEncoder.encode(s == null ? "" : s, StandardCharsets.UTF_8);
    }}

    @GetMapping("/authorize")
    public Map<String, Object> authorize() {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String state = UUID.randomUUID().toString().replace("-", "");
        String nonce = UUID.randomUUID().toString().replace("-", "");
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("state", state);
        r.put("nonce", nonce);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("authorize_url",
                "https://mock.lama.local/esignet/authorize?state=" + state + "&nonce=" + nonce);
            return r;
        }}
        String base = env("ESIGNET_BASE_URL", "");
        String cid = env("ESIGNET_CLIENT_ID", "");
        String redir = env("ESIGNET_REDIRECT_URI", "");
        String scope = env("ESIGNET_SCOPE", "openid profile email").replace(" ", "+");
        if (base.isEmpty() || cid.isEmpty() || redir.isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_REDIRECT_URI unset");
            return r;
        }}
        String url = base.replaceAll("/$$", "") + "/authorize?response_type=code"
            + "&client_id=" + enc(cid) + "&redirect_uri=" + enc(redir)
            + "&scope=" + scope + "&state=" + state + "&nonce=" + nonce
            + "&acr_values=mosip:idp:acr:generated-code";
        r.put("mode", "live");
        r.put("authorize_url", url);
        return r;
    }}

    @PostMapping("/token")
    public Map<String, Object> token(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("access_token", "mock.access." + rid);
            r.put("id_token", "mock.id." + rid);
            r.put("token_type", "Bearer");
            r.put("expires_in", 3600);
            r.put("scope", "openid profile email");
            return r;
        }}
        String base = env("ESIGNET_BASE_URL", "");
        String cid = env("ESIGNET_CLIENT_ID", "");
        String sec = env("ESIGNET_CLIENT_SECRET", "");
        String redir = env("ESIGNET_REDIRECT_URI", "");
        if (base.isEmpty() || cid.isEmpty() || sec.isEmpty() || redir.isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_CLIENT_SECRET / ESIGNET_REDIRECT_URI unset");
            return r;
        }}
        Map<String, String> form = new LinkedHashMap<>();
        form.put("grant_type", "authorization_code");
        form.put("code", String.valueOf(body.getOrDefault("code", "")));
        form.put("client_id", cid);
        form.put("client_secret", sec);
        form.put("redirect_uri", redir);
        Object cv = body.get("code_verifier");
        if (cv != null) form.put("code_verifier", String.valueOf(cv));
        StringBuilder fb = new StringBuilder();
        for (Map.Entry<String, String> e : form.entrySet()) {{
            if (fb.length() > 0) fb.append('&');
            fb.append(enc(e.getKey())).append('=').append(enc(e.getValue()));
        }}
        try {{
            HttpRequest req = HttpRequest.newBuilder(URI.create(base.replaceAll("/$$", "") + "/token"))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(fb.toString()))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("access_token", data.getOrDefault("access_token", ""));
            r.put("id_token", data.getOrDefault("id_token", ""));
            r.put("token_type", data.getOrDefault("token_type", "Bearer"));
            r.put("expires_in", data.getOrDefault("expires_in", 0));
            r.put("scope", data.getOrDefault("scope", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/userinfo")
    public Map<String, Object> userinfo(@RequestParam("access_token") String accessToken) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("sub", "mock-sub-" + rid);
            r.put("name", "Ramesh Kumar");
            r.put("given_name", "Ramesh");
            r.put("family_name", "Kumar");
            r.put("email", "ramesh@example.test");
            r.put("phone_number", "+91XXXXX12345");
            return r;
        }}
        String base = env("ESIGNET_BASE_URL", "");
        if (base.isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "ESIGNET_BASE_URL unset");
            return r;
        }}
        try {{
            HttpRequest req = HttpRequest.newBuilder(URI.create(base.replaceAll("/$$", "") + "/userinfo"))
                .header("Authorization", "Bearer " + accessToken)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(req, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("sub", data.getOrDefault("sub", ""));
            r.put("name", data.getOrDefault("name", ""));
            r.put("given_name", data.getOrDefault("given_name", ""));
            r.put("family_name", data.getOrDefault("family_name", ""));
            r.put("email", data.getOrDefault("email", ""));
            r.put("phone_number", data.getOrDefault("phone_number", ""));
            r.put("picture_uri", data.getOrDefault("picture", ""));
            r.put("raw", data);
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


ESIGNET_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";
const enc = (s: string) => encodeURIComponent(s || "");

router.get("/authorize", async (_req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const state = randomUUID().replace(/-/g, "");
  const nonce = randomUUID().replace(/-/g, "");
  if (!isLive()) {{
    return res.json({{
      request_id: rid, state, nonce, mode: "mock",
      authorize_url: `https://mock.lama.local/esignet/authorize?state=${{state}}&nonce=${{nonce}}`,
    }});
  }}
  const base = env("ESIGNET_BASE_URL"); const cid = env("ESIGNET_CLIENT_ID");
  const redir = env("ESIGNET_REDIRECT_URI"); const scope = env("ESIGNET_SCOPE", "openid profile email").replace(/ /g, "+");
  if (!base || !cid || !redir) {{
    return res.json({{ request_id: rid, state, nonce, mode: "live",
      error: "ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_REDIRECT_URI unset" }});
  }}
  const url = `${{base.replace(/\\/$$/, "")}}/authorize?response_type=code&client_id=${{enc(cid)}}` +
    `&redirect_uri=${{enc(redir)}}&scope=${{scope}}&state=${{state}}&nonce=${{nonce}}&acr_values=mosip:idp:acr:generated-code`;
  return res.json({{ request_id: rid, state, nonce, mode: "live", authorize_url: url }});
}});

router.post("/token", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock",
      access_token: `mock.access.${{rid}}`, id_token: `mock.id.${{rid}}`,
      token_type: "Bearer", expires_in: 3600, scope: "openid profile email" }});
  }}
  const base = env("ESIGNET_BASE_URL"); const cid = env("ESIGNET_CLIENT_ID");
  const sec = env("ESIGNET_CLIENT_SECRET"); const redir = env("ESIGNET_REDIRECT_URI");
  if (!base || !cid || !sec || !redir) {{
    return res.json({{ request_id: rid, mode: "live",
      error: "ESIGNET_BASE_URL / ESIGNET_CLIENT_ID / ESIGNET_CLIENT_SECRET / ESIGNET_REDIRECT_URI unset" }});
  }}
  const form = new URLSearchParams();
  form.append("grant_type", "authorization_code");
  form.append("code", String(req.body?.code || ""));
  form.append("client_id", cid); form.append("client_secret", sec);
  form.append("redirect_uri", redir);
  if (req.body?.code_verifier) form.append("code_verifier", String(req.body.code_verifier));
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/token`, form.toString(), {{
      timeout: 30000, headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
    }});
    return res.json({{ request_id: rid, mode: "live",
      access_token: r.data?.access_token || "", id_token: r.data?.id_token || "",
      token_type: r.data?.token_type || "Bearer", expires_in: r.data?.expires_in || 0,
      scope: r.data?.scope || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/userinfo", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const accessToken = String(req.query.access_token || "");
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock",
      sub: "mock-sub-" + rid, name: "Ramesh Kumar",
      given_name: "Ramesh", family_name: "Kumar",
      email: "ramesh@example.test", phone_number: "+91XXXXX12345" }});
  }}
  const base = env("ESIGNET_BASE_URL");
  if (!base) return res.json({{ request_id: rid, mode: "live", error: "ESIGNET_BASE_URL unset" }});
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/userinfo`, {{
      timeout: 30000, headers: {{ Authorization: `Bearer ${{accessToken}}` }},
    }});
    return res.json({{ request_id: rid, mode: "live",
      sub: r.data?.sub || "", name: r.data?.name || "",
      given_name: r.data?.given_name || "", family_name: r.data?.family_name || "",
      email: r.data?.email || "", phone_number: r.data?.phone_number || "",
      picture_uri: r.data?.picture || "", raw: r.data }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Inji (MOSIP) — VC wallet (issue + verify)
# ---------------------------------------------------------------------------

INJI_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class InjiWalletController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/credentials/issue")
    public Map<String, Object> issue(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("issued", true);
            r.put("credential_id", "vc:mock:" + rid);
            r.put("credential_jwt", "eyJtb2NrIjoidmMifQ." + rid + ".sig");
            r.put("push_status", "PUSHED");
            return r;
        }}
        String base = env("INJI_ISSUER_BASE_URL", "");
        String key = env("INJI_ISSUER_API_KEY", "");
        String iss = env("INJI_ISSUER_ID", "");
        if (base.isEmpty() || key.isEmpty() || iss.isEmpty()) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("push_status", "FAILED");
            r.put("error", "INJI_ISSUER_BASE_URL / INJI_ISSUER_API_KEY / INJI_ISSUER_ID unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("issuer", iss);
            reqBody.put("holder", body.getOrDefault("holder_id", ""));
            reqBody.put("credentialType", body.getOrDefault("credential_type", "ProofOfIdentityCredential"));
            reqBody.put("claims", body.getOrDefault("claims", new HashMap<>()));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(base.replaceAll("/$$", "") + "/credentials/issue"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("issued", ok);
            if (!ok) {{
                r.put("push_status", "FAILED");
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("credential_id", data.getOrDefault("credential_id", ""));
            r.put("credential_jwt", data.getOrDefault("credential", ""));
            r.put("push_status", data.getOrDefault("push_status", "PENDING"));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("push_status", "FAILED");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/credentials/verify")
    public Map<String, Object> verify(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("valid", true);
            r.put("issuer", "did:web:mock-issuer.lama.local");
            r.put("credential_type", "ProofOfIdentityCredential");
            Map<String, Object> claims = new LinkedHashMap<>();
            claims.put("name", "Ramesh Kumar");
            claims.put("dob", "1985-04-21");
            r.put("subject_claims", claims);
            r.put("expires_at", "2030-12-31T23:59:59Z");
            r.put("revoked", false);
            return r;
        }}
        String base = env("INJI_VERIFIER_BASE_URL", "");
        String key = env("INJI_VERIFIER_API_KEY", "");
        if (base.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", "INJI_VERIFIER_BASE_URL / INJI_VERIFIER_API_KEY unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("credential", body.getOrDefault("credential_jwt", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(base.replaceAll("/$$", "") + "/credentials/verify"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("valid", false);
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("valid", Boolean.TRUE.equals(data.get("valid")));
            r.put("issuer", data.getOrDefault("issuer", ""));
            r.put("credential_type", data.getOrDefault("credentialType", ""));
            r.put("subject_claims", data.getOrDefault("credentialSubject", new HashMap<>()));
            r.put("expires_at", data.getOrDefault("expiresAt", ""));
            r.put("revoked", Boolean.TRUE.equals(data.get("revoked")));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


INJI_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/credentials/issue", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", issued: true,
      credential_id: "vc:mock:" + rid,
      credential_jwt: "eyJtb2NrIjoidmMifQ." + rid + ".sig",
      push_status: "PUSHED" }});
  }}
  const base = env("INJI_ISSUER_BASE_URL"); const key = env("INJI_ISSUER_API_KEY"); const iss = env("INJI_ISSUER_ID");
  if (!base || !key || !iss) {{
    return res.json({{ request_id: rid, mode: "live", issued: false, push_status: "FAILED",
      error: "INJI_ISSUER_BASE_URL / INJI_ISSUER_API_KEY / INJI_ISSUER_ID unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/credentials/issue`, {{
      issuer: iss, holder: req.body?.holder_id || "",
      credentialType: req.body?.credential_type || "ProofOfIdentityCredential",
      claims: req.body?.claims || {{}},
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live", issued: true,
      credential_id: r.data?.credential_id || "",
      credential_jwt: r.data?.credential || "",
      push_status: r.data?.push_status || "PENDING" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", issued: false, push_status: "FAILED",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/credentials/verify", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", valid: true,
      issuer: "did:web:mock-issuer.lama.local",
      credential_type: "ProofOfIdentityCredential",
      subject_claims: {{ name: "Ramesh Kumar", dob: "1985-04-21" }},
      expires_at: "2030-12-31T23:59:59Z", revoked: false }});
  }}
  const base = env("INJI_VERIFIER_BASE_URL"); const key = env("INJI_VERIFIER_API_KEY");
  if (!base || !key) {{
    return res.json({{ request_id: rid, mode: "live", valid: false,
      error: "INJI_VERIFIER_BASE_URL / INJI_VERIFIER_API_KEY unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/credentials/verify`,
      {{ credential: req.body?.credential_jwt || "" }},
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live",
      valid: !!r.data?.valid, issuer: r.data?.issuer || "",
      credential_type: r.data?.credentialType || "",
      subject_claims: r.data?.credentialSubject || {{}},
      expires_at: r.data?.expiresAt || "", revoked: !!r.data?.revoked }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", valid: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# MOSIP Auth — yes/no auth + e-KYC
# ---------------------------------------------------------------------------

MOSIP_AUTH_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class MosipAuthController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/auth")
    public Map<String, Object> auth(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String uid = String.valueOf(body.getOrDefault("uin_or_vid", ""));
        String authType = String.valueOf(body.getOrDefault("auth_type", "OTP")).toUpperCase();
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("auth_type", authType);
        r.put("identity_masked", IntegrationUtil.maskId(uid, 4));
        if (!isLive()) {{
            boolean ok = !"OTP".equals(authType) || "123456".equals(String.valueOf(body.getOrDefault("otp", "")));
            r.put("mode", "mock");
            r.put("authenticated", ok);
            r.put("transaction_id", "mock-txn-" + rid);
            r.put("response_code", ok ? "OK" : "AUTH_FAILED");
            if (!ok) r.put("error", "Mock OTP mismatch (expected 123456)");
            return r;
        }}
        String base = env("MOSIP_AUTH_BASE_URL", "");
        String partner = env("MOSIP_PARTNER_ID", "");
        String key = env("MOSIP_PARTNER_API_KEY", "");
        if (base.isEmpty() || partner.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("authenticated", false);
            r.put("error", "MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset");
            return r;
        }}
        try {{
            Map<String, Object> at = new LinkedHashMap<>();
            at.put("otp", "OTP".equals(authType));
            at.put("demo", "DEMO".equals(authType));
            at.put("bio", "BIO".equals(authType));
            Map<String, Object> req = new LinkedHashMap<>();
            req.put("otp", body.getOrDefault("otp", ""));
            req.put("demographics", body.getOrDefault("demographics", new HashMap<>()));
            req.put("biometrics", body.getOrDefault("biometrics_b64", ""));
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("id", "mosip.identity.auth");
            reqBody.put("version", "1.0");
            reqBody.put("requestTime", Instant.now().toString());
            reqBody.put("individualId", uid);
            reqBody.put("individualIdType", uid.length() == 16 ? "VID" : "UIN");
            reqBody.put("authType", at);
            reqBody.put("request", req);
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/idauthentication/v1/auth/" + partner))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            Map<String, Object> data = resp.body() == null || resp.body().isEmpty()
                ? new HashMap<>() : M.readValue(resp.body(), Map.class);
            Map<String, Object> respMap = (Map<String, Object>) data.getOrDefault("response", new HashMap<>());
            r.put("authenticated", Boolean.TRUE.equals(respMap.get("authStatus")));
            r.put("transaction_id", data.getOrDefault("transactionID", ""));
            Object tok = respMap.get("authToken");
            r.put("response_code", tok == null || tok.toString().isEmpty() ? "OK" : tok);
            if (resp.statusCode() != 200) r.put("error", "Provider HTTP " + resp.statusCode());
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("authenticated", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/ekyc")
    public Map<String, Object> ekyc(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String uid = String.valueOf(body.getOrDefault("uin_or_vid", ""));
        String otp = String.valueOf(body.getOrDefault("otp", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("identity_masked", IntegrationUtil.maskId(uid, 4));
        if (!isLive()) {{
            boolean ok = "123456".equals(otp);
            r.put("mode", "mock");
            r.put("success", ok);
            if (ok) {{
                r.put("name", "RAMESH KUMAR"); r.put("dob", "1985-04-21");
                r.put("gender", "M"); r.put("address_masked", "XX, MG Road, Bengaluru");
            }} else {{
                r.put("error", "Mock OTP mismatch (expected 123456)");
            }}
            return r;
        }}
        String base = env("MOSIP_AUTH_BASE_URL", "");
        String partner = env("MOSIP_PARTNER_ID", "");
        String key = env("MOSIP_PARTNER_API_KEY", "");
        if (base.isEmpty() || partner.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("success", false);
            r.put("error", "MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("individualId", uid);
            reqBody.put("otp", otp);
            reqBody.put("transactionID", body.getOrDefault("transaction_id", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/idauthentication/v1/kyc/" + partner))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            Map<String, Object> data = resp.body() == null || resp.body().isEmpty()
                ? new HashMap<>() : M.readValue(resp.body(), Map.class);
            Map<String, Object> respMap = (Map<String, Object>) data.getOrDefault("response", new HashMap<>());
            Map<String, Object> idMap = (Map<String, Object>) respMap.getOrDefault("identity", new HashMap<>());
            r.put("success", resp.statusCode() == 200);
            r.put("name", idMap.getOrDefault("name", ""));
            r.put("dob", idMap.getOrDefault("dateOfBirth", ""));
            r.put("gender", idMap.getOrDefault("gender", ""));
            r.put("address_masked", idMap.getOrDefault("addressLine1", ""));
            if (resp.statusCode() != 200) r.put("error", "Provider HTTP " + resp.statusCode());
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("success", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


MOSIP_AUTH_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";
import {{ maskId }} from "../_util";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/auth", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const uid = String(req.body?.uin_or_vid || "");
  const authType = String(req.body?.auth_type || "OTP").toUpperCase();
  const base_resp: any = {{ request_id: rid, auth_type: authType, identity_masked: maskId(uid, 4) }};
  if (!isLive()) {{
    const ok = authType !== "OTP" || req.body?.otp === "123456";
    return res.json({{ ...base_resp, mode: "mock", authenticated: ok,
      transaction_id: "mock-txn-" + rid, response_code: ok ? "OK" : "AUTH_FAILED",
      ...(ok ? {{}} : {{ error: "Mock OTP mismatch (expected 123456)" }}) }});
  }}
  const base = env("MOSIP_AUTH_BASE_URL"); const partner = env("MOSIP_PARTNER_ID"); const key = env("MOSIP_PARTNER_API_KEY");
  if (!base || !partner || !key) {{
    return res.json({{ ...base_resp, mode: "live", authenticated: false,
      error: "MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset" }});
  }}
  try {{
    const body = {{
      id: "mosip.identity.auth", version: "1.0",
      requestTime: new Date().toISOString(),
      individualId: uid, individualIdType: uid.length === 16 ? "VID" : "UIN",
      authType: {{ otp: authType === "OTP", demo: authType === "DEMO", bio: authType === "BIO" }},
      request: {{ otp: req.body?.otp || "", demographics: req.body?.demographics || {{}}, biometrics: req.body?.biometrics_b64 || "" }},
    }};
    const r = await axios.post(
      `${{base.replace(/\\/$$/, "")}}/idauthentication/v1/auth/${{partner}}`,
      body,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }},
    );
    const respObj = r.data?.response || {{}};
    return res.json({{ ...base_resp, mode: "live",
      authenticated: !!respObj.authStatus,
      transaction_id: r.data?.transactionID || "",
      response_code: respObj.authToken || "OK" }});
  }} catch (e: any) {{
    return res.json({{ ...base_resp, mode: "live", authenticated: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/ekyc", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const uid = String(req.body?.uin_or_vid || "");
  const otp = String(req.body?.otp || "");
  const base_resp: any = {{ request_id: rid, identity_masked: maskId(uid, 4) }};
  if (!isLive()) {{
    const ok = otp === "123456";
    if (ok) return res.json({{ ...base_resp, mode: "mock", success: true,
      name: "RAMESH KUMAR", dob: "1985-04-21", gender: "M", address_masked: "XX, MG Road, Bengaluru" }});
    return res.json({{ ...base_resp, mode: "mock", success: false, error: "Mock OTP mismatch (expected 123456)" }});
  }}
  const base = env("MOSIP_AUTH_BASE_URL"); const partner = env("MOSIP_PARTNER_ID"); const key = env("MOSIP_PARTNER_API_KEY");
  if (!base || !partner || !key) {{
    return res.json({{ ...base_resp, mode: "live", success: false,
      error: "MOSIP_AUTH_BASE_URL / MOSIP_PARTNER_ID / MOSIP_PARTNER_API_KEY unset" }});
  }}
  try {{
    const r = await axios.post(
      `${{base.replace(/\\/$$/, "")}}/idauthentication/v1/kyc/${{partner}}`,
      {{ individualId: uid, otp, transactionID: req.body?.transaction_id || "" }},
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }},
    );
    const id = r.data?.response?.identity || {{}};
    return res.json({{ ...base_resp, mode: "live", success: true,
      name: id.name || "", dob: id.dateOfBirth || "", gender: id.gender || "",
      address_masked: id.addressLine1 || "" }});
  }} catch (e: any) {{
    return res.json({{ ...base_resp, mode: "live", success: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# DIVOC — open verifiable credentials
# ---------------------------------------------------------------------------

DIVOC_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class DivocController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/issue")
    public Map<String, Object> issue(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("issued", true);
            r.put("certificate_id", "cert:mock:" + rid);
            r.put("osid", "1-" + rid);
            r.put("qr_url", "https://mock.lama.local/divoc/verify/" + rid);
            return r;
        }}
        String base = env("DIVOC_BASE_URL", "");
        String token = env("DIVOC_ISSUER_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("error", "DIVOC_BASE_URL / DIVOC_ISSUER_TOKEN unset");
            return r;
        }}
        try {{
            String entity = String.valueOf(body.getOrDefault("entity", ""));
            Object payload = body.getOrDefault("payload", new HashMap<>());
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/" + entity))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(payload)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("issued", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("certificate_id", data.getOrDefault("certificateId", ""));
            r.put("osid", data.getOrDefault("osid", ""));
            r.put("qr_url", data.getOrDefault("verifyUrl", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/verify")
    public Map<String, Object> verify(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("valid", true);
            r.put("entity", "VaccinationCertificate");
            r.put("issuer", "did:web:mock-issuer.lama.local");
            r.put("issued_to", "Ramesh Kumar");
            r.put("revoked", false);
            return r;
        }}
        String url = env("DIVOC_VERIFY_URL", "");
        if (url.isEmpty()) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", "DIVOC_VERIFY_URL unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("signedCredential", body.getOrDefault("signed_credential", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("valid", false);
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("valid", Boolean.TRUE.equals(data.get("verified")));
            r.put("entity", data.getOrDefault("entity", ""));
            r.put("issuer", data.getOrDefault("issuer", ""));
            r.put("issued_to", data.getOrDefault("issuedTo", ""));
            r.put("revoked", Boolean.TRUE.equals(data.get("revoked")));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


DIVOC_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/issue", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", issued: true,
      certificate_id: "cert:mock:" + rid, osid: "1-" + rid,
      qr_url: `https://mock.lama.local/divoc/verify/${{rid}}` }});
  }}
  const base = env("DIVOC_BASE_URL"); const token = env("DIVOC_ISSUER_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, mode: "live", issued: false,
      error: "DIVOC_BASE_URL / DIVOC_ISSUER_TOKEN unset" }});
  }}
  try {{
    const entity = String(req.body?.entity || "");
    const r = await axios.post(
      `${{base.replace(/\\/$$/, "")}}/api/v1/${{entity}}`,
      req.body?.payload || {{}},
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}`, "Content-Type": "application/json" }} }},
    );
    return res.json({{ request_id: rid, mode: "live", issued: true,
      certificate_id: r.data?.certificateId || "", osid: r.data?.osid || "",
      qr_url: r.data?.verifyUrl || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", issued: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/verify", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", valid: true,
      entity: "VaccinationCertificate", issuer: "did:web:mock-issuer.lama.local",
      issued_to: "Ramesh Kumar", revoked: false }});
  }}
  const url = env("DIVOC_VERIFY_URL");
  if (!url) return res.json({{ request_id: rid, mode: "live", valid: false, error: "DIVOC_VERIFY_URL unset" }});
  try {{
    const r = await axios.post(url, {{ signedCredential: req.body?.signed_credential || "" }}, {{ timeout: 30000 }});
    return res.json({{ request_id: rid, mode: "live",
      valid: !!r.data?.verified, entity: r.data?.entity || "",
      issuer: r.data?.issuer || "", issued_to: r.data?.issuedTo || "",
      revoked: !!r.data?.revoked }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", valid: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Sunbird RC — registry + credentialing
# ---------------------------------------------------------------------------

SUNBIRD_RC_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class SunbirdRcController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/registry/{{schema}}")
    public Map<String, Object> register(@PathVariable("schema") String schema,
                                        @RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("schema_name", schema);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("success", true);
            r.put("osid", "1-mock-" + rid);
            return r;
        }}
        String base = env("SUNBIRD_RC_BASE_URL", "");
        String token = env("SUNBIRD_RC_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("success", false);
            r.put("error", "SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset");
            return r;
        }}
        try {{
            Object entity = body.getOrDefault("entity", new HashMap<>());
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/" + schema))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(entity)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("success", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            Object result = data.get("result");
            String osid = "";
            if (result instanceof Map) {{
                Object inner = ((Map<?, ?>) result).get(schema.toLowerCase());
                if (inner instanceof Map) osid = String.valueOf(((Map<?, ?>) inner).getOrDefault("osid", ""));
            }}
            r.put("osid", osid);
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("success", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/registry/{{schema}}/{{osid}}")
    public Map<String, Object> getEntity(@PathVariable("schema") String schema,
                                         @PathVariable("osid") String osid) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("osid", osid);
        r.put("schema_name", schema);
        if (!isLive()) {{
            Map<String, Object> ent = new LinkedHashMap<>();
            ent.put("osid", osid);
            ent.put("name", "Mock Entity");
            r.put("mode", "mock");
            r.put("entity", ent);
            return r;
        }}
        String base = env("SUNBIRD_RC_BASE_URL", "");
        String token = env("SUNBIRD_RC_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/" + schema + "/" + osid))
                .header("Authorization", "Bearer " + token)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            r.put("entity", M.readValue(resp.body(), Map.class));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/credentials")
    public Map<String, Object> issueCredential(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("issued", true);
            r.put("credential_id", "vc:mock:" + rid);
            r.put("credential_jwt", "eyJtb2NrIjoidmMifQ." + rid + ".sig");
            return r;
        }}
        String base = env("SUNBIRD_VC_BASE_URL", "");
        String token = env("SUNBIRD_RC_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("error", "SUNBIRD_VC_BASE_URL / SUNBIRD_RC_TOKEN unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("schema", body.getOrDefault("schema_name", ""));
            reqBody.put("osid", body.getOrDefault("osid", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(base.replaceAll("/$$", "") + "/credentials"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("issued", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("credential_id", data.getOrDefault("id", ""));
            r.put("credential_jwt", data.getOrDefault("credential", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("issued", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


SUNBIRD_RC_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/registry/:schema", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const schema = req.params.schema;
  if (!isLive()) {{
    return res.json({{ request_id: rid, schema_name: schema, mode: "mock",
      success: true, osid: "1-mock-" + rid }});
  }}
  const base = env("SUNBIRD_RC_BASE_URL"); const token = env("SUNBIRD_RC_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, schema_name: schema, mode: "live", success: false,
      error: "SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/${{schema}}`,
      req.body?.entity || {{}},
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}`, "Content-Type": "application/json" }} }});
    const inner = r.data?.result?.[schema.toLowerCase()] || {{}};
    return res.json({{ request_id: rid, schema_name: schema, mode: "live",
      success: true, osid: inner.osid || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, schema_name: schema, mode: "live", success: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/registry/:schema/:osid", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const {{ schema, osid }} = req.params;
  if (!isLive()) {{
    return res.json({{ request_id: rid, schema_name: schema, osid, mode: "mock",
      entity: {{ osid, name: "Mock Entity" }} }});
  }}
  const base = env("SUNBIRD_RC_BASE_URL"); const token = env("SUNBIRD_RC_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, schema_name: schema, osid, mode: "live",
      error: "SUNBIRD_RC_BASE_URL / SUNBIRD_RC_TOKEN unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/${{schema}}/${{osid}}`,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}` }} }});
    return res.json({{ request_id: rid, schema_name: schema, osid, mode: "live", entity: r.data }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, schema_name: schema, osid, mode: "live",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/credentials", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", issued: true,
      credential_id: "vc:mock:" + rid,
      credential_jwt: "eyJtb2NrIjoidmMifQ." + rid + ".sig" }});
  }}
  const base = env("SUNBIRD_VC_BASE_URL"); const token = env("SUNBIRD_RC_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, mode: "live", issued: false,
      error: "SUNBIRD_VC_BASE_URL / SUNBIRD_RC_TOKEN unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/credentials`,
      {{ schema: req.body?.schema_name || "", osid: req.body?.osid || "" }},
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live", issued: true,
      credential_id: r.data?.id || "", credential_jwt: r.data?.credential || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", issued: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# OpenG2P — beneficiary + disbursement
# ---------------------------------------------------------------------------

OPENG2P_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class OpenG2PController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @GetMapping("/beneficiary/{{beneficiary_id}}")
    public Map<String, Object> beneficiary(@PathVariable("beneficiary_id") String id) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("beneficiary_id", id);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("found", true);
            r.put("name_masked", "RAMESH K****");
            r.put("bank_account_masked", "****1234");
            r.put("program_ids", Arrays.asList("PM-KISAN", "NSAP"));
            r.put("kyc_status", "VERIFIED");
            return r;
        }}
        String base = env("OPENG2P_BASE_URL", "");
        String token = env("OPENG2P_PARTNER_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("found", false);
            r.put("error", "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/beneficiary/" + id))
                .header("Authorization", "Bearer " + token)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            r.put("found", resp.statusCode() == 200);
            if (resp.statusCode() != 200) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("name_masked", data.getOrDefault("nameMasked", ""));
            r.put("bank_account_masked", data.getOrDefault("bankAccountMasked", ""));
            r.put("program_ids", data.getOrDefault("programIds", new ArrayList<>()));
            r.put("kyc_status", data.getOrDefault("kycStatus", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("found", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/disbursement/initiate")
    public Map<String, Object> disburse(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("accepted", true);
            r.put("disbursement_id", "g2p-mock-" + rid);
            r.put("status", "ACCEPTED");
            return r;
        }}
        String base = env("OPENG2P_BASE_URL", "");
        String token = env("OPENG2P_PARTNER_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("accepted", false);
            r.put("status", "FAILED");
            r.put("error", "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset");
            return r;
        }}
        try {{
            double amt = ((Number) body.getOrDefault("amount_rupees", 0)).doubleValue();
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("beneficiaryId", body.getOrDefault("beneficiary_id", ""));
            reqBody.put("amount", Math.round(amt * 100));
            reqBody.put("currency", "INR");
            reqBody.put("programId", body.getOrDefault("program_id", ""));
            reqBody.put("paymentReference", body.getOrDefault("payment_reference", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/disbursement"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + token)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            int sc = resp.statusCode();
            boolean ok = sc == 200 || sc == 201 || sc == 202;
            r.put("accepted", ok);
            if (!ok) {{ r.put("status", "FAILED"); r.put("error", "Provider HTTP " + sc); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("disbursement_id", data.getOrDefault("disbursementId", ""));
            r.put("status", data.getOrDefault("status", "INITIATED"));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("accepted", false);
            r.put("status", "FAILED");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/disbursement/{{disbursement_id}}/status")
    public Map<String, Object> disburseStatus(@PathVariable("disbursement_id") String id) {{
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("disbursement_id", id);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("status", "SUCCESS");
            r.put("amount_rupees", 2000.0);
            r.put("settled_at", Instant.now().toString());
            r.put("bank_ref", "UTR" + id.substring(0, Math.min(10, id.length())).toUpperCase());
            return r;
        }}
        String base = env("OPENG2P_BASE_URL", "");
        String token = env("OPENG2P_PARTNER_TOKEN", "");
        if (base.isEmpty() || token.isEmpty()) {{
            r.put("mode", "live");
            r.put("status", "PENDING");
            r.put("error", "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/disbursement/" + id))
                .header("Authorization", "Bearer " + token)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("status", "PENDING");
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("status", String.valueOf(data.getOrDefault("status", "PENDING")).toUpperCase());
            Object amt = data.getOrDefault("amount", 0);
            double amtP = amt instanceof Number ? ((Number) amt).doubleValue() : 0;
            r.put("amount_rupees", amtP / 100.0);
            r.put("settled_at", data.getOrDefault("settledAt", ""));
            r.put("bank_ref", data.getOrDefault("bankRef", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("status", "PENDING");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


OPENG2P_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.get("/beneficiary/:beneficiary_id", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const id = req.params.beneficiary_id;
  if (!isLive()) {{
    return res.json({{ request_id: rid, beneficiary_id: id, mode: "mock", found: true,
      name_masked: "RAMESH K****", bank_account_masked: "****1234",
      program_ids: ["PM-KISAN", "NSAP"], kyc_status: "VERIFIED" }});
  }}
  const base = env("OPENG2P_BASE_URL"); const token = env("OPENG2P_PARTNER_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, beneficiary_id: id, mode: "live", found: false,
      error: "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/beneficiary/${{id}}`,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}` }} }});
    return res.json({{ request_id: rid, beneficiary_id: id, mode: "live", found: true,
      name_masked: r.data?.nameMasked || "",
      bank_account_masked: r.data?.bankAccountMasked || "",
      program_ids: r.data?.programIds || [],
      kyc_status: r.data?.kycStatus || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, beneficiary_id: id, mode: "live", found: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/disbursement/initiate", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", accepted: true,
      disbursement_id: "g2p-mock-" + rid, status: "ACCEPTED" }});
  }}
  const base = env("OPENG2P_BASE_URL"); const token = env("OPENG2P_PARTNER_TOKEN");
  if (!base || !token) {{
    return res.json({{ request_id: rid, mode: "live", accepted: false, status: "FAILED",
      error: "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset" }});
  }}
  try {{
    const amt = Number(req.body?.amount_rupees || 0);
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/disbursement`, {{
      beneficiaryId: req.body?.beneficiary_id || "",
      amount: Math.round(amt * 100), currency: "INR",
      programId: req.body?.program_id || "",
      paymentReference: req.body?.payment_reference || "",
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live", accepted: true,
      disbursement_id: r.data?.disbursementId || "",
      status: r.data?.status || "INITIATED" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", accepted: false, status: "FAILED",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/disbursement/:disbursement_id/status", async (req: Request, res: Response) => {{
  const id = req.params.disbursement_id;
  if (!isLive()) {{
    return res.json({{ disbursement_id: id, mode: "mock", status: "SUCCESS",
      amount_rupees: 2000.0, settled_at: new Date().toISOString(),
      bank_ref: "UTR" + id.substring(0, 10).toUpperCase() }});
  }}
  const base = env("OPENG2P_BASE_URL"); const token = env("OPENG2P_PARTNER_TOKEN");
  if (!base || !token) {{
    return res.json({{ disbursement_id: id, mode: "live", status: "PENDING",
      error: "OPENG2P_BASE_URL / OPENG2P_PARTNER_TOKEN unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/disbursement/${{id}}`,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{token}}` }} }});
    return res.json({{ disbursement_id: id, mode: "live",
      status: String(r.data?.status || "PENDING").toUpperCase(),
      amount_rupees: (Number(r.data?.amount || 0)) / 100.0,
      settled_at: r.data?.settledAt || "", bank_ref: r.data?.bankRef || "" }});
  }} catch (e: any) {{
    return res.json({{ disbursement_id: id, mode: "live", status: "PENDING",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Registries — picked up by live_impls.JAVA_IMPLS / NODE_IMPLS via .update(...)
# ---------------------------------------------------------------------------

DPG_JAVA_IMPLS = {
    "esignet_oidc": ("EsignetOidcController", ESIGNET_JAVA),
    "inji_wallet":  ("InjiWalletController",  INJI_JAVA),
    "mosip_auth":   ("MosipAuthController",   MOSIP_AUTH_JAVA),
    "divoc":        ("DivocController",       DIVOC_JAVA),
    "sunbird_rc":   ("SunbirdRcController",   SUNBIRD_RC_JAVA),
    "openg2p":      ("OpenG2PController",     OPENG2P_JAVA),
}

DPG_NODE_IMPLS = {
    "esignet_oidc": ESIGNET_NODE,
    "inji_wallet":  INJI_NODE,
    "mosip_auth":   MOSIP_AUTH_NODE,
    "divoc":        DIVOC_NODE,
    "sunbird_rc":   SUNBIRD_RC_NODE,
    "openg2p":      OPENG2P_NODE,
}


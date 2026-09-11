"""LAMA — full Java + Node live-mode implementations for the India-stack
DPG / DPI integrations (iter-13.65).

Mirror of `live_impls_dpg.py` for the six India-specific entries:
Bharatkosh (NTRP), PFMS, API Setu, Bhashini, ABDM, Jeevan Pramaan.
Same mock-first / env-driven contract; every `{` / `}` doubled because
catalog renderer calls `.format(...)` on these templates.
"""

# ---------------------------------------------------------------------------
# Bharatkosh (NTRP) — challan + status
# ---------------------------------------------------------------------------

BHARATKOSH_JAVA = '''package com.lama.integrations;

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
public class BharatkoshNtrpController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/challan/create")
    public Map<String, Object> create(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            String cid = "NTRP-MOCK-" + rid.toUpperCase();
            r.put("mode", "mock");
            r.put("created", true);
            r.put("challan_id", cid);
            r.put("pay_url", "https://mock.lama.local/bharatkosh/pay/" + cid);
            r.put("expires_in_seconds", 900);
            return r;
        }}
        String base = env("BHARATKOSH_BASE_URL", "");
        String dept = env("BHARATKOSH_DEPT_CODE", "");
        String key = env("BHARATKOSH_API_KEY", "");
        if (base.isEmpty() || dept.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("created", false);
            r.put("error", "BHARATKOSH_BASE_URL / BHARATKOSH_DEPT_CODE / BHARATKOSH_API_KEY unset");
            return r;
        }}
        try {{
            double amt = ((Number) body.getOrDefault("amount_rupees", 0)).doubleValue();
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("deptCode", dept);
            reqBody.put("purposeCode", body.getOrDefault("purpose_code", ""));
            reqBody.put("amount", Math.round(amt * 100));
            reqBody.put("payerName", body.getOrDefault("payer_name", ""));
            reqBody.put("payerPAN", body.getOrDefault("payer_pan", ""));
            reqBody.put("purposeDescription", body.getOrDefault("purpose_description", ""));
            reqBody.put("referenceNo", body.getOrDefault("reference_no", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/challan/create"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("created", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("challan_id", data.getOrDefault("challanId", ""));
            r.put("pay_url", data.getOrDefault("paymentUrl", ""));
            Object exp = data.getOrDefault("expiresInSeconds", 0);
            r.put("expires_in_seconds", exp instanceof Number ? ((Number) exp).intValue() : 0);
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("created", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/challan/{{challan_id}}")
    public Map<String, Object> status(@PathVariable("challan_id") String id) {{
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("challan_id", id);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("status", "PAID");
            r.put("amount_rupees", 1500.0);
            r.put("paid_at", java.time.Instant.now().toString());
            r.put("bank_ref", "NTRPREF" + id.substring(Math.max(0, id.length() - 8)).toUpperCase());
            return r;
        }}
        String base = env("BHARATKOSH_BASE_URL", "");
        String key = env("BHARATKOSH_API_KEY", "");
        if (base.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("status", "PENDING");
            r.put("error", "BHARATKOSH_BASE_URL / BHARATKOSH_API_KEY unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/challan/" + id))
                .header("Authorization", "Bearer " + key)
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
            r.put("paid_at", data.getOrDefault("paidAt", ""));
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


BHARATKOSH_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/challan/create", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    const cid = "NTRP-MOCK-" + rid.toUpperCase();
    return res.json({{ request_id: rid, mode: "mock", created: true,
      challan_id: cid, pay_url: `https://mock.lama.local/bharatkosh/pay/${{cid}}`,
      expires_in_seconds: 900 }});
  }}
  const base = env("BHARATKOSH_BASE_URL"); const dept = env("BHARATKOSH_DEPT_CODE"); const key = env("BHARATKOSH_API_KEY");
  if (!base || !dept || !key) {{
    return res.json({{ request_id: rid, mode: "live", created: false,
      error: "BHARATKOSH_BASE_URL / BHARATKOSH_DEPT_CODE / BHARATKOSH_API_KEY unset" }});
  }}
  try {{
    const amt = Number(req.body?.amount_rupees || 0);
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/challan/create`, {{
      deptCode: dept, purposeCode: req.body?.purpose_code || "",
      amount: Math.round(amt * 100), payerName: req.body?.payer_name || "",
      payerPAN: req.body?.payer_pan || "",
      purposeDescription: req.body?.purpose_description || "",
      referenceNo: req.body?.reference_no || "",
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live", created: true,
      challan_id: r.data?.challanId || "", pay_url: r.data?.paymentUrl || "",
      expires_in_seconds: Number(r.data?.expiresInSeconds || 0) }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", created: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/challan/:challan_id", async (req: Request, res: Response) => {{
  const id = req.params.challan_id;
  if (!isLive()) {{
    return res.json({{ challan_id: id, mode: "mock", status: "PAID", amount_rupees: 1500.0,
      paid_at: new Date().toISOString(),
      bank_ref: "NTRPREF" + id.substring(Math.max(0, id.length - 8)).toUpperCase() }});
  }}
  const base = env("BHARATKOSH_BASE_URL"); const key = env("BHARATKOSH_API_KEY");
  if (!base || !key) {{
    return res.json({{ challan_id: id, mode: "live", status: "PENDING",
      error: "BHARATKOSH_BASE_URL / BHARATKOSH_API_KEY unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/challan/${{id}}`,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}` }} }});
    return res.json({{ challan_id: id, mode: "live",
      status: String(r.data?.status || "PENDING").toUpperCase(),
      amount_rupees: (Number(r.data?.amount || 0)) / 100.0,
      paid_at: r.data?.paidAt || "", bank_ref: r.data?.bankRef || "" }});
  }} catch (e: any) {{
    return res.json({{ challan_id: id, mode: "live", status: "PENDING",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# PFMS — beneficiary validate + DBT payment
# ---------------------------------------------------------------------------

PFMS_JAVA = '''package com.lama.integrations;

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
public class PfmsController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/beneficiary/validate")
    public Map<String, Object> validate(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String bid = String.valueOf(body.getOrDefault("beneficiary_id", ""));
        String acc = String.valueOf(body.getOrDefault("bank_account", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("beneficiary_id", bid);
        r.put("bank_account_masked", IntegrationUtil.maskId(acc, 4));
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("valid", true);
            Object name = body.get("name");
            r.put("account_holder_match", name == null || String.valueOf(name).length() > 1);
            r.put("bank_name", "STATE BANK OF INDIA");
            r.put("branch", "MG ROAD");
            return r;
        }}
        String base = env("PFMS_BASE_URL", "");
        String agency = env("PFMS_AGENCY_CODE", "");
        String key = env("PFMS_API_KEY", "");
        if (base.isEmpty() || agency.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", "PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("agencyCode", agency);
            reqBody.put("beneficiaryId", bid);
            reqBody.put("bankAccount", acc);
            reqBody.put("ifsc", body.getOrDefault("ifsc", ""));
            reqBody.put("name", body.getOrDefault("name", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/beneficiary/validate"))
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
            r.put("account_holder_match", data.get("nameMatch"));
            r.put("bank_name", data.getOrDefault("bankName", ""));
            r.put("branch", data.getOrDefault("branch", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("valid", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/payment/instruction")
    public Map<String, Object> payment(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("accepted", true);
            r.put("payment_id", "PFMS-MOCK-" + rid.toUpperCase());
            r.put("status", "ACCEPTED");
            r.put("bank_ref", "UTR" + rid.substring(0, 10).toUpperCase());
            return r;
        }}
        String base = env("PFMS_BASE_URL", "");
        String agency = env("PFMS_AGENCY_CODE", "");
        String key = env("PFMS_API_KEY", "");
        if (base.isEmpty() || agency.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("accepted", false);
            r.put("status", "FAILED");
            r.put("error", "PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset");
            return r;
        }}
        try {{
            double amt = ((Number) body.getOrDefault("amount_rupees", 0)).doubleValue();
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("agencyCode", agency);
            reqBody.put("beneficiaryId", body.getOrDefault("beneficiary_id", ""));
            reqBody.put("amount", Math.round(amt * 100));
            reqBody.put("programCode", body.getOrDefault("program_code", ""));
            reqBody.put("purpose", body.getOrDefault("payment_purpose", ""));
            reqBody.put("reference", body.getOrDefault("payment_reference", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/payment"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            int sc = resp.statusCode();
            boolean ok = sc == 200 || sc == 201 || sc == 202;
            r.put("accepted", ok);
            if (!ok) {{ r.put("status", "FAILED"); r.put("error", "Provider HTTP " + sc); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("payment_id", data.getOrDefault("paymentId", ""));
            r.put("status", data.getOrDefault("status", "INITIATED"));
            r.put("bank_ref", data.getOrDefault("bankRef", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("accepted", false);
            r.put("status", "FAILED");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


PFMS_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";
import {{ maskId }} from "../_util";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/beneficiary/validate", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const bid = String(req.body?.beneficiary_id || "");
  const acc = String(req.body?.bank_account || "");
  const base_resp: any = {{ request_id: rid, beneficiary_id: bid, bank_account_masked: maskId(acc, 4) }};
  if (!isLive()) {{
    const name = req.body?.name;
    return res.json({{ ...base_resp, mode: "mock", valid: true,
      account_holder_match: name === undefined || String(name).length > 1,
      bank_name: "STATE BANK OF INDIA", branch: "MG ROAD" }});
  }}
  const base = env("PFMS_BASE_URL"); const agency = env("PFMS_AGENCY_CODE"); const key = env("PFMS_API_KEY");
  if (!base || !agency || !key) {{
    return res.json({{ ...base_resp, mode: "live", valid: false,
      error: "PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/beneficiary/validate`, {{
      agencyCode: agency, beneficiaryId: bid, bankAccount: acc,
      ifsc: req.body?.ifsc || "", name: req.body?.name || "",
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ ...base_resp, mode: "live",
      valid: !!r.data?.valid, account_holder_match: r.data?.nameMatch,
      bank_name: r.data?.bankName || "", branch: r.data?.branch || "" }});
  }} catch (e: any) {{
    return res.json({{ ...base_resp, mode: "live", valid: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/payment/instruction", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    return res.json({{ request_id: rid, mode: "mock", accepted: true,
      payment_id: "PFMS-MOCK-" + rid.toUpperCase(), status: "ACCEPTED",
      bank_ref: "UTR" + rid.substring(0, 10).toUpperCase() }});
  }}
  const base = env("PFMS_BASE_URL"); const agency = env("PFMS_AGENCY_CODE"); const key = env("PFMS_API_KEY");
  if (!base || !agency || !key) {{
    return res.json({{ request_id: rid, mode: "live", accepted: false, status: "FAILED",
      error: "PFMS_BASE_URL / PFMS_AGENCY_CODE / PFMS_API_KEY unset" }});
  }}
  try {{
    const amt = Number(req.body?.amount_rupees || 0);
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/payment`, {{
      agencyCode: agency,
      beneficiaryId: req.body?.beneficiary_id || "",
      amount: Math.round(amt * 100),
      programCode: req.body?.program_code || "",
      purpose: req.body?.payment_purpose || "",
      reference: req.body?.payment_reference || "",
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, mode: "live", accepted: true,
      payment_id: r.data?.paymentId || "", status: r.data?.status || "INITIATED",
      bank_ref: r.data?.bankRef || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", accepted: false, status: "FAILED",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# API Setu — catalog + generic invoke
# ---------------------------------------------------------------------------

API_SETU_JAVA = '''package com.lama.integrations;

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
public class ApiSetuController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @GetMapping("/catalog")
    public Map<String, Object> catalog() {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            List<Map<String, Object>> apis = new ArrayList<>();
            apis.add(Map.of("id", "driving-license-verify", "department", "MORTH", "method", "POST"));
            apis.add(Map.of("id", "vehicle-rc-verify", "department", "MORTH", "method", "POST"));
            apis.add(Map.of("id", "voter-id-verify", "department", "ECI", "method", "POST"));
            apis.add(Map.of("id", "ration-card-verify", "department", "DFPD", "method", "POST"));
            r.put("mode", "mock");
            r.put("apis", apis);
            r.put("total", apis.size());
            return r;
        }}
        String base = env("API_SETU_BASE_URL", "");
        String cid = env("API_SETU_CLIENT_ID", "");
        String sec = env("API_SETU_CLIENT_SECRET", "");
        if (base.isEmpty() || cid.isEmpty() || sec.isEmpty()) {{
            r.put("mode", "live");
            r.put("apis", new ArrayList<>());
            r.put("total", 0);
            r.put("error", "API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/apis"))
                .header("X-APISETU-CLIENTID", cid)
                .header("X-APISETU-APIKEY", sec)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            Map<String, Object> data = resp.body() == null || resp.body().isEmpty()
                ? new HashMap<>() : M.readValue(resp.body(), Map.class);
            Object apis = data.getOrDefault("apis", data.getOrDefault("items", new ArrayList<>()));
            int total = apis instanceof List ? ((List<?>) apis).size() : 0;
            r.put("apis", apis);
            r.put("total", total);
            if (resp.statusCode() != 200) r.put("error", "Provider HTTP " + resp.statusCode());
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("apis", new ArrayList<>());
            r.put("total", 0);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/invoke/{{api_id}}")
    public Map<String, Object> invoke(@PathVariable("api_id") String apiId,
                                      @RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("api_id", apiId);
        Object params = body.getOrDefault("params", new HashMap<>());
        String method = String.valueOf(body.getOrDefault("method", "POST")).toUpperCase();
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("status", 200);
            Map<String, Object> bodyMock = new LinkedHashMap<>();
            bodyMock.put("verified", true);
            bodyMock.put("name", "Ramesh Kumar");
            bodyMock.put("issued_on", "2020-01-15");
            bodyMock.put("echo", params);
            r.put("body", bodyMock);
            return r;
        }}
        String base = env("API_SETU_BASE_URL", "");
        String cid = env("API_SETU_CLIENT_ID", "");
        String sec = env("API_SETU_CLIENT_SECRET", "");
        if (base.isEmpty() || cid.isEmpty() || sec.isEmpty()) {{
            r.put("mode", "live");
            r.put("status", 0);
            r.put("error", "API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset");
            return r;
        }}
        try {{
            String url = base.replaceAll("/$$", "") + "/api/v1/invoke/" + apiId;
            HttpRequest hr;
            if ("GET".equals(method) && params instanceof Map) {{
                StringBuilder qs = new StringBuilder();
                for (Map.Entry<?, ?> e : ((Map<?, ?>) params).entrySet()) {{
                    if (qs.length() > 0) qs.append('&');
                    qs.append(URLEncoder.encode(String.valueOf(e.getKey()), StandardCharsets.UTF_8))
                      .append('=')
                      .append(URLEncoder.encode(String.valueOf(e.getValue()), StandardCharsets.UTF_8));
                }}
                hr = HttpRequest.newBuilder(URI.create(qs.length() == 0 ? url : url + "?" + qs))
                    .header("X-APISETU-CLIENTID", cid)
                    .header("X-APISETU-APIKEY", sec)
                    .GET().build();
            }} else {{
                hr = HttpRequest.newBuilder(URI.create(url))
                    .header("X-APISETU-CLIENTID", cid)
                    .header("X-APISETU-APIKEY", sec)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(params)))
                    .build();
            }}
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            r.put("status", resp.statusCode());
            try {{
                r.put("body", M.readValue(resp.body(), Object.class));
            }} catch (Exception parseEx) {{
                r.put("body", resp.body());
            }}
            if (resp.statusCode() != 200) r.put("error", "Provider HTTP " + resp.statusCode());
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("status", 0);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


API_SETU_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.get("/catalog", async (_req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  if (!isLive()) {{
    const apis = [
      {{ id: "driving-license-verify", department: "MORTH", method: "POST" }},
      {{ id: "vehicle-rc-verify", department: "MORTH", method: "POST" }},
      {{ id: "voter-id-verify", department: "ECI", method: "POST" }},
      {{ id: "ration-card-verify", department: "DFPD", method: "POST" }},
    ];
    return res.json({{ request_id: rid, mode: "mock", apis, total: apis.length }});
  }}
  const base = env("API_SETU_BASE_URL"); const cid = env("API_SETU_CLIENT_ID"); const sec = env("API_SETU_CLIENT_SECRET");
  if (!base || !cid || !sec) {{
    return res.json({{ request_id: rid, mode: "live", apis: [], total: 0,
      error: "API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/apis`,
      {{ timeout: 30000, headers: {{ "X-APISETU-CLIENTID": cid, "X-APISETU-APIKEY": sec }} }});
    const apis = r.data?.apis || r.data?.items || [];
    return res.json({{ request_id: rid, mode: "live", apis, total: apis.length }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", apis: [], total: 0,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/invoke/:api_id", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const apiId = req.params.api_id;
  const params = req.body?.params || {{}};
  const method = String(req.body?.method || "POST").toUpperCase();
  if (!isLive()) {{
    return res.json({{ request_id: rid, api_id: apiId, mode: "mock", status: 200,
      body: {{ verified: true, name: "Ramesh Kumar", issued_on: "2020-01-15", echo: params }} }});
  }}
  const base = env("API_SETU_BASE_URL"); const cid = env("API_SETU_CLIENT_ID"); const sec = env("API_SETU_CLIENT_SECRET");
  if (!base || !cid || !sec) {{
    return res.json({{ request_id: rid, api_id: apiId, mode: "live", status: 0,
      error: "API_SETU_BASE_URL / API_SETU_CLIENT_ID / API_SETU_CLIENT_SECRET unset" }});
  }}
  try {{
    const url = `${{base.replace(/\\/$$/, "")}}/api/v1/invoke/${{apiId}}`;
    const headers = {{ "X-APISETU-CLIENTID": cid, "X-APISETU-APIKEY": sec, "Content-Type": "application/json" }};
    const r = method === "GET"
      ? await axios.get(url, {{ timeout: 30000, headers, params }})
      : await axios.post(url, params, {{ timeout: 30000, headers }});
    return res.json({{ request_id: rid, api_id: apiId, mode: "live",
      status: r.status, body: r.data }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, api_id: apiId, mode: "live",
      status: e?.response?.status || 0,
      body: e?.response?.data ?? null,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Bhashini — translate / transliterate / TTS
# ---------------------------------------------------------------------------

BHASHINI_JAVA = '''package com.lama.integrations;

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
public class BhashiniController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}
    private static String[] creds() {{
        return new String[] {{
            env("BHASHINI_BASE_URL", ""),
            env("BHASHINI_API_KEY", ""),
            env("BHASHINI_USER_ID", ""),
        }};
    }}

    @PostMapping("/translate")
    public Map<String, Object> translate(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String src = String.valueOf(body.getOrDefault("source_lang", ""));
        String tgt = String.valueOf(body.getOrDefault("target_lang", ""));
        String text = String.valueOf(body.getOrDefault("text", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("source_lang", src);
        r.put("target_lang", tgt);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("translated_text", "[mock " + src + "->" + tgt + "] " + text);
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("translated_text", "");
            r.put("error", "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("sourceLanguage", src);
            reqBody.put("targetLanguage", tgt);
            reqBody.put("text", text);
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/api/v1/translate"))
                .header("Authorization", c[1])
                .header("userID", c[2])
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("translated_text", "");
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("translated_text", data.getOrDefault("translatedText", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("translated_text", "");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/transliterate")
    public Map<String, Object> transliterate(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String src = String.valueOf(body.getOrDefault("source_script", ""));
        String tgt = String.valueOf(body.getOrDefault("target_script", ""));
        String text = String.valueOf(body.getOrDefault("text", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("source_script", src);
        r.put("target_script", tgt);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("transliterated_text", "[mock " + src + "->" + tgt + "] " + text);
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("transliterated_text", "");
            r.put("error", "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("sourceScript", src);
            reqBody.put("targetScript", tgt);
            reqBody.put("text", text);
            reqBody.put("language", body.getOrDefault("language", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/api/v1/transliterate"))
                .header("Authorization", c[1])
                .header("userID", c[2])
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("transliterated_text", "");
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("transliterated_text", data.getOrDefault("transliteratedText", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("transliterated_text", "");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/tts")
    public Map<String, Object> tts(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String lang = String.valueOf(body.getOrDefault("language", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("language", lang);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("audio_base64", "UklGRiYAAABXQVZFZm10IBAA");
            r.put("audio_format", "wav");
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("audio_base64", "");
            r.put("error", "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("language", lang);
            reqBody.put("text", body.getOrDefault("text", ""));
            reqBody.put("voice", body.getOrDefault("voice", "default"));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/api/v1/tts"))
                .header("Authorization", c[1])
                .header("userID", c[2])
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("audio_base64", "");
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("audio_base64", data.getOrDefault("audio", ""));
            r.put("audio_format", data.getOrDefault("format", "wav"));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("audio_base64", "");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


BHASHINI_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";
const creds = () => ({{
  base: env("BHASHINI_BASE_URL"),
  key: env("BHASHINI_API_KEY"),
  user: env("BHASHINI_USER_ID"),
}});

router.post("/translate", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const src = String(req.body?.source_lang || ""); const tgt = String(req.body?.target_lang || "");
  const text = String(req.body?.text || "");
  if (!isLive()) {{
    return res.json({{ request_id: rid, source_lang: src, target_lang: tgt, mode: "mock",
      translated_text: `[mock ${{src}}->${{tgt}}] ${{text}}` }});
  }}
  const {{ base, key, user }} = creds();
  if (!base || !key || !user) {{
    return res.json({{ request_id: rid, source_lang: src, target_lang: tgt, mode: "live",
      translated_text: "",
      error: "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/translate`,
      {{ sourceLanguage: src, targetLanguage: tgt, text }},
      {{ timeout: 30000, headers: {{ Authorization: key, userID: user, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, source_lang: src, target_lang: tgt, mode: "live",
      translated_text: r.data?.translatedText || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, source_lang: src, target_lang: tgt, mode: "live", translated_text: "",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/transliterate", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const src = String(req.body?.source_script || ""); const tgt = String(req.body?.target_script || "");
  const text = String(req.body?.text || "");
  if (!isLive()) {{
    return res.json({{ request_id: rid, source_script: src, target_script: tgt, mode: "mock",
      transliterated_text: `[mock ${{src}}->${{tgt}}] ${{text}}` }});
  }}
  const {{ base, key, user }} = creds();
  if (!base || !key || !user) {{
    return res.json({{ request_id: rid, source_script: src, target_script: tgt, mode: "live",
      transliterated_text: "",
      error: "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/transliterate`,
      {{ sourceScript: src, targetScript: tgt, text, language: req.body?.language || "" }},
      {{ timeout: 30000, headers: {{ Authorization: key, userID: user, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, source_script: src, target_script: tgt, mode: "live",
      transliterated_text: r.data?.transliteratedText || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, source_script: src, target_script: tgt, mode: "live",
      transliterated_text: "",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/tts", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const lang = String(req.body?.language || "");
  if (!isLive()) {{
    return res.json({{ request_id: rid, language: lang, mode: "mock",
      audio_base64: "UklGRiYAAABXQVZFZm10IBAA", audio_format: "wav" }});
  }}
  const {{ base, key, user }} = creds();
  if (!base || !key || !user) {{
    return res.json({{ request_id: rid, language: lang, mode: "live", audio_base64: "",
      error: "BHASHINI_BASE_URL / BHASHINI_API_KEY / BHASHINI_USER_ID unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/tts`,
      {{ language: lang, text: req.body?.text || "", voice: req.body?.voice || "default" }},
      {{ timeout: 60000, headers: {{ Authorization: key, userID: user, "Content-Type": "application/json" }} }});
    return res.json({{ request_id: rid, language: lang, mode: "live",
      audio_base64: r.data?.audio || "", audio_format: r.data?.format || "wav" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, language: lang, mode: "live", audio_base64: "",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# ABDM — Ayushman Bharat Health Stack
# ---------------------------------------------------------------------------

ABDM_JAVA = '''package com.lama.integrations;

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
public class AbdmController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}
    private static String[] creds() {{
        return new String[] {{
            env("ABDM_BASE_URL", ""),
            env("ABDM_CLIENT_ID", ""),
            env("ABDM_CLIENT_SECRET", ""),
        }};
    }}

    @PostMapping("/abha/create")
    public Map<String, Object> abhaCreate(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String otp = String.valueOf(body.getOrDefault("otp", ""));
        Object pref = body.get("health_id_preference");
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        if (!isLive()) {{
            boolean ok = "123456".equals(otp);
            r.put("mode", "mock");
            r.put("created", ok);
            if (ok) {{
                r.put("abha_number", "91-1234-5678-9012");
                r.put("abha_address", pref == null ? "ramesh@abdm" : String.valueOf(pref));
                r.put("name", "RAMESH KUMAR");
                r.put("gender", "M");
                r.put("dob", "1985-04-21");
            }} else {{
                r.put("error", "Mock OTP mismatch (expected 123456)");
            }}
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("created", false);
            r.put("error", "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("otp", otp);
            reqBody.put("txnId", body.getOrDefault("transaction_id", ""));
            reqBody.put("healthId", pref == null ? "" : String.valueOf(pref));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/v1/registration/aadhaar/createHealthIdWithPreVerified"))
                .header("Content-Type", "application/json")
                .header("X-CM-ID", c[1])
                .header("Authorization", "Bearer " + c[2])
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("created", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("abha_number", data.getOrDefault("healthIdNumber", ""));
            r.put("abha_address", data.getOrDefault("healthId", ""));
            r.put("name", data.getOrDefault("name", ""));
            r.put("gender", data.getOrDefault("gender", ""));
            r.put("dob", data.getOrDefault("dayOfBirth", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("created", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/abha/{{abha_number}}")
    public Map<String, Object> abhaProfile(@PathVariable("abha_number") String abha) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("abha_number", abha);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("abha_address", "ramesh@abdm");
            r.put("name", "RAMESH KUMAR");
            r.put("gender", "M");
            r.put("dob", "1985-04-21");
            r.put("state", "KARNATAKA");
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/v1/account/profile/" + abha))
                .header("X-CM-ID", c[1])
                .header("Authorization", "Bearer " + c[2])
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("abha_address", data.getOrDefault("healthId", ""));
            r.put("name", data.getOrDefault("name", ""));
            r.put("gender", data.getOrDefault("gender", ""));
            r.put("dob", data.getOrDefault("dayOfBirth", ""));
            r.put("state", data.getOrDefault("stateName", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @PostMapping("/hpr/verify")
    public Map<String, Object> hprVerify(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String hpr = String.valueOf(body.getOrDefault("hpr_id", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("hpr_id", hpr);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("verified", true);
            r.put("name", "DR. RAMESH KUMAR");
            r.put("qualifications", Arrays.asList("MBBS", "MD (General Medicine)"));
            r.put("registration_council", "MEDICAL COUNCIL OF INDIA");
            r.put("practice_status", "ACTIVE");
            return r;
        }}
        String[] c = creds();
        if (c[0].isEmpty() || c[1].isEmpty() || c[2].isEmpty()) {{
            r.put("mode", "live");
            r.put("verified", false);
            r.put("error", "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                c[0].replaceAll("/$$", "") + "/v1/hpr/verify/" + hpr))
                .header("X-CM-ID", c[1])
                .header("Authorization", "Bearer " + c[2])
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("verified", false);
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("verified", Boolean.TRUE.equals(data.get("verified")));
            r.put("name", data.getOrDefault("name", ""));
            r.put("qualifications", data.getOrDefault("qualifications", new ArrayList<>()));
            r.put("registration_council", data.getOrDefault("council", ""));
            r.put("practice_status", data.getOrDefault("status", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("verified", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


ABDM_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";
const creds = () => ({{
  base: env("ABDM_BASE_URL"),
  cid: env("ABDM_CLIENT_ID"),
  sec: env("ABDM_CLIENT_SECRET"),
}});

router.post("/abha/create", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const otp = String(req.body?.otp || "");
  const pref = req.body?.health_id_preference;
  if (!isLive()) {{
    const ok = otp === "123456";
    if (ok) {{
      return res.json({{ request_id: rid, mode: "mock", created: true,
        abha_number: "91-1234-5678-9012", abha_address: pref || "ramesh@abdm",
        name: "RAMESH KUMAR", gender: "M", dob: "1985-04-21" }});
    }}
    return res.json({{ request_id: rid, mode: "mock", created: false,
      error: "Mock OTP mismatch (expected 123456)" }});
  }}
  const {{ base, cid, sec }} = creds();
  if (!base || !cid || !sec) {{
    return res.json({{ request_id: rid, mode: "live", created: false,
      error: "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset" }});
  }}
  try {{
    const r = await axios.post(
      `${{base.replace(/\\/$$/, "")}}/v1/registration/aadhaar/createHealthIdWithPreVerified`,
      {{ otp, txnId: req.body?.transaction_id || "", healthId: pref || "" }},
      {{ timeout: 30000, headers: {{ "X-CM-ID": cid, Authorization: `Bearer ${{sec}}`, "Content-Type": "application/json" }} }},
    );
    return res.json({{ request_id: rid, mode: "live", created: true,
      abha_number: r.data?.healthIdNumber || "", abha_address: r.data?.healthId || "",
      name: r.data?.name || "", gender: r.data?.gender || "", dob: r.data?.dayOfBirth || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, mode: "live", created: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/abha/:abha_number", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const abha = req.params.abha_number;
  if (!isLive()) {{
    return res.json({{ request_id: rid, abha_number: abha, mode: "mock",
      abha_address: "ramesh@abdm", name: "RAMESH KUMAR",
      gender: "M", dob: "1985-04-21", state: "KARNATAKA" }});
  }}
  const {{ base, cid, sec }} = creds();
  if (!base || !cid || !sec) {{
    return res.json({{ request_id: rid, abha_number: abha, mode: "live",
      error: "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/v1/account/profile/${{abha}}`,
      {{ timeout: 30000, headers: {{ "X-CM-ID": cid, Authorization: `Bearer ${{sec}}` }} }});
    return res.json({{ request_id: rid, abha_number: abha, mode: "live",
      abha_address: r.data?.healthId || "", name: r.data?.name || "",
      gender: r.data?.gender || "", dob: r.data?.dayOfBirth || "",
      state: r.data?.stateName || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, abha_number: abha, mode: "live",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.post("/hpr/verify", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const hpr = String(req.body?.hpr_id || "");
  if (!isLive()) {{
    return res.json({{ request_id: rid, hpr_id: hpr, mode: "mock", verified: true,
      name: "DR. RAMESH KUMAR",
      qualifications: ["MBBS", "MD (General Medicine)"],
      registration_council: "MEDICAL COUNCIL OF INDIA", practice_status: "ACTIVE" }});
  }}
  const {{ base, cid, sec }} = creds();
  if (!base || !cid || !sec) {{
    return res.json({{ request_id: rid, hpr_id: hpr, mode: "live", verified: false,
      error: "ABDM_BASE_URL / ABDM_CLIENT_ID / ABDM_CLIENT_SECRET unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/v1/hpr/verify/${{hpr}}`,
      {{ timeout: 30000, headers: {{ "X-CM-ID": cid, Authorization: `Bearer ${{sec}}` }} }});
    return res.json({{ request_id: rid, hpr_id: hpr, mode: "live",
      verified: !!r.data?.verified, name: r.data?.name || "",
      qualifications: r.data?.qualifications || [],
      registration_council: r.data?.council || "",
      practice_status: r.data?.status || "" }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, hpr_id: hpr, mode: "live", verified: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Jeevan Pramaan — Digital Life Certificate
# ---------------------------------------------------------------------------

JEEVAN_PRAMAAN_JAVA = '''package com.lama.integrations;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.LocalDate;
import java.util.*;

/** LAMA-injected integration: {label} ({integration_id}). */
@RestController
@RequestMapping("{route_prefix}")
public class JeevanPramaanController {{

    private static final ObjectMapper M = new ObjectMapper();

    @Value("${{{gate_env}:mock}}")
    private String mode;

    private boolean isLive() {{ return "live".equalsIgnoreCase(mode); }}
    private static String env(String k, String d) {{
        String v = System.getenv(k);
        return (v == null || v.isEmpty()) ? d : v;
    }}

    @PostMapping("/dlc/generate")
    public Map<String, Object> generate(@RequestBody Map<String, Object> body) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        String aadhaar = String.valueOf(body.getOrDefault("aadhaar", ""));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("aadhaar_masked", IntegrationUtil.maskId(aadhaar, 4));
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("generated", true);
            r.put("pramaan_id", "JP-MOCK-" + rid.toUpperCase());
            r.put("issued_on", LocalDate.now().toString());
            r.put("valid_until", "2027-06-12");
            return r;
        }}
        String base = env("JEEVAN_PRAMAAN_BASE_URL", "");
        String key = env("JEEVAN_PRAMAAN_API_KEY", "");
        if (base.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("generated", false);
            r.put("error", "JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset");
            return r;
        }}
        try {{
            Map<String, Object> reqBody = new LinkedHashMap<>();
            reqBody.put("aadhaar", aadhaar);
            reqBody.put("pdoCode", body.getOrDefault("pension_pdo_code", ""));
            reqBody.put("ppoNumber", body.getOrDefault("ppo_number", ""));
            reqBody.put("biometric", body.getOrDefault("biometric_b64", ""));
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/dlc/generate"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + key)
                .POST(HttpRequest.BodyPublishers.ofString(M.writeValueAsString(reqBody)))
                .build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            boolean ok = resp.statusCode() == 200 || resp.statusCode() == 201;
            r.put("generated", ok);
            if (!ok) {{ r.put("error", "Provider HTTP " + resp.statusCode()); return r; }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("pramaan_id", data.getOrDefault("pramaanId", ""));
            r.put("issued_on", data.getOrDefault("issuedOn", ""));
            r.put("valid_until", data.getOrDefault("validUntil", ""));
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("generated", false);
            r.put("error", ex.getMessage());
        }}
        return r;
    }}

    @GetMapping("/dlc/{{pramaan_id}}")
    public Map<String, Object> details(@PathVariable("pramaan_id") String id) {{
        String rid = UUID.randomUUID().toString().substring(0, 12);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("request_id", rid);
        r.put("pramaan_id", id);
        if (!isLive()) {{
            r.put("mode", "mock");
            r.put("aadhaar_masked", "********1234");
            r.put("pensioner_name", "RAMESH KUMAR");
            r.put("pension_pdo_code", "MOCK-PDO-001");
            r.put("ppo_number", "PPO/2020/12345");
            r.put("issued_on", "2026-01-15");
            r.put("valid_until", "2027-01-15");
            r.put("status", "ACTIVE");
            return r;
        }}
        String base = env("JEEVAN_PRAMAAN_BASE_URL", "");
        String key = env("JEEVAN_PRAMAAN_API_KEY", "");
        if (base.isEmpty() || key.isEmpty()) {{
            r.put("mode", "live");
            r.put("error", "JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset");
            return r;
        }}
        try {{
            HttpRequest hr = HttpRequest.newBuilder(URI.create(
                base.replaceAll("/$$", "") + "/api/v1/dlc/" + id))
                .header("Authorization", "Bearer " + key)
                .GET().build();
            HttpResponse<String> resp = IntegrationUtil.httpClient().send(hr, HttpResponse.BodyHandlers.ofString());
            r.put("mode", "live");
            if (resp.statusCode() != 200) {{
                r.put("error", "Provider HTTP " + resp.statusCode());
                return r;
            }}
            Map<String, Object> data = M.readValue(resp.body(), Map.class);
            r.put("aadhaar_masked", data.getOrDefault("aadhaarMasked", ""));
            r.put("pensioner_name", data.getOrDefault("name", ""));
            r.put("pension_pdo_code", data.getOrDefault("pdoCode", ""));
            r.put("ppo_number", data.getOrDefault("ppoNumber", ""));
            r.put("issued_on", data.getOrDefault("issuedOn", ""));
            r.put("valid_until", data.getOrDefault("validUntil", ""));
            r.put("status", String.valueOf(data.getOrDefault("status", "")).toUpperCase());
        }} catch (Exception ex) {{
            r.put("mode", "live");
            r.put("error", ex.getMessage());
        }}
        return r;
    }}
}}
'''


JEEVAN_PRAMAAN_NODE = '''/** LAMA-injected integration: {label} ({integration_id}). */
import {{ Router, Request, Response }} from "express";
import {{ randomUUID }} from "crypto";
import axios from "axios";
import {{ maskId }} from "../_util";

const router = Router();
const env = (k: string, d = "") => {{ const v = process.env[k]; return (v === undefined || v === "") ? d : v; }};
const mode = () => (env("{gate_env}", "mock") || "mock").toLowerCase();
const isLive = () => mode() === "live";

router.post("/dlc/generate", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const aadhaar = String(req.body?.aadhaar || "");
  const base_resp: any = {{ request_id: rid, aadhaar_masked: maskId(aadhaar, 4) }};
  if (!isLive()) {{
    return res.json({{ ...base_resp, mode: "mock", generated: true,
      pramaan_id: "JP-MOCK-" + rid.toUpperCase(),
      issued_on: new Date().toISOString().slice(0, 10),
      valid_until: "2027-06-12" }});
  }}
  const base = env("JEEVAN_PRAMAAN_BASE_URL"); const key = env("JEEVAN_PRAMAAN_API_KEY");
  if (!base || !key) {{
    return res.json({{ ...base_resp, mode: "live", generated: false,
      error: "JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset" }});
  }}
  try {{
    const r = await axios.post(`${{base.replace(/\\/$$/, "")}}/api/v1/dlc/generate`, {{
      aadhaar, pdoCode: req.body?.pension_pdo_code || "",
      ppoNumber: req.body?.ppo_number || "",
      biometric: req.body?.biometric_b64 || "",
    }}, {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}`, "Content-Type": "application/json" }} }});
    return res.json({{ ...base_resp, mode: "live", generated: true,
      pramaan_id: r.data?.pramaanId || "",
      issued_on: r.data?.issuedOn || "",
      valid_until: r.data?.validUntil || "" }});
  }} catch (e: any) {{
    return res.json({{ ...base_resp, mode: "live", generated: false,
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

router.get("/dlc/:pramaan_id", async (req: Request, res: Response) => {{
  const rid = randomUUID().substring(0, 12);
  const id = req.params.pramaan_id;
  if (!isLive()) {{
    return res.json({{ request_id: rid, pramaan_id: id, mode: "mock",
      aadhaar_masked: "********1234", pensioner_name: "RAMESH KUMAR",
      pension_pdo_code: "MOCK-PDO-001", ppo_number: "PPO/2020/12345",
      issued_on: "2026-01-15", valid_until: "2027-01-15", status: "ACTIVE" }});
  }}
  const base = env("JEEVAN_PRAMAAN_BASE_URL"); const key = env("JEEVAN_PRAMAAN_API_KEY");
  if (!base || !key) {{
    return res.json({{ request_id: rid, pramaan_id: id, mode: "live",
      error: "JEEVAN_PRAMAAN_BASE_URL / JEEVAN_PRAMAAN_API_KEY unset" }});
  }}
  try {{
    const r = await axios.get(`${{base.replace(/\\/$$/, "")}}/api/v1/dlc/${{id}}`,
      {{ timeout: 30000, headers: {{ Authorization: `Bearer ${{key}}` }} }});
    return res.json({{ request_id: rid, pramaan_id: id, mode: "live",
      aadhaar_masked: r.data?.aadhaarMasked || "",
      pensioner_name: r.data?.name || "",
      pension_pdo_code: r.data?.pdoCode || "", ppo_number: r.data?.ppoNumber || "",
      issued_on: r.data?.issuedOn || "", valid_until: r.data?.validUntil || "",
      status: String(r.data?.status || "").toUpperCase() }});
  }} catch (e: any) {{
    return res.json({{ request_id: rid, pramaan_id: id, mode: "live",
      error: e?.response ? `Provider HTTP ${{e.response.status}}` : (e?.message || String(e)) }});
  }}
}});

export default router;
'''


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

DPG_INDIA_JAVA_IMPLS = {
    "bharatkosh_ntrp": ("BharatkoshNtrpController", BHARATKOSH_JAVA),
    "pfms":            ("PfmsController",           PFMS_JAVA),
    "api_setu":        ("ApiSetuController",        API_SETU_JAVA),
    "bhashini":        ("BhashiniController",       BHASHINI_JAVA),
    "abdm":            ("AbdmController",           ABDM_JAVA),
    "jeevan_pramaan":  ("JeevanPramaanController",  JEEVAN_PRAMAAN_JAVA),
}

DPG_INDIA_NODE_IMPLS = {
    "bharatkosh_ntrp": BHARATKOSH_NODE,
    "pfms":            PFMS_NODE,
    "api_setu":        API_SETU_NODE,
    "bhashini":        BHASHINI_NODE,
    "abdm":            ABDM_NODE,
    "jeevan_pramaan":  JEEVAN_PRAMAAN_NODE,
}


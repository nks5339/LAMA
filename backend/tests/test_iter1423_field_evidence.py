"""iter-14.23 — Field-level evidence extraction + ROUTE enrichment.

iter-14.22 gave CEOTS 170 ROUTE entities but NO field-level detail, so
CodeGen saw routes with empty request/response shapes and (per the
anti-hallucination contract) emitted `⚠ EVIDENCE GAP` markers → empty DTO
records → confidence collapse to 55%.

These tests pin the join that was missing:
  • Struts 1 ActionForm subclass  → FORM_FIELD entities
  • struts-config form-bean        → ROUTE.request_fields (resolved)
  • web.xml security-constraint    → ROUTE.roles
  • Spring @RequestBody / ResponseEntity<> → request/response_fields
  • the inline TOON evidence block the codegen prompt carries
  • the FIELD-EVIDENCE CONTRACT prompt clause (codegen.service + gap_recovery)
  • the post-generation validator penalising gap markers so the confidence
    loop selects the file for regeneration
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from kb.owl_extractor import extract_java, extract_xml, enrich_routes  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 1. Struts ActionForm field extraction
# ─────────────────────────────────────────────────────────────────────

ACCOUNTS_FORM = """\
package com.tcs.accounts;

import javax.validation.constraints.NotNull;
import org.apache.struts.action.ActionForm;

public class AccountsMasterForm extends ActionForm {
    @NotNull
    private String accountId;
    private java.math.BigDecimal amount;
    private java.util.Date postingDate;
    private String remarks;

    public String getAccountId() { return accountId; }
    public void setAccountId(String v) { this.accountId = v; }
    public java.math.BigDecimal getAmount() { return amount; }
    public void setAmount(java.math.BigDecimal v) { this.amount = v; }
}
"""


def test_actionform_emits_four_form_fields_with_types():
    ents = extract_java(ACCOUNTS_FORM, "AccountsMasterForm.java")
    ff = [e for e in ents if e["type"] == "FORM_FIELD"]
    assert len(ff) == 4, [f["name"] for f in ff]
    by_name = {f["name"]: f for f in ff}
    assert by_name["accountId"]["java_type"] == "String"
    assert by_name["amount"]["java_type"] == "BigDecimal"
    assert by_name["postingDate"]["java_type"] == "Date"
    assert by_name["remarks"]["java_type"] == "String"
    # owner_class carries the FQN so it can join to the form-bean type.
    assert all(f["owner_class"] == "com.tcs.accounts.AccountsMasterForm" for f in ff)
    # @NotNull → required
    assert by_name["accountId"]["required"] is True
    assert by_name["amount"]["required"] is False


# ─────────────────────────────────────────────────────────────────────
# 2. struts-config form-bean → ROUTE.request_fields
# ─────────────────────────────────────────────────────────────────────

STRUTS_CONFIG = """\
<struts-config>
  <form-beans>
    <form-bean name="accountsMasterForm" type="com.tcs.accounts.AccountsMasterForm"/>
  </form-beans>
  <action-mappings>
    <action path="/AccountsMaster" type="com.tcs.accounts.AccountsMasterAction"
            name="accountsMasterForm" scope="request" validate="true">
      <forward name="success" path="/jsp/accounts/postSuccess.jsp"/>
    </action>
  </action-mappings>
</struts-config>
"""


def test_struts_route_gets_request_fields_from_form_bean():
    ents = extract_java(ACCOUNTS_FORM, "AccountsMasterForm.java")
    ents += extract_xml(STRUTS_CONFIG, "struts-config.xml")
    enrich_routes(ents)
    routes = [e for e in ents
              if e["type"] == "ROUTE" and e.get("framework") == "struts1"
              and e["name"] == "/AccountsMaster"]
    assert routes, "struts route not emitted"
    r = routes[0]
    assert r.get("form_bean_class") == "com.tcs.accounts.AccountsMasterForm"
    names = [f["name"] for f in r.get("request_fields", [])]
    assert names == ["accountId", "amount", "postingDate", "remarks"], names
    # target_view resolved from the success forward
    assert r.get("target_view") == "/jsp/accounts/postSuccess.jsp"


# ─────────────────────────────────────────────────────────────────────
# 3. web.xml security-constraint → ROUTE.roles
# ─────────────────────────────────────────────────────────────────────

WEB_XML = """\
<web-app>
  <security-role><role-name>ACCOUNTS_ADMIN</role-name></security-role>
  <security-constraint>
    <web-resource-collection>
      <web-resource-name>Accounts</web-resource-name>
      <url-pattern>/AccountsMaster.do</url-pattern>
    </web-resource-collection>
    <auth-constraint>
      <role-name>ACCOUNTS_ADMIN</role-name>
    </auth-constraint>
  </security-constraint>
</web-app>
"""


def test_webxml_security_constraint_tags_route_roles():
    wents = extract_xml(WEB_XML, "web.xml")
    # ROLE + SECURITY_CONSTRAINT both surface
    assert any(e["type"] == "ROLE" and e["name"] == "ACCOUNTS_ADMIN" for e in wents)
    sc = [e for e in wents if e["type"] == "SECURITY_CONSTRAINT"]
    assert sc and sc[0]["roles"] == ["ACCOUNTS_ADMIN"]
    assert sc[0]["url_patterns"] == ["/AccountsMaster.do"]

    ents = extract_xml(STRUTS_CONFIG, "struts-config.xml") + wents
    enrich_routes(ents)
    route = next(e for e in ents
                 if e["type"] == "ROUTE" and e.get("framework") == "struts1"
                 and e["name"] == "/AccountsMaster")
    # /AccountsMaster (struts path) matches url-pattern /AccountsMaster.do
    assert route.get("roles") == ["ACCOUNTS_ADMIN"], route.get("roles")


# ─────────────────────────────────────────────────────────────────────
# 4. Spring @RequestBody + ResponseEntity<> resolution
# ─────────────────────────────────────────────────────────────────────

SPRING_CTRL = '@RestController public class Foo { @PostMapping("/x") public ResponseEntity<Bar> h(@RequestBody Baz b){return null;} }'
BAZ = "public class Baz { public int a; public String b; }"
BAR = "public class Bar { public boolean ok; }"


def test_spring_request_and_response_fields_resolved():
    ents = (extract_java(SPRING_CTRL, "Foo.java")
            + extract_java(BAZ, "Baz.java")
            + extract_java(BAR, "Bar.java"))
    enrich_routes(ents)
    route = next(e for e in ents if e["type"] == "ROUTE" and e["name"] == "/x")
    assert route.get("request_body_class") == "Baz"
    assert route.get("response_body_class") == "Bar"
    req = [f["name"] for f in route.get("request_fields", [])]
    resp = [f["name"] for f in route.get("response_fields", [])]
    assert req == ["a", "b"], req
    assert resp == ["ok"], resp


# ─────────────────────────────────────────────────────────────────────
# 5. Inline TOON evidence block the codegen prompt carries
# ─────────────────────────────────────────────────────────────────────

def test_render_route_field_toon_inline_block():
    from routes.codegen import _render_route_field_toon
    ents = extract_java(ACCOUNTS_FORM, "AccountsMasterForm.java")
    ents += extract_xml(STRUTS_CONFIG, "struts-config.xml")
    ents += extract_xml(WEB_XML, "web.xml")
    enrich_routes(ents)
    routes = [e for e in ents if e["type"] == "ROUTE" and e.get("framework") == "struts1"]
    block = _render_route_field_toon(routes)
    assert "ROUTE FIELD EVIDENCE" not in block  # label added by caller, not renderer
    assert "COLUMN TYPE MAP" in block
    assert "form_bean_class: com.tcs.accounts.AccountsMasterForm" in block
    assert "request_fields:" in block
    for fld in ("accountId", "amount", "postingDate", "remarks"):
        assert f"- {fld}:" in block, fld
    assert "roles: [ACCOUNTS_ADMIN]" in block
    assert "target_view: /jsp/accounts/postSuccess.jsp" in block


# ─────────────────────────────────────────────────────────────────────
# 6. FIELD-EVIDENCE CONTRACT prompt clause (seed rev-bump)
# ─────────────────────────────────────────────────────────────────────

def test_codegen_prompts_carry_field_evidence_contract():
    from seed import GLOBAL_PROMPTS
    by_key = {p["key"]: p for p in GLOBAL_PROMPTS if "key" in p and "template" in p}
    svc = by_key["codegen.service"]
    assert "FIELD-EVIDENCE CONTRACT" in svc["template"]
    assert "request_fields" in svc["template"]
    assert svc.get("force_update") is True
    assert "v8" in svc["description"]
    gap = by_key["codegen.gap_recovery"]
    assert "FIELD-EVIDENCE CONTRACT" in gap["template"]
    assert gap.get("force_update") is True


# ─────────────────────────────────────────────────────────────────────
# 7. Post-generation validator — gap markers penalise / flag for regen
# ─────────────────────────────────────────────────────────────────────

def test_evidence_gap_marker_penalises_parity_axis():
    from codegen.parity_loop import _score_parity
    clean = "public record AccountsDto(String accountId, java.math.BigDecimal amount) {}"
    gapped = clean + "\n// field spec\n> \u26a0 EVIDENCE GAP: amount not evidenced"
    s_clean = _score_parity(clean).score
    s_gap = _score_parity(gapped).score
    assert s_clean == 100.0
    assert s_gap < 95.0, s_gap  # drops below the 95 threshold → selected for regen


def test_enrich_routes_is_idempotent():
    ents = extract_java(ACCOUNTS_FORM, "AccountsMasterForm.java")
    ents += extract_xml(STRUTS_CONFIG, "struts-config.xml")
    enrich_routes(ents)
    r1 = next(e for e in ents if e["type"] == "ROUTE" and e.get("framework") == "struts1")
    first = list(r1.get("request_fields"))
    enrich_routes(ents)  # run again
    r2 = next(e for e in ents if e["type"] == "ROUTE" and e.get("framework") == "struts1")
    assert list(r2.get("request_fields")) == first

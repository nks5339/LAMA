"""iter-14.22 — Route extraction hardening for Struts 1 + Spring MVC + JSP.

The ceots project (Java / JSP / Struts 1 + Spring MVC / Oracle, 1202 Java +
487 JSP files) produced only 3 ROUTE entities pre-14.22 because:

  1. `JAVA_ANNOTATION_ROUTE_RE` picked up the FIRST string arg of a
     mapping annotation only, missed class-level `@RequestMapping("/x")`
     prefixes, and never combined class + method paths.
  2. JSP `<form action="…">` was emitted as JSP_FORM only, not as a
     synthetic ROUTE, so JSP-driven legacy flows were invisible to
     arch.recommend.
  3. Struts 1 ROUTE emission omitted `handler_class`, `handler_method`,
     `forwards`, `http_method` — enough for existence but not enough
     for the 1:1 API-parity enforcement now required by arch.recommend.

These tests pin the new behaviour."""
from kb.owl_extractor import extract_java, extract_jsp, extract_xml, extract_zip


# ---------- Spring MVC ----------

SPRING_CTRL = """\
package com.acme.user;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/users")
public class UserController {

    @GetMapping("/{id}")
    public User get(@PathVariable Long id) { return null; }

    @PostMapping
    public User create(@RequestBody User u) { return u; }

    @RequestMapping(value = "/search", method = RequestMethod.POST)
    public java.util.List<User> search(@RequestParam String q) { return null; }
}
"""


def test_spring_class_method_paths_combined():
    ents = extract_java(SPRING_CTRL, "UserController.java")
    routes = [e for e in ents if e.get("type") == "ROUTE"]
    paths = {(r["http_method"], r["name"]) for r in routes}
    assert ("GET", "/api/users/{id}") in paths
    assert ("POST", "/api/users") in paths
    assert ("POST", "/api/users/search") in paths
    # handler class + method attached
    r_search = next(r for r in routes if r["name"] == "/api/users/search")
    assert r_search["handler_class"] == "UserController"
    assert r_search["handler_method"] == "search"
    assert r_search["framework"] == "spring-mvc"


def test_spring_bare_request_mapping_class_only():
    src = """\
@Controller
@RequestMapping("/admin")
public class AdminController {
    @RequestMapping
    public String home() { return "index"; }
}
"""
    ents = extract_java(src, "AdminController.java")
    routes = [e for e in ents if e.get("type") == "ROUTE"]
    assert any(r["name"] == "/admin" for r in routes)


def test_non_controller_falls_back_to_legacy_scan():
    """@RequestMapping outside a @Controller (JAX-RS-ish helper) still fires."""
    src = """\
package com.acme;
public class Helper {
    @GetMapping("/util/ping")
    public String ping() { return "pong"; }
}
"""
    ents = extract_java(src, "Helper.java")
    routes = [e for e in ents if e.get("type") == "ROUTE"]
    assert any(r["name"] == "/util/ping" for r in routes)


# ---------- Struts 1 ----------

STRUTS_CFG = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE struts-config PUBLIC "-//Apache Software Foundation//DTD Struts Configuration 1.2//EN"
    "http://struts.apache.org/dtds/struts-config_1_2.dtd">
<struts-config>
  <form-beans>
    <form-bean name="loginForm" type="com.acme.login.LoginForm"/>
  </form-beans>
  <action-mappings>
    <action path="/login" type="com.acme.login.LoginAction" name="loginForm" scope="request" validate="true" input="/login.jsp">
      <forward name="success" path="/home.jsp"/>
      <forward name="failure" path="/login.jsp"/>
    </action>
    <action path="/logout" type="com.acme.login.LogoutAction"/>
  </action-mappings>
</struts-config>
"""


def test_struts_action_emits_route_with_forwards():
    ents = extract_xml(STRUTS_CFG, "struts-config.xml")
    routes = [e for e in ents if e.get("type") == "ROUTE" and e.get("framework") == "struts1"]
    paths = {r["name"] for r in routes}
    assert paths == {"/login", "/logout"}
    login = next(r for r in routes if r["name"] == "/login")
    assert login["handler_class"] == "com.acme.login.LoginAction"
    assert login["handler_method"] == "execute"
    assert login["form_bean"] == "loginForm"
    assert login["http_method"] == "GET+POST"
    fw_names = {f["name"] for f in login["forwards"]}
    assert {"success", "failure"} <= fw_names


# ---------- web.xml ----------

WEB_XML = """\
<web-app>
  <servlet>
    <servlet-name>report</servlet-name>
    <servlet-class>com.acme.report.ReportServlet</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>report</servlet-name>
    <url-pattern>/reports/*</url-pattern>
  </servlet-mapping>
</web-app>
"""


def test_webxml_servlet_mapping_emits_route_with_handler_class():
    ents = extract_xml(WEB_XML, "web.xml")
    routes = [e for e in ents if e.get("type") == "ROUTE" and e.get("framework") == "servlet"]
    assert len(routes) == 1
    r = routes[0]
    assert r["name"] == "/reports/*"
    assert r["handler_class"] == "com.acme.report.ReportServlet"
    assert r["servlet_name"] == "report"


# ---------- JSP ----------

JSP_PAGE = """\
<html><body>
  <form action="/user/save.do" method="post">
    <input name="uname"/>
  </form>
  <form action="#" method="get"><input/></form>
  <form action="javascript:void(0)"><input/></form>
  <form action="https://external.example/x"><input/></form>
</body></html>
"""


def test_jsp_form_emits_synthetic_route():
    ents = extract_jsp(JSP_PAGE, "user_form.jsp")
    forms = [e for e in ents if e.get("type") == "JSP_FORM"]
    routes = [e for e in ents if e.get("type") == "ROUTE"]
    # 4 form tags, 1 synthetic ROUTE (the .do action)
    assert len(forms) == 4
    assert len(routes) == 1
    assert routes[0]["name"] == "/user/save.do"
    assert routes[0]["http_method"] == "POST"
    assert routes[0]["framework"] == "jsp-form"


# ---------- Integration through extract_zip ----------

def test_zip_fixture_yields_many_routes():
    """Feed a mini Struts + Spring + JSP corpus and assert ≥ 5 ROUTEs."""
    blob = (
        "===== FILE: struts-config.xml =====\n" + STRUTS_CFG + "\n"
        "===== FILE: web.xml =====\n" + WEB_XML + "\n"
        "===== FILE: UserController.java =====\n" + SPRING_CTRL + "\n"
        "===== FILE: user_form.jsp =====\n" + JSP_PAGE + "\n"
    )
    ents = extract_zip(blob, "corpus.zip")
    routes = [e for e in ents if e.get("type") == "ROUTE"]
    frameworks = {r.get("framework") for r in routes}
    assert {"struts1", "servlet", "spring-mvc", "jsp-form"} <= frameworks
    assert len(routes) >= 7   # 2 struts + 1 servlet + 3 spring + 1 jsp


# ---------- Parity annotator ----------

def test_annotate_parity_full_coverage_and_missing():
    """The parity annotator scores each service on legacy → target endpoint
    coverage and stamps an aggregate `_parity` block."""
    from routes.architecture import _annotate_parity

    parsed = {
        "services": [
            {
                "name": "users",
                "routes_detail": [
                    {"verb": "GET",  "path": "/api/users/{id}"},
                    {"verb": "POST", "path": "/api/users"},
                ],
                "api_endpoints": ["GET /api/users/{id}", "POST /api/users"],
            },
            {
                "name": "orders",
                "routes_detail": [
                    {"verb": "GET",  "path": "/api/orders"},
                    {"verb": "POST", "path": "/api/orders"},
                ],
                # Missing POST → coverage 50%
                "api_endpoints": ["GET /api/orders"],
            },
        ],
    }
    agg = _annotate_parity(parsed)
    # per-service
    users = parsed["services"][0]["parity"]
    orders = parsed["services"][1]["parity"]
    assert users["coverage_pct"] == 100.0
    assert users["pass"] is True
    assert users["missing"] == []
    assert orders["coverage_pct"] == 50.0
    assert orders["pass"] is False
    assert orders["missing"] == ["POST /api/orders"]
    # aggregate
    assert agg["legacy"] == 4
    assert agg["target"] == 3
    assert agg["missing"] == 1
    assert agg["aggregate_coverage_pct"] == 75.0
    assert agg["pass"] is False


# ---------- Recommend prompt regression ----------

def test_arch_recommend_prompt_carries_parity_block():
    """iter-14.22 rev bump — the seed template MUST enumerate the 1:1
    parity constraint AND the sparse-routes fallback."""
    from seed import GLOBAL_PROMPTS

    row = next(p for p in GLOBAL_PROMPTS if p["key"] == "arch.recommend")
    tpl = row["template"]
    assert "API PARITY" in tpl
    assert "1:1 LEGACY → TARGET" in tpl or "1:1 legacy → target" in tpl.lower()
    assert "SPARSE-ROUTES FALLBACK" in tpl
    assert "No inventions" in tpl and "No drops" in tpl
    # `parity` block in the prompt uses YAML-ish `key: <int>` syntax with
    # doubled braces (Python .format() escapes) — assert on the plain
    # `parity` token + the `coverage_pct` field name that must appear in
    # the machine-enforcement block.
    assert "parity" in tpl
    assert "coverage_pct" in tpl
    # Force-reseed on next boot
    assert row.get("force_update") is True
    # Description reflects the bump
    assert "iter-14.22" in row["description"]

"""LAMA -- integration renderer + injector (iter-13.60).

Reads enabled integrations off `project_integrations`, renders a **complete,
runnable mini-project** scaffolded for the target language, and upserts the
resulting files into `codegen_files` so the new project ships inside the
existing ZIP-download / GitHub-push pipeline.

Output layout (one folder per generated service -- `integrations-service/`):

  python  -> main.py + requirements.txt + Dockerfile + README.md + .env.example
             + app/integrations/<id>/router.py     (full impl from catalog)
  java    -> pom.xml + Dockerfile + README.md + .env.example
             + src/main/java/com/lama/integrations/Application.java
             + src/main/java/com/lama/integrations/<Name>Controller.java
             + src/main/resources/application.yml
  nodejs  -> package.json + tsconfig.json + Dockerfile + README.md + .env.example
             + src/index.ts
             + src/integrations/<id>/router.ts

For Python the reference templates in `catalog.py` are used verbatim (mock +
live both fully implemented). For Java and Node the renderer emits
compilable, mock-by-default controllers/handlers with TODO markers pointing
at the Python reference -- the project boots end-to-end on all 3 targets.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import json
import uuid

from db import codegen_files, project_integrations, projects, audit_log
from integrations.catalog import CATALOG, get_entry
from integrations.live_impls import (
    JAVA_UTIL, JAVA_UTIL_PATH,
    NODE_UTIL, NODE_UTIL_PATH,
    render_java_controller, render_node_router,
)


SERVICE_DIR = "integrations-service"


_LANG_ALIASES = {
    "python": "python", "py": "python", "fastapi": "python", "django": "python", "flask": "python",
    "java": "java", "spring": "java", "spring boot": "java", "springboot": "java",
    "nodejs": "nodejs", "node": "nodejs", "node.js": "nodejs", "nestjs": "nodejs",
    "typescript": "nodejs", "ts": "nodejs", "express": "nodejs",
}


def resolve_backend_lang(target_tech: str) -> str:
    t = (target_tech or "").lower()
    for needle, lang in _LANG_ALIASES.items():
        if needle in t:
            return lang
    return "python"


def _is_middleware(entry: Dict[str, Any]) -> bool:
    """Catalog entries with kind=='middleware' are mounted as request
    middleware/aspects rather than as a router. Default kind is 'router'."""
    return (entry.get("kind") or "router").lower() == "middleware"


def _py_middleware_descriptor(entry: Dict[str, Any]) -> tuple:
    """(module_path, class_name) for `from <module> import <Class>`."""
    rel_path, _ = entry["file_templates"]["python"]
    if rel_path.endswith(".py"):
        rel_path = rel_path[:-3]
    module = rel_path.replace("/", ".")
    mw_meta = entry.get("middleware") or {}
    cls = mw_meta.get("python_class") or (
        "".join(part.capitalize() for part in entry["id"].split("_")) + "Middleware"
    )
    return module, cls


def _node_middleware_descriptor(entry: Dict[str, Any]) -> tuple:
    """(import_module, exported_name) for `import { name } from "<module>"`."""
    parts = entry["id"].split("_")
    mw_meta = entry.get("middleware") or {}
    name = mw_meta.get("node_export") or (
        parts[0] + "".join(p.capitalize() for p in parts[1:]) + "Middleware"
    )
    module = "./integrations/" + entry["id"] + "/router"
    return module, name


# ---------------------------------------------------------------------------
# Selection CRUD
# ---------------------------------------------------------------------------

async def list_selections(project_id: str) -> List[Dict[str, Any]]:
    selected = {}
    cursor = project_integrations.find({"project_id": project_id}, {"_id": 0})
    async for row in cursor:
        selected[row["integration_id"]] = row
    out = []
    for entry in CATALOG:
        sel = selected.get(entry["id"], {})
        out.append({
            **{k: v for k, v in entry.items() if k != "file_templates"},
            "enabled": bool(sel.get("enabled")),
            "config_overrides": sel.get("config_overrides", {}),
            "last_injected_at": sel.get("last_injected_at", ""),
            "last_injected_files": sel.get("last_injected_files", []),
        })
    return out


async def set_selection(project_id: str, integration_id: str, enabled: bool,
                        config_overrides: Dict[str, str] | None = None) -> Dict[str, Any]:
    entry = get_entry(integration_id)
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "integration_id": integration_id,
        "enabled": bool(enabled),
        "config_overrides": config_overrides or {},
        "updated_at": now,
    }
    existing = await project_integrations.find_one(
        {"project_id": project_id, "integration_id": integration_id}, {"_id": 0}
    )
    if existing:
        doc["id"] = existing["id"]
        doc["created_at"] = existing.get("created_at", now)
        doc["last_injected_at"] = existing.get("last_injected_at", "")
        doc["last_injected_files"] = existing.get("last_injected_files", [])
    else:
        doc["created_at"] = now
        doc["last_injected_at"] = ""
        doc["last_injected_files"] = []
    await project_integrations.update_one(
        {"project_id": project_id, "integration_id": integration_id},
        {"$set": doc}, upsert=True,
    )
    await audit_log.insert_one({
        "action": "integration.selection.update",
        "project_id": project_id,
        "at": now,
        "details": {"integration_id": integration_id, "enabled": enabled,
                    "label": entry["label"]},
    })
    return doc


# ---------------------------------------------------------------------------
# Shared assets -- .env.example + README
# ---------------------------------------------------------------------------

def _env_example_block(entries: List[Dict[str, Any]],
                       overrides_per_id: Dict[str, Dict[str, str]]) -> str:
    lines = [
        "# -------------------------------------------------------------",
        "# Govt-services integrations - generated by LAMA (iter-13.60).",
        "# Flip each <NAME>_MODE to `live` and supply real credentials to",
        "# switch from canned mock responses to real provider calls.",
        "# NO CODE CHANGES required - env vars are read at request time.",
        "# -------------------------------------------------------------",
        "PORT=8080",
        "CORS_ORIGINS=*",
        "",
    ]
    for entry in entries:
        lines.append("# " + entry["label"] + " (" + entry["id"] + ")  - providers: "
                     + ", ".join(entry.get("providers", [])))
        ov = overrides_per_id.get(entry["id"], {})
        for v in entry.get("env_vars", []):
            default = ov.get(v["name"], v.get("default", ""))
            lines.append(v["name"] + "=" + default + "  # " + v["desc"])
        lines.append("")
    return "\n".join(lines)


def _readme(entries: List[Dict[str, Any]], language: str) -> str:
    run_cmd = {
        "python": "pip install -r requirements.txt && uvicorn main:app --reload --port 8080",
        "java":   "mvn -B package && java -jar target/integrations-service.jar",
        "nodejs": "npm install && npm run build && npm start",
    }.get(language, "")
    row_lines = []
    for e in entries:
        eps = ", ".join("`" + ep["method"] + " " + ep["path"] + "`"
                        for ep in e.get("endpoints", []))
        row_lines.append(
            "| `" + e["id"] + "` | " + e["label"] + " | " + e["category"]
            + " | `" + e.get("gate_env", "") + "` | " + eps + " |"
        )
    rows = "\n".join(row_lines)
    py_note = ""
    if language != "python":
        py_note = (
            "\n> **Note** — every target (Python, Java, Node.js) now ships with\n"
            "> **both** mock and live-mode implementations of every integration.\n"
            "> Flip `<GATE>_MODE=live` in `.env` and supply the matching\n"
            "> `*_API_KEY` / `*_URL` env vars — no code changes required.\n"
            "> The Python implementation in `backend/integrations/catalog.py`\n"
            "> remains the canonical reference if you need to add a new provider.\n"
        )
    return (
        "# Govt-Services Integrations  -  " + language + " target\n\n"
        "Generated by **LAMA** (Legacy Application Modernization & Alignment) -\n"
        "iter-13.60. A standalone " + language + " micro-service that exposes Indian\n"
        "govt-service integrations behind a uniform **mock-first** API.\n\n"
        "## Quick start\n\n"
        "```sh\n"
        "cp .env.example .env             # then edit credentials\n"
        + run_cmd + "\n"
        "```\n\n"
        "Or via Docker:\n\n"
        "```sh\n"
        "docker build -t integrations-service .\n"
        "docker run --rm -p 8080:8080 --env-file .env integrations-service\n"
        "```\n\n"
        "Service comes up on **http://localhost:8080** - verify with:\n\n"
        "```sh\n"
        "curl http://localhost:8080/health\n"
        "curl http://localhost:8080/\n"
        "```\n\n"
        "## Enabled integrations\n\n"
        "| ID | Label | Category | Mode env-var | Endpoints |\n"
        "|---|---|---|---|---|\n"
        + rows + "\n\n"
        "## Mock -> Live\n\n"
        "Every integration ships with a `<NAME>_MODE` env var (see `.env.example`).\n\n"
        "* `<NAME>_MODE=mock` *(default)* - endpoints return canned responses so the\n"
        "  service is end-to-end callable without a single provider account.\n"
        "* `<NAME>_MODE=live` - endpoints call the real provider; supply the\n"
        "  matching `*_API_KEY` / `*_URL` env vars in `.env`.\n\n"
        "There are **no code changes** between mock and live - switching is purely\n"
        "configuration. This is the contract LAMA enforces on every injected\n"
        "integration.\n"
        + py_note +
        "\n## Security notes (DPDP Act 2023)\n\n"
        "* Raw PAN / Aadhaar / OTP / KYC payloads are **never logged**. Identifiers\n"
        "  are masked for display and hashed (SHA-256) for audit trails.\n"
        "* Provider credentials live in `.env` (excluded from git via `.gitignore`).\n"
        "* TLS verification can be tuned via `LAMA_DISABLE_SSL_VERIFY` /\n"
        "  `LAMA_CA_BUNDLE` for corporate MITM proxies (Zscaler / Netskope).\n"
    )


# ---------------------------------------------------------------------------
# Python scaffold
# ---------------------------------------------------------------------------

def _python_scaffold(entries: List[Dict[str, Any]], env_block: str) -> List[Tuple[str, str, str]]:
    router_entries = [e for e in entries if not _is_middleware(e)]
    middleware_entries = [e for e in entries if _is_middleware(e)]

    import_lines = []
    include_lines = []
    for e in router_entries:
        var = e["id"] + "_router"
        import_lines.append("from app.integrations." + e["id"] + ".router import router as " + var)
        include_lines.append("app.include_router(" + var + ")")

    mw_import_lines = []
    mw_add_lines = []
    for e in middleware_entries:
        module, cls = _py_middleware_descriptor(e)
        mw_import_lines.append("from " + module + " import " + cls)
        mw_add_lines.append("app.add_middleware(" + cls + ")")

    imports_block = "\n".join(import_lines + mw_import_lines)
    includes_block = "\n".join(include_lines) if include_lines else "# (no routers enabled)"
    middleware_block = ("\n".join(mw_add_lines) + "\n\n") if mw_add_lines else ""
    enabled_ids_repr = json.dumps([e["id"] for e in entries])

    main_py = (
        '"""Govt-services integration micro-service - generated by LAMA (iter-13.60)."""\n'
        "import logging\n"
        "import os\n"
        "from typing import Any, Dict\n\n"
        "from dotenv import load_dotenv\n"
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.cors import CORSMiddleware\n\n"
        "load_dotenv()\n\n"
        + imports_block + "\n\n"
        "logging.basicConfig(\n"
        '    level=os.getenv("LOG_LEVEL", "INFO").upper(),\n'
        '    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",\n'
        ")\n\n"
        "app = FastAPI(\n"
        '    title="Govt-Services Integrations",\n'
        '    version="1.0.0",\n'
        '    description="LAMA-generated mock-first facade for Indian govt services.",\n'
        ")\n\n"
        "app.add_middleware(\n"
        "    CORSMiddleware,\n"
        '    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),\n'
        "    allow_credentials=True,\n"
        '    allow_methods=["*"],\n'
        '    allow_headers=["*"],\n'
        ")\n\n"
        + middleware_block
        + includes_block + "\n\n"
        "_INTEGRATIONS = " + enabled_ids_repr + "\n\n\n"
        '@app.get("/")\n'
        "def root() -> Dict[str, Any]:\n"
        "    return {\n"
        '        "service": "integrations",\n'
        '        "status": "ok",\n'
        '        "integrations": _INTEGRATIONS,\n'
        '        "docs": "/docs",\n'
        "    }\n\n\n"
        '@app.get("/health")\n'
        "def health() -> Dict[str, Any]:\n"
        '    return {"ok": True}\n\n\n'
        'if __name__ == "__main__":\n'
        "    import uvicorn\n"
        '    uvicorn.run("main:app", host="0.0.0.0",\n'
        '                port=int(os.getenv("PORT", "8080")), reload=False)\n'
    )

    requirements_txt = (
        "fastapi==0.110.0\n"
        "uvicorn[standard]==0.27.0\n"
        "httpx==0.28.0\n"
        "pydantic==2.13.0\n"
        "python-dotenv==1.0.0\n"
    )
    dockerfile = (
        "FROM python:3.11-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n"
        "EXPOSE 8080\n"
        'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]\n'
    )
    gitignore = "__pycache__/\n*.pyc\n.env\n.venv/\nvenv/\n.idea/\n.vscode/\n*.egg-info/\ndist/\nbuild/\n"

    out: List[Tuple[str, str, str]] = [
        ("main.py", main_py, "python"),
        ("requirements.txt", requirements_txt, "text"),
        ("Dockerfile", dockerfile, "dockerfile"),
        ("README.md", _readme(entries, "python"), "markdown"),
        (".env.example", env_block, "bash"),
        (".gitignore", gitignore, "text"),
        ("app/__init__.py", "", "python"),
        ("app/integrations/__init__.py", "", "python"),
    ]
    for e in entries:
        out.append(("app/integrations/" + e["id"] + "/__init__.py", "", "python"))
    return out


def _python_integration_files(entries: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for entry in entries:
        templates = entry.get("file_templates", {})
        if "python" not in templates:
            continue
        rel_path, body = templates["python"]
        env_doc = "\n".join("  - " + v["name"].ljust(26) + " " + v["desc"]
                            for v in entry.get("env_vars", []))
        rendered = body.format(
            integration_id=entry["id"],
            label=entry["label"],
            gate_env=entry.get("gate_env", "MODE"),
            route_prefix=entry.get("route_prefix", "/integrations/" + entry["id"]),
            env_doc=env_doc or "  (no env vars)",
        )
        out.append((rel_path, rendered, "python"))
    return out


# ---------------------------------------------------------------------------
# Java (Spring Boot 3) scaffold
# ---------------------------------------------------------------------------

def _java_class_name(integration_id: str) -> str:
    return "".join(p.capitalize() for p in integration_id.split("_")) + "Controller"


def _java_scaffold(entries: List[Dict[str, Any]], env_block: str) -> List[Tuple[str, str, str]]:
    extra_deps = ""
    if any(_is_middleware(e) for e in entries):
        extra_deps = (
            "        <dependency>\n"
            "            <groupId>org.springframework.boot</groupId>\n"
            "            <artifactId>spring-boot-starter-aop</artifactId>\n"
            "        </dependency>\n"
        )
    pom = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
        "    <modelVersion>4.0.0</modelVersion>\n"
        "    <parent>\n"
        "        <groupId>org.springframework.boot</groupId>\n"
        "        <artifactId>spring-boot-starter-parent</artifactId>\n"
        "        <version>3.2.5</version>\n"
        "        <relativePath/>\n"
        "    </parent>\n"
        "    <groupId>com.lama</groupId>\n"
        "    <artifactId>integrations-service</artifactId>\n"
        "    <version>1.0.0</version>\n"
        "    <name>Govt-Services Integrations</name>\n"
        "    <properties>\n"
        "        <java.version>17</java.version>\n"
        "    </properties>\n"
        "    <dependencies>\n"
        "        <dependency>\n"
        "            <groupId>org.springframework.boot</groupId>\n"
        "            <artifactId>spring-boot-starter-web</artifactId>\n"
        "        </dependency>\n"
        "        <dependency>\n"
        "            <groupId>org.springframework.boot</groupId>\n"
        "            <artifactId>spring-boot-starter-actuator</artifactId>\n"
        "        </dependency>\n"
        + extra_deps +
        "    </dependencies>\n"
        "    <build>\n"
        "        <finalName>integrations-service</finalName>\n"
        "        <plugins>\n"
        "            <plugin>\n"
        "                <groupId>org.springframework.boot</groupId>\n"
        "                <artifactId>spring-boot-maven-plugin</artifactId>\n"
        "            </plugin>\n"
        "        </plugins>\n"
        "    </build>\n"
        "</project>\n"
    )

    ids_list = ",\n        ".join('"' + e["id"] + '"' for e in entries)
    application_java = (
        "package com.lama.integrations;\n\n"
        "import org.springframework.boot.SpringApplication;\n"
        "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
        "import org.springframework.web.bind.annotation.GetMapping;\n"
        "import org.springframework.web.bind.annotation.RestController;\n\n"
        "import java.util.List;\n"
        "import java.util.Map;\n\n"
        "@SpringBootApplication\n"
        "public class Application {\n\n"
        "    public static final List<String> ENABLED_INTEGRATIONS = List.of(\n"
        "        " + ids_list + "\n"
        "    );\n\n"
        "    public static void main(String[] args) {\n"
        "        SpringApplication.run(Application.class, args);\n"
        "    }\n\n"
        "    @RestController\n"
        "    static class RootController {\n"
        '        @GetMapping("/")\n'
        "        public Map<String, Object> root() {\n"
        "            return Map.of(\n"
        '                "service", "integrations",\n'
        '                "status", "ok",\n'
        '                "integrations", ENABLED_INTEGRATIONS\n'
        "            );\n"
        "        }\n\n"
        '        @GetMapping("/health")\n'
        "        public Map<String, Object> health() {\n"
        '            return Map.of("ok", true);\n'
        "        }\n"
        "    }\n"
        "}\n"
    )

    application_yml = (
        "server:\n"
        "  port: ${PORT:8080}\n\n"
        "management:\n"
        "  endpoints:\n"
        "    web:\n"
        "      exposure:\n"
        "        include: health,info\n"
    )

    dockerfile = (
        "FROM eclipse-temurin:17-jdk-alpine AS build\n"
        "WORKDIR /app\n"
        "RUN apk add --no-cache maven\n"
        "COPY pom.xml .\n"
        "COPY src ./src\n"
        "RUN mvn -B -q -DskipTests package\n\n"
        "FROM eclipse-temurin:17-jre-alpine\n"
        "WORKDIR /app\n"
        "COPY --from=build /app/target/integrations-service.jar app.jar\n"
        "EXPOSE 8080\n"
        'ENTRYPOINT ["java", "-jar", "app.jar"]\n'
    )

    gitignore = "target/\n*.class\n.env\n.idea/\n.vscode/\n*.iml\n.mvn/\n"

    out: List[Tuple[str, str, str]] = [
        ("pom.xml", pom, "xml"),
        ("Dockerfile", dockerfile, "dockerfile"),
        ("README.md", _readme(entries, "java"), "markdown"),
        (".env.example", env_block, "bash"),
        (".gitignore", gitignore, "text"),
        ("src/main/resources/application.yml", application_yml, "yaml"),
        ("src/main/java/com/lama/integrations/Application.java", application_java, "java"),
        (JAVA_UTIL_PATH, JAVA_UTIL, "java"),
    ]
    for e in entries:
        live = render_java_controller(e)
        if live is not None:
            out.append(live)
        else:
            out.append(_java_controller(e))
    return out


def _java_controller(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    cls = _java_class_name(entry["id"])
    prefix = entry.get("route_prefix", "/integrations/" + entry["id"])
    gate = entry.get("gate_env", "MODE")
    endpoints = entry.get("endpoints", [])
    endpoints_doc = ", ".join(ep["method"] + " " + ep["path"] for ep in endpoints)

    mapping_annos = {
        "GET":    "@GetMapping",
        "POST":   "@PostMapping",
        "PUT":    "@PutMapping",
        "DELETE": "@DeleteMapping",
        "PATCH":  "@PatchMapping",
    }

    used = set()
    handler_lines: List[str] = []
    for idx, ep in enumerate(endpoints):
        ep_method = (ep.get("method") or "GET").upper()
        ep_path = ep.get("path", prefix)
        sub_path = ep_path[len(prefix):] if ep_path.startswith(prefix) else ep_path
        if not sub_path:
            sub_path = "/"
        base = "".join(c if c.isalnum() else "_" for c in sub_path.strip("/")) or ("handle" + str(idx))
        base = base.lower().lstrip("_") or ("handle" + str(idx))
        name = base
        n = 1
        while name in used:
            n += 1
            name = base + str(n)
        used.add(name)

        anno = mapping_annos.get(ep_method, "@RequestMapping")
        path_vars: List[str] = []
        for token in sub_path.split("/"):
            if token.startswith("{") and token.endswith("}"):
                path_vars.append(token[1:-1])

        params_parts = ['@PathVariable("' + v + '") String ' + v for v in path_vars]
        if ep_method in ("POST", "PUT", "PATCH"):
            params_parts.append("@RequestBody(required = false) Map<String, Object> body")
        params_decl = ", ".join(params_parts)

        body_extras = ""  # noqa: F841 - legacy local, kept for diff readability


        upstream_env = entry["id"].upper() + "_UPSTREAM_URL"
        api_key_env  = entry["id"].upper() + "_API_KEY"
        auth_hdr_env = entry["id"].upper() + "_AUTH_HEADER"
        has_body = ep_method in ("POST", "PUT", "PATCH")
        body_publisher = ('java.net.http.HttpRequest.BodyPublishers.ofString('
                          'body == null ? "{}" : MAPPER.writeValueAsString(body))') if has_body else \
                         'java.net.http.HttpRequest.BodyPublishers.noBody()'
        url_build_path = sub_path
        for v in path_vars:
            url_build_path = url_build_path.replace("{" + v + "}", '" + java.net.URLEncoder.encode(' + v + ', java.nio.charset.StandardCharsets.UTF_8) + "')
        path_var_extras = "".join('\n            resp.put("' + v + '", ' + v + ");" for v in path_vars)
        mock_body_extras = ('\n            if (body != null) resp.put("echo", body);') if has_body else ""

        handler = (
            "    " + anno + '("' + sub_path + '")\n'
            "    public Map<String, Object> " + name + "(" + params_decl + ") {\n"
            "        String requestId = java.util.UUID.randomUUID().toString().substring(0, 12);\n"
            "        Map<String, Object> resp = new java.util.LinkedHashMap<>();\n"
            '        resp.put("integration", "' + entry["id"] + '");\n'
            '        resp.put("label", "' + entry["label"].replace('"', '\\"') + '");\n'
            '        resp.put("endpoint", "' + ep_method + " " + ep_path + '");\n'
            '        resp.put("request_id", requestId);\n'
            '        boolean live = "live".equalsIgnoreCase(mode);\n'
            '        resp.put("mode", live ? "live" : "mock");\n'
            "        if (!live) {\n"
            '            resp.put("status", "OK");\n'
            '            resp.put("note", "Canned mock response - set ' + gate + '=live and supply ' + upstream_env + ' / ' + api_key_env + ' to call the real provider.");'
            + mock_body_extras + path_var_extras + "\n"
            "            return resp;\n"
            "        }\n"
            "        // Live mode - generic upstream HTTP proxy. Configure via env:\n"
            "        //   " + upstream_env + "  - base URL (e.g. https://api.provider.gov.in)\n"
            "        //   " + api_key_env + "  - API key / bearer token (optional)\n"
            "        //   " + auth_hdr_env + " - header name for the key (default: Authorization). For Bearer prefix, prefix the value with 'Bearer '.\n"
            '        String upstream = com.lama.integrations.IntegrationUtil.env("' + upstream_env + '", "");\n'
            '        if (upstream == null || upstream.isEmpty()) {\n'
            '            resp.put("status", "MISCONFIGURED");\n'
            '            resp.put("error", "' + upstream_env + ' is not set");\n'
            '            return resp;\n'
            "        }\n"
            "        try {\n"
            '            String path = "' + url_build_path + '";\n'
            '            String url = upstream.replaceAll("/+$", "") + path;\n'
            "            java.net.http.HttpRequest.Builder rb = java.net.http.HttpRequest.newBuilder()\n"
            "                .uri(java.net.URI.create(url))\n"
            '                .header("Content-Type", "application/json")\n'
            '                .header("Accept", "application/json")\n'
            '                .header("X-Request-Id", requestId)\n'
            "                .timeout(java.time.Duration.ofSeconds(30))\n"
            '                .method("' + ep_method + '", ' + body_publisher + ');\n'
            '            String key = com.lama.integrations.IntegrationUtil.env("' + api_key_env + '", "");\n'
            "            if (!key.isEmpty()) {\n"
            '                String hdrName = com.lama.integrations.IntegrationUtil.env("' + auth_hdr_env + '", "Authorization");\n'
            "                rb.header(hdrName, key);\n"
            "            }\n"
            "            java.net.http.HttpResponse<String> upstreamResp = com.lama.integrations.IntegrationUtil.httpClient()\n"
            "                .send(rb.build(), java.net.http.HttpResponse.BodyHandlers.ofString());\n"
            '            resp.put("status", upstreamResp.statusCode());\n'
            '            resp.put("upstream_url", url);\n'
            "            String b = upstreamResp.body() == null ? \"\" : upstreamResp.body();\n"
            '            String ct = upstreamResp.headers().firstValue("content-type").orElse("");\n'
            '            if (ct.contains("application/json") && !b.isEmpty()) {\n'
            "                try {\n"
            '                    resp.put("data", MAPPER.readValue(b, Object.class));\n'
            "                } catch (Exception je) {\n"
            '                    resp.put("data", b);\n'
            "                }\n"
            "            } else {\n"
            '                resp.put("data", b);\n'
            "            }\n"
            "        } catch (Exception ex) {\n"
            '            resp.put("status", "UPSTREAM_ERROR");\n'
            '            resp.put("error", ex.getClass().getSimpleName() + ": " + ex.getMessage());\n'
            "        }" + path_var_extras + "\n"
            "        return resp;\n"
            "    }\n"
        )
        handler_lines.append(handler)

    handler_block = "\n".join(handler_lines) if handler_lines else "    // No endpoints declared in the catalog entry."

    body = (
        "package com.lama.integrations;\n\n"
        "import com.fasterxml.jackson.databind.ObjectMapper;\n"
        "import org.springframework.beans.factory.annotation.Value;\n"
        "import org.springframework.web.bind.annotation.*;\n\n"
        "import java.util.Map;\n\n"
        "/**\n"
        " * LAMA-injected integration: " + entry["label"] + " (" + entry["id"] + ").\n"
        " * Category: " + entry.get("category", "") + " - Providers: " + ", ".join(entry.get("providers", [])) + ".\n"
        " * Endpoints: " + endpoints_doc + "\n"
        " *\n"
        " * Live mode is a generic upstream HTTP proxy driven by env vars\n"
        " * (`<ID>_UPSTREAM_URL`, `<ID>_API_KEY`, `<ID>_AUTH_HEADER`). Mock mode is\n"
        " * the default - flip " + gate + "=live to forward calls to the real provider.\n"
        " */\n"
        "@RestController\n"
        '@RequestMapping("' + prefix + '")\n'
        "public class " + cls + " {\n\n"
        "    private static final ObjectMapper MAPPER = new ObjectMapper();\n\n"
        '    @Value("${' + gate + ':mock}")\n'
        "    private String mode;\n\n"
        + handler_block + "\n"
        "}\n"
    )
    return ("src/main/java/com/lama/integrations/" + cls + ".java", body, "java")


# ---------------------------------------------------------------------------
# Node.js (Express + TypeScript) scaffold
# ---------------------------------------------------------------------------

def _node_var_name(integration_id: str) -> str:
    parts = integration_id.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:]) + "Router"


def _node_scaffold(entries: List[Dict[str, Any]], env_block: str) -> List[Tuple[str, str, str]]:
    pkg = {
        "name": "integrations-service",
        "version": "1.0.0",
        "description": "Govt-services integrations facade - generated by LAMA (iter-13.60).",
        "main": "dist/index.js",
        "scripts": {
            "build": "tsc",
            "start": "node dist/index.js",
            "dev": "ts-node-dev --respawn --transpile-only src/index.ts",
        },
        "dependencies": {
            "express": "^4.18.2",
            "dotenv": "^16.3.1",
            "axios": "^1.6.2",
        },
        "devDependencies": {
            "typescript": "^5.3.2",
            "@types/node": "^20.10.0",
            "@types/express": "^4.17.21",
            "ts-node-dev": "^2.0.0",
        },
    }
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "commonjs",
            "outDir": "dist",
            "rootDir": "src",
            "esModuleInterop": True,
            "strict": True,
            "skipLibCheck": True,
            "resolveJsonModule": True,
        },
        "include": ["src/**/*"],
    }

    import_lines = []
    mount_lines = []
    mw_import_lines = []
    mw_use_lines = []
    for e in entries:
        if _is_middleware(e):
            module, name = _node_middleware_descriptor(e)
            mw_import_lines.append('import { ' + name + ' } from "' + module + '";')
            mw_use_lines.append("app.use(" + name + ");")
            continue
        var = _node_var_name(e["id"])
        import_lines.append('import ' + var + ' from "./integrations/' + e["id"] + '/router";')
        prefix = e.get("route_prefix", "/integrations/" + e["id"])
        mount_lines.append('app.use("' + prefix + '", ' + var + ");")
    imports_block = "\n".join(import_lines + mw_import_lines)
    mw_block = ("\n".join(mw_use_lines) + "\n\n") if mw_use_lines else ""
    mounts_block = "\n".join(mount_lines) if mount_lines else "// (no routers enabled)"
    enabled_ids_repr = json.dumps([e["id"] for e in entries])

    index_ts = (
        'import express, { Request, Response } from "express";\n'
        'import dotenv from "dotenv";\n'
        + imports_block + "\n\n"
        "dotenv.config();\n\n"
        "const app = express();\n"
        "app.use(express.json());\n\n"
        + mw_block
        + mounts_block + "\n\n"
        'app.get("/", (_req: Request, res: Response) => {\n'
        "  res.json({\n"
        '    service: "integrations",\n'
        '    status: "ok",\n'
        "    integrations: " + enabled_ids_repr + ",\n"
        "  });\n"
        "});\n\n"
        'app.get("/health", (_req: Request, res: Response) => res.json({ ok: true }));\n\n'
        'const port = parseInt(process.env.PORT || "8080", 10);\n'
        "app.listen(port, () => {\n"
        "  // eslint-disable-next-line no-console\n"
        "  console.log(`[integrations-service] listening on :${port}`);\n"
        "});\n"
    )

    dockerfile = (
        "FROM node:20-alpine AS build\n"
        "WORKDIR /app\n"
        "COPY package.json tsconfig.json ./\n"
        "RUN npm install\n"
        "COPY src ./src\n"
        "RUN npm run build\n\n"
        "FROM node:20-alpine\n"
        "WORKDIR /app\n"
        "COPY --from=build /app/package.json ./\n"
        "COPY --from=build /app/dist ./dist\n"
        "RUN npm install --omit=dev\n"
        "EXPOSE 8080\n"
        'CMD ["node", "dist/index.js"]\n'
    )
    gitignore = "node_modules/\ndist/\n.env\n.idea/\n.vscode/\n*.log\n"

    out: List[Tuple[str, str, str]] = [
        ("package.json", json.dumps(pkg, indent=2) + "\n", "json"),
        ("tsconfig.json", json.dumps(tsconfig, indent=2) + "\n", "json"),
        ("Dockerfile", dockerfile, "dockerfile"),
        ("README.md", _readme(entries, "nodejs"), "markdown"),
        (".env.example", env_block, "bash"),
        (".gitignore", gitignore, "text"),
        ("src/index.ts", index_ts, "typescript"),
        (NODE_UTIL_PATH, NODE_UTIL, "typescript"),
    ]
    for e in entries:
        live = render_node_router(e)
        if live is not None:
            out.append(live)
        else:
            out.append(_node_router(e))
    return out


def _node_router(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    prefix = entry.get("route_prefix", "/integrations/" + entry["id"])
    gate = entry.get("gate_env", "MODE")
    endpoints = entry.get("endpoints", [])

    handler_lines: List[str] = []
    for ep in endpoints:
        ep_method = (ep.get("method") or "GET").lower()
        ep_path = ep.get("path", prefix)
        sub_path = ep_path[len(prefix):] if ep_path.startswith(prefix) else ep_path
        if not sub_path:
            sub_path = "/"
        # Convert FastAPI-style {x} -> Express :x
        express_path = sub_path
        while "{" in express_path and "}" in express_path:
            a = express_path.index("{")
            b = express_path.index("}", a)
            name = express_path[a + 1:b]
            express_path = express_path[:a] + ":" + name + express_path[b + 1:]

        upstream_env = entry["id"].upper() + "_UPSTREAM_URL"
        api_key_env  = entry["id"].upper() + "_API_KEY"
        auth_hdr_env = entry["id"].upper() + "_AUTH_HEADER"
        has_body = ep_method in ("post", "put", "patch")
        # Build the upstream URL from path params, mirroring the route
        url_path_expr = '"' + sub_path + '"'
        for token in sub_path.split("/"):
            if token.startswith("{") and token.endswith("}"):
                v = token[1:-1]
                url_path_expr = url_path_expr.replace(
                    "{" + v + "}", '" + encodeURIComponent(String(req.params.' + v + ')) + "'
                )
        # Trim leading/trailing empty literal pieces ("" + ...)
        # (the runtime string concat handles them fine, but it stays readable)

        data_arg = "req.body" if has_body else "undefined"
        handler = (
            "router." + ep_method + '("' + express_path + '", async (req: Request, res: Response) => {\n'
            "  const requestId = randomUUID().substring(0, 12);\n"
            '  const live = mode() === "live";\n'
            "  if (!live) {\n"
            "    return res.json({\n"
            '      integration: "' + entry["id"] + '",\n'
            '      label: "' + entry["label"].replace('"', '\\"') + '",\n'
            '      endpoint: "' + ep_method.upper() + " " + ep_path + '",\n'
            "      request_id: requestId,\n"
            '      mode: "mock",\n'
            '      status: "OK",\n'
            '      note: "Canned mock response - set ' + gate + '=live and supply ' + upstream_env + ' / ' + api_key_env + ' to call the real provider.",\n'
            "      params: req.params,\n"
            "      body: req.body,\n"
            "    });\n"
            "  }\n"
            "  // Live mode - generic upstream HTTP proxy. Configure via env:\n"
            "  //   " + upstream_env + "  - base URL (e.g. https://api.provider.gov.in)\n"
            "  //   " + api_key_env + "  - API key / bearer token (optional)\n"
            "  //   " + auth_hdr_env + " - header name (default: Authorization). Prefix value with 'Bearer ' if needed.\n"
            '  const upstream = (process.env.' + upstream_env + ' || "").replace(/\\/+$/, "");\n'
            "  if (!upstream) {\n"
            "    return res.json({\n"
            '      integration: "' + entry["id"] + '",\n'
            '      endpoint: "' + ep_method.upper() + " " + ep_path + '",\n'
            "      request_id: requestId,\n"
            '      mode: "live",\n'
            '      status: "MISCONFIGURED",\n'
            '      error: "' + upstream_env + ' is not set",\n'
            "    });\n"
            "  }\n"
            "  try {\n"
            "    const url = upstream + " + url_path_expr + ";\n"
            '    const headers: Record<string, string> = {\n'
            '      "Content-Type": "application/json",\n'
            '      "Accept": "application/json",\n'
            '      "X-Request-Id": requestId,\n'
            "    };\n"
            '    const key = process.env.' + api_key_env + ' || "";\n'
            "    if (key) {\n"
            '      const hdr = process.env.' + auth_hdr_env + ' || "Authorization";\n'
            "      headers[hdr] = key;\n"
            "    }\n"
            "    const upstreamResp = await axios.request({\n"
            "      url,\n"
            '      method: "' + ep_method.upper() + '",\n'
            "      headers,\n"
            "      data: " + data_arg + ",\n"
            "      timeout: 30000,\n"
            "      validateStatus: () => true,\n"
            "    });\n"
            "    return res.status(upstreamResp.status).json({\n"
            '      integration: "' + entry["id"] + '",\n'
            '      endpoint: "' + ep_method.upper() + " " + ep_path + '",\n'
            "      request_id: requestId,\n"
            '      mode: "live",\n'
            "      status: upstreamResp.status,\n"
            "      upstream_url: url,\n"
            "      data: upstreamResp.data,\n"
            "    });\n"
            "  } catch (e: any) {\n"
            "    return res.status(502).json({\n"
            '      integration: "' + entry["id"] + '",\n'
            '      endpoint: "' + ep_method.upper() + " " + ep_path + '",\n'
            "      request_id: requestId,\n"
            '      mode: "live",\n'
            '      status: "UPSTREAM_ERROR",\n'
            "      error: e?.message || String(e),\n"
            "    });\n"
            "  }\n"
            "});\n"
        )
        handler_lines.append(handler)

    handler_block = "\n".join(handler_lines) if handler_lines else "// No endpoints declared in the catalog entry."

    body = (
        'import { Router, Request, Response } from "express";\n'
        'import { randomUUID } from "crypto";\n'
        'import axios from "axios";\n\n'
        "// LAMA-injected integration: " + entry["label"] + " (" + entry["id"] + ").\n"
        "// Live mode is a generic upstream HTTP proxy driven by env vars\n"
        "// (`<ID>_UPSTREAM_URL`, `<ID>_API_KEY`, `<ID>_AUTH_HEADER`). Mock mode is\n"
        "// the default - flip " + gate + "=live to forward calls to the real provider.\n\n"
        "const router = Router();\n\n"
        "function mode(): string {\n"
        '  return (process.env.' + gate + ' || "mock").toLowerCase();\n'
        "}\n\n"
        + handler_block + "\n\n"
        "export default router;\n"
    )
    return ("src/integrations/" + entry["id"] + "/router.ts", body, "typescript")


# ---------------------------------------------------------------------------
# codegen_files writer
# ---------------------------------------------------------------------------

async def _upsert_codegen_file(project_id: str, file_path: str, content: str,
                               language: str = "python") -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    existing = await codegen_files.find_one(
        {"project_id": project_id, "file_path": file_path}, {"_id": 0}
    )
    doc = {
        "id": (existing or {}).get("id") or uuid.uuid4().hex,
        "project_id": project_id,
        "service_id": (existing or {}).get("service_id", "integrations"),
        "service_name": "integrations-service",
        "file_path": file_path,
        "content": content,
        "language": language,
        "file_type": "integration",
        "version": ((existing or {}).get("version", 0)) + 1,
        "edited": False,
        "created_at": (existing or {}).get("created_at", now),
        "updated_at": now,
    }
    await codegen_files.update_one(
        {"project_id": project_id, "file_path": file_path},
        {"$set": doc}, upsert=True,
    )
    return doc


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

_SCAFFOLDERS = {
    "python": _python_scaffold,
    "java":   _java_scaffold,
    "nodejs": _node_scaffold,
}


async def inject_project(project_id: str, language_override: str = "") -> Dict[str, Any]:
    proj = await projects.find_one({"id": project_id}, {"_id": 0})
    if not proj:
        raise ValueError("Project not found")

    language = (language_override or "").lower() or resolve_backend_lang(proj.get("target_tech", ""))
    if language not in _SCAFFOLDERS:
        language = "python"

    enabled_rows = await project_integrations.find(
        {"project_id": project_id, "enabled": True}, {"_id": 0},
    ).to_list(100)
    if not enabled_rows:
        return {"language": language, "injected": 0, "files": [], "skipped": True,
                "service_dir": SERVICE_DIR,
                "message": "No integrations enabled for this project."}

    overrides_per_id = {r["integration_id"]: r.get("config_overrides") or {} for r in enabled_rows}
    enabled_entries: List[Dict[str, Any]] = []
    for row in enabled_rows:
        try:
            enabled_entries.append(get_entry(row["integration_id"]))
        except KeyError:
            continue

    env_block = _env_example_block(enabled_entries, overrides_per_id)

    # 1) Scaffold (main app + Dockerfile + README + manifest + .env.example)
    triplets: List[Tuple[str, str, str]] = _SCAFFOLDERS[language](enabled_entries, env_block)

    # 2) Per-integration source files (Python uses the rich catalog templates;
    #    Java + Node controllers are already emitted as part of the scaffold).
    if language == "python":
        triplets.extend(_python_integration_files(enabled_entries))

    # 3) Write every file under integrations-service/<rel_path>
    now = datetime.now(timezone.utc).isoformat()
    files_written: List[Dict[str, Any]] = []
    for rel_path, content, lang_tag in triplets:
        full_path = SERVICE_DIR + "/" + rel_path
        saved = await _upsert_codegen_file(project_id, full_path, content, language=lang_tag)
        files_written.append({"file_path": full_path, "version": saved["version"],
                              "language": lang_tag})

    # 4) Mark every enabled integration as injected
    for entry in enabled_entries:
        await project_integrations.update_one(
            {"project_id": project_id, "integration_id": entry["id"]},
            {"$set": {
                "last_injected_at": now,
                "last_injected_files": [f["file_path"] for f in files_written
                                        if entry["id"] in f["file_path"]],
            }},
        )

    await audit_log.insert_one({
        "action": "integration.inject",
        "project_id": project_id,
        "at": now,
        "details": {
            "language": language,
            "service_dir": SERVICE_DIR,
            "integrations": [e["id"] for e in enabled_entries],
            "file_count": len(files_written),
        },
    })

    return {
        "language": language,
        "service_dir": SERVICE_DIR,
        "injected": len(enabled_entries),
        "files": files_written,
        "skipped": False,
    }


"""iter-18.17 — DevOps + Tester agents.

Deterministic-path coverage for the two new post-transform agents:

* ``devops_agent`` detects structural gaps (missing @SpringBootApplication,
  missing application.yml sections, missing spring-boot-maven-plugin,
  unresolved compile errors) that a green javac can't see, and applies
  deterministic fixes.
* ``tester_agent`` runs static endpoint + config parity checks against
  the source tree and produces a PASS / PASS_WITH_WARNINGS / FAIL
  verdict.  Boot smoke is exercised via a monkeypatch since it needs
  ``mvn`` in the environment.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dcte import devops_agent, tester_agent  # noqa: E402


def _write(tree: Path, rel: str, body: str) -> Path:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ──────────────────────────── DevOps ──────────────────────────────

def test_devops_detects_missing_main_class(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/Foo.java",
           "package com.acme;\npublic class Foo {}\n")
    gaps = devops_agent.detect_structural_gaps(dest)
    kinds = {g["kind"] for g in gaps}
    assert "missing_main_class" in kinds
    assert "missing_application_config" in kinds


def test_devops_detects_incomplete_application_yml(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/App.java",
           "package com.acme;\n"
           "import org.springframework.boot.SpringApplication;\n"
           "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
           "@SpringBootApplication public class App {\n"
           "  public static void main(String[] a){SpringApplication.run(App.class,a);}\n"
           "}\n")
    _write(dest, "src/main/resources/application.yml",
           "spring:\n  application:\n    name: svc\n")
    gaps = devops_agent.detect_structural_gaps(dest)
    kinds = [g["kind"] for g in gaps]
    assert "incomplete_application_config" in kinds
    incomplete = next(g for g in gaps if g["kind"] == "incomplete_application_config")
    assert "server.port" in incomplete["missing_sections"]
    assert "management.endpoints (actuator)" in incomplete["missing_sections"]


def test_devops_fix_patches_main_and_yaml(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/Foo.java",
           "package com.acme;\npublic class Foo {}\n")
    gaps = devops_agent.detect_structural_gaps(dest)
    fixes, unresolved = devops_agent.apply_deterministic_fixes(dest, gaps)
    kinds = {f["kind"] for f in fixes}
    assert "missing_main_class" in kinds
    assert "missing_application_config" in kinds
    # Re-scan must find no more of the same kinds.
    gaps2 = devops_agent.detect_structural_gaps(dest)
    kinds2 = {g["kind"] for g in gaps2}
    assert "missing_main_class" not in kinds2
    assert "missing_application_config" not in kinds2
    # The new main class must be discoverable + valid Spring Boot.
    mains = list(dest.rglob("Application.java"))
    assert mains, "expected an Application.java to be created"
    body = mains[0].read_text(encoding="utf-8")
    assert "@SpringBootApplication" in body
    assert "SpringApplication.run" in body


def test_devops_appends_missing_yml_sections_without_clobbering(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/App.java",
           "package com.acme;\n"
           "import org.springframework.boot.SpringApplication;\n"
           "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
           "@SpringBootApplication public class App {\n"
           "  public static void main(String[] a){SpringApplication.run(App.class,a);}\n"
           "}\n")
    original_yaml = "spring:\n  application:\n    name: svc\n"
    yml = _write(dest, "src/main/resources/application.yml", original_yaml)
    gaps = devops_agent.detect_structural_gaps(dest)
    devops_agent.apply_deterministic_fixes(dest, gaps)
    body = yml.read_text(encoding="utf-8")
    # Original block preserved.
    assert "application:\n    name: svc" in body
    # New sections appended.
    assert "server:" in body and "port: 8080" in body
    assert "management:" in body and "actuator" not in body  # (no comment mentioning actuator)
    assert "endpoints:" in body


def test_devops_injects_spring_boot_maven_plugin(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "pom.xml",
           "<?xml version='1.0'?>\n"
           "<project>\n"
           "  <build>\n"
           "    <plugins>\n"
           "      <plugin><artifactId>maven-compiler-plugin</artifactId></plugin>\n"
           "    </plugins>\n"
           "  </build>\n"
           "</project>\n")
    gaps = devops_agent.detect_structural_gaps(dest)
    kinds = [g["kind"] for g in gaps]
    assert "missing_pom_plugin" in kinds
    devops_agent.apply_deterministic_fixes(dest, gaps)
    body = (dest / "pom.xml").read_text(encoding="utf-8")
    assert "spring-boot-maven-plugin" in body


def test_devops_flags_failed_build(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/App.java",
           "package com.acme;\n"
           "import org.springframework.boot.SpringApplication;\n"
           "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
           "@SpringBootApplication public class App {\n"
           "  public static void main(String[] a){SpringApplication.run(App.class,a);}\n"
           "}\n")
    build_result = {
        "success": False, "skipped": False, "attempts": 5,
        "fixes_applied": 3, "errors": [{"file": "X", "line": 1, "msg": "boom"}],
        "tool": "mvn", "final_output_tail": "BUILD FAILURE",
    }
    gaps = devops_agent.detect_structural_gaps(dest, build_result=build_result)
    kinds = [g["kind"] for g in gaps]
    assert "build_still_failing" in kinds


@pytest.mark.asyncio
async def test_devops_run_returns_result_object(tmp_path):
    dest = tmp_path / "svc"
    _write(dest, "src/main/java/com/acme/Foo.java",
           "package com.acme;\npublic class Foo {}\n")
    res = await devops_agent.run_devops(dest)
    d = res.as_dict()
    assert d["attempted"] is True
    assert d["fixes_applied"] >= 2  # main class + application.yml
    assert isinstance(d["gaps_found"], list)


# ──────────────────────────── Tester ──────────────────────────────

def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "src-app"
    _write(src, "src/main/java/com/legacy/UserResource.java",
           "package com.legacy;\n"
           "import jakarta.ws.rs.Path;\n"
           "@Path(\"/api/users\")\n"
           "public class UserResource { }\n")
    _write(src, "src/main/java/com/legacy/OrderResource.java",
           "package com.legacy;\n"
           "import jakarta.ws.rs.Path;\n"
           "@Path(\"/api/orders\")\n"
           "public class OrderResource { }\n")
    _write(src, "src/main/resources/microprofile-config.properties",
           "server.port=9000\n"
           "db.url=jdbc:oracle:thin:@localhost:1521:orcl\n"
           "db.username=app\n")
    return src


def _make_dest(tmp_path: Path, include_orders: bool = True) -> Path:
    dst = tmp_path / "dst-app"
    _write(dst, "src/main/java/com/acme/UserController.java",
           "package com.acme;\n"
           "import org.springframework.web.bind.annotation.*;\n"
           "@RestController @RequestMapping(\"/api/users\")\n"
           "public class UserController { @GetMapping public String x(){return \"\";} }\n")
    if include_orders:
        _write(dst, "src/main/java/com/acme/OrderController.java",
               "package com.acme;\n"
               "import org.springframework.web.bind.annotation.*;\n"
               "@RestController @RequestMapping(\"/api/orders\")\n"
               "public class OrderController { @GetMapping public String y(){return \"\";} }\n")
    _write(dst, "src/main/resources/application.yml",
           "server:\n  port: 9000\n"
           "spring:\n  datasource:\n    url: jdbc:postgresql://x/y\n"
           "    username: app\n")
    return dst


def test_tester_endpoint_parity_full_match(tmp_path):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path, include_orders=True)
    parity = tester_agent.endpoint_parity(src, dst)
    assert parity["source_count"] == 2
    assert parity["matched"] == 2
    assert parity["coverage"] == 1.0
    assert parity["missing"] == []


def test_tester_endpoint_parity_detects_missing(tmp_path):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path, include_orders=False)
    parity = tester_agent.endpoint_parity(src, dst)
    assert parity["source_count"] == 2
    assert parity["matched"] == 1
    assert "/api/orders" in parity["missing"]
    assert parity["coverage"] == 0.5


def test_tester_config_parity_catches_db_gap(tmp_path):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path)
    parity = tester_agent.config_parity(src, dst)
    # source has server.port, db.url, db.username → 3 keys.
    assert parity["source_count"] == 3
    # Destination YAML has server.port + spring.datasource.url + spring.datasource.username.
    # db.url and db.username don't literally appear but should be
    # matched via the suffix rewrite (dst key ends in ".url" / ".username").
    assert parity["matched"] >= 2


@pytest.mark.asyncio
async def test_tester_run_pass_verdict_when_everything_matches(tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path, include_orders=True)

    # Boot smoke is exercised by monkeypatching to a green result so the
    # test doesn't need mvn / a running JVM.
    def _fake_boot(dest_root, *, timeout_s=180):
        return {"runnable": True, "tool": "mvn spring-boot:run",
                "port": 9000, "healthy": True, "elapsed_s": 4.2,
                "reason": None, "output_tail": "Started"}
    monkeypatch.setattr(tester_agent, "run_boot_smoke", _fake_boot)

    verdict = await tester_agent.run_tester(dst, source_root=src, run_boot=True, timeout_s=30)
    d = verdict.as_dict()
    assert d["verdict"] == "PASS", d
    assert d["boot_smoke"]["healthy"] is True
    assert d["parity_endpoints"]["coverage"] == 1.0


@pytest.mark.asyncio
async def test_tester_fail_verdict_when_boot_unhealthy(tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path, include_orders=True)

    def _fake_boot(dest_root, *, timeout_s=180):
        return {"runnable": True, "tool": "mvn", "port": 9000,
                "healthy": False, "elapsed_s": 30.0,
                "reason": "URLError: Connection refused",
                "output_tail": "BeanCreationException"}
    monkeypatch.setattr(tester_agent, "run_boot_smoke", _fake_boot)

    verdict = await tester_agent.run_tester(dst, source_root=src, run_boot=True, timeout_s=30)
    assert verdict.verdict == "FAIL"


@pytest.mark.asyncio
async def test_tester_pass_with_warnings_when_boot_not_runnable(tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    dst = _make_dest(tmp_path, include_orders=True)

    def _fake_boot(dest_root, *, timeout_s=180):
        return {"runnable": False, "healthy": False,
                "reason": "no mvn on PATH and no target/*.jar"}
    monkeypatch.setattr(tester_agent, "run_boot_smoke", _fake_boot)

    verdict = await tester_agent.run_tester(dst, source_root=src, run_boot=True, timeout_s=30)
    assert verdict.verdict == "PASS_WITH_WARNINGS"


# ──────────────────────────── Fabric ──────────────────────────────

def test_new_agent_keys_registered():
    from fabric.model_fabric import AGENT_COMPLEXITY
    assert AGENT_COMPLEXITY.get("dcte.devops") == "medium"
    assert AGENT_COMPLEXITY.get("dcte.tester") == "low"


def test_new_job_statuses_present():
    from dcte.models import DcteJobStatus
    assert DcteJobStatus.DEVOPS.value == "devops"
    assert DcteJobStatus.TESTING.value == "testing"

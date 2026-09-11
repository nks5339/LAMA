"""Smoke test for the structural validator + skeleton plumbing.

Run via:  python3 backend/_test_structural_validator.py
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(__file__))

from codegen.file_templates import (
    required_tokens_for,
    get_file_instruction,
    FILE_TYPE_SKELETONS,
)


def _validate_structure(content, file_type, backend_lang):
    if not content or len(content.strip()) < 40:
        return False, ["content too short / empty"]
    tokens = required_tokens_for(file_type or "", backend_lang or "")
    if not tokens:
        return True, []
    missing = []
    for tok in tokens:
        if tok.startswith(".regex/"):
            pat = tok[len(".regex/"):]
            try:
                if not re.search(pat, content):
                    missing.append("pattern " + repr(pat))
            except re.error:
                continue
        else:
            if tok not in content:
                missing.append(repr(tok))
    return (len(missing) == 0), missing


def main():
    print("--- 1. Skeleton plumbing ---")
    java_ctrl_instr = get_file_instruction("controller", "java")
    assert "@RestController" in java_ctrl_instr, "java ctrl skeleton lost @RestController"
    assert "@RequestMapping" in java_ctrl_instr
    assert "MANDATORY SKELETON" in java_ctrl_instr
    assert "package com.lama" in java_ctrl_instr
    print("  java/controller instr OK  (len=%d)" % len(java_ctrl_instr))

    py_ctrl_instr = get_file_instruction("controller", "python")
    assert "APIRouter" in py_ctrl_instr
    assert "MANDATORY SKELETON" in py_ctrl_instr
    print("  python/controller instr OK  (len=%d)" % len(py_ctrl_instr))

    dn_ctrl_instr = get_file_instruction("controller", "dotnet")
    assert "[ApiController]" in dn_ctrl_instr
    print("  dotnet/controller instr OK")

    print("\n--- 2. Validator: GOOD samples ---")
    java_good = (
        "package com.lama.x.web;\n"
        "import org.springframework.web.bind.annotation.*;\n"
        "@RestController\n"
        "@RequestMapping(\"/api/v1/orders\")\n"
        "public class OrderController {\n"
        "    @GetMapping(\"/{id}\") public String get(@PathVariable String id) { return id; }\n"
        "}\n"
    )
    ok, miss = _validate_structure(java_good, "controller", "java")
    print("  java controller (good):", ok, miss); assert ok

    java_svc_good = (
        "package com.lama.x.service;\n"
        "import org.springframework.stereotype.Service;\n"
        "@Service\n"
        "public class OrderService { }\n"
    )
    ok, miss = _validate_structure(java_svc_good, "service", "java")
    print("  java service (good):", ok, miss); assert ok

    java_ent_good = (
        "package com.lama.x.domain;\n"
        "import jakarta.persistence.*;\n"
        "@Entity\n"
        "@Table(name=\"orders\")\n"
        "public class Order { @Id private Long id; }\n"
    )
    ok, miss = _validate_structure(java_ent_good, "entity", "java")
    print("  java entity (good):", ok, miss); assert ok

    py_good = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get(\"/x\")\n"
        "async def x(): return {}\n"
    )
    ok, miss = _validate_structure(py_good, "controller", "python")
    print("  python controller (good):", ok, miss); assert ok

    dn_good = (
        "using Microsoft.AspNetCore.Mvc;\n"
        "namespace X.Web.Controllers;\n"
        "[ApiController]\n"
        "[Route(\"api/v1/x\")]\n"
        "public class XController : ControllerBase {\n"
        "    [HttpGet(\"{id}\")] public IActionResult Get(string id) => Ok();\n"
        "}\n"
    )
    ok, miss = _validate_structure(dn_good, "controller", "dotnet")
    print("  dotnet controller (good):", ok, miss); assert ok

    print("\n--- 3. Validator: BAD samples (must be rejected) ---")
    java_bad = (
        "package com.lama.x.web;\n"
        "public class OrderController {\n"
        "    public String get(String id) { return id; }\n"
        "}\n"
    )
    ok, miss = _validate_structure(java_bad, "controller", "java")
    print("  java controller (BAD = no @RestController):", ok, miss)
    assert not ok and any("@RestController" in m for m in miss)

    java_svc_bad = (
        "package com.lama.x.service;\n"
        "public class OrderService {\n"
        "    public Object get() { return null; }\n"
        "}\n"
    )
    ok, miss = _validate_structure(java_svc_bad, "service", "java")
    print("  java service (BAD = no @Service):", ok, miss)
    assert not ok and any("@Service" in m for m in miss)

    java_ent_bad = "package com.lama.x.domain;\npublic class Order { Long id; }\n"
    ok, miss = _validate_structure(java_ent_bad, "entity", "java")
    print("  java entity (BAD = no @Entity/@Table):", ok, miss)
    assert not ok

    py_bad = "def x():\n    return None\n"
    ok, miss = _validate_structure(py_bad, "controller", "python")
    print("  python controller (BAD = no APIRouter):", ok, miss)
    assert not ok

    dn_bad = "public class XController { }\n"
    ok, miss = _validate_structure(dn_bad, "controller", "dotnet")
    print("  dotnet controller (BAD = no [ApiController]):", ok, miss)
    assert not ok

    print("\n--- 4. The actual garbage the user complained about ---")
    user_garbage_java = (
        "public class ChangePwdController {\n"
        "    // Preconditions:\n"
        "    // TODO: backfill from legacy KB\n"
        "    public void changePassword() {\n"
        "        throw new UnsupportedOperationException();\n"
        "    }\n"
        "}\n"
    )
    ok, miss = _validate_structure(user_garbage_java, "controller", "java")
    print("  USER-REPORTED GARBAGE:", ok, miss)
    assert not ok, "User-reported garbage SHOULD be rejected by validator"
    assert any("package" in m or "@RestController" in m or "@RequestMapping" in m for m in miss)

    print("\nAll validator tests PASSED.")
    print("Skeletons defined for", len(FILE_TYPE_SKELETONS), "(file_type, lang) pairs.")


if __name__ == "__main__":
    main()


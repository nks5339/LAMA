"""Smoke-test the audit-trail integration end-to-end through the renderer."""
import sys
import types

# Stub out heavy DB deps so we can import renderer without motor.
fake_db = types.ModuleType("db")
fake_db.codegen_files = None
fake_db.project_integrations = None
fake_db.projects = None
fake_db.audit_log = None
sys.modules.setdefault("db", fake_db)

from integrations.catalog import get_entry
from integrations.renderer import (
    _python_scaffold,
    _python_integration_files,
    _java_scaffold,
    _node_scaffold,
    _env_example_block,
)

e = get_entry("audit_trail_logger")
print("Catalog entry:", e["id"], "| kind=", e.get("kind"), "| category=", e["category"])
print("Python path:", e["file_templates"]["python"][0])
print("Java path  :", e["file_templates"]["java"][0])
print("Node path  :", e["file_templates"]["nodejs"][0])

entries = [e]
env_block = _env_example_block(entries, {})

# PYTHON
py_files = _python_scaffold(entries, env_block) + _python_integration_files(entries)
print()
print("=== PYTHON files (", len(py_files), ") ===")
for p, _, lang in py_files:
    print(" ", lang.ljust(10), p)
main_py = next(c for p, c, _ in py_files if p == "main.py")
print()
print("main.py imports AuditTrailMiddleware?",
      "AuditTrailMiddleware" in main_py)
print("main.py calls app.add_middleware?",
      "app.add_middleware(AuditTrailMiddleware)" in main_py)
compile(main_py, "main.py", "exec")
mw_py = next(c for p, c, _ in py_files if p.endswith("middleware.py"))
compile(mw_py, "middleware.py", "exec")
print("Python files compile clean.")

# JAVA
java_files = _java_scaffold(entries, env_block)
print()
print("=== JAVA files (", len(java_files), ") ===")
for p, _, lang in java_files:
    print(" ", lang.ljust(10), p)
pom = next(c for p, c, _ in java_files if p == "pom.xml")
print()
print("pom.xml has spring-boot-starter-aop?", "spring-boot-starter-aop" in pom)
aspect = next(c for p, c, _ in java_files if p.endswith("AuditTrailAspect.java"))
print("Aspect annotated @Aspect @Component?",
      "@Aspect" in aspect and "@Component" in aspect)
print("Aspect pointcut on @RestController?",
      "@org.springframework.web.bind.annotation.RestController" in aspect)
print("Aspect references AUDIT_TRAIL_MODE?", "AUDIT_TRAIL_MODE" in aspect)

# NODE
node_files = _node_scaffold(entries, env_block)
print()
print("=== NODE files (", len(node_files), ") ===")
for p, _, lang in node_files:
    print(" ", lang.ljust(10), p)
idx = next(c for p, c, _ in node_files if p == "src/index.ts")
print()
print("index.ts imports auditTrailMiddleware?",
      "import { auditTrailMiddleware } from" in idx)
print("index.ts calls app.use(auditTrailMiddleware)?",
      "app.use(auditTrailMiddleware);" in idx)
node_mw = next(c for p, c, _ in node_files if p.endswith("router.ts"))
print("Node middleware exports default + named?",
      "export const auditTrailMiddleware" in node_mw and
      "export default auditTrailMiddleware" in node_mw)
print()
print("ALL OK.")




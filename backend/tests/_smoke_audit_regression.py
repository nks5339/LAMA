"""Regression smoke for the integrations catalog after audit-logger addition."""
import sys
import types

fake = types.ModuleType("db")
for n in ("codegen_files", "project_integrations", "projects", "audit_log"):
    setattr(fake, n, None)
sys.modules.setdefault("db", fake)

from integrations.catalog import CATALOG, catalog_for_ui
from integrations.renderer import (
    _is_middleware,
    _python_scaffold,
    _node_scaffold,
    _java_scaffold,
    _env_example_block,
)

print("CATALOG size:", len(CATALOG))
print("Categories  :", sorted({e["category"] for e in CATALOG}))
print("IDs         :", [e["id"] for e in CATALOG])

ui = catalog_for_ui()
print("UI projection size:", len(ui))
print("UI never leaks file_templates?",
      all("file_templates" not in row for row in ui))

pan = next(e for e in CATALOG if e["id"] == "pan_verification")
audit = next(e for e in CATALOG if e["id"] == "audit_trail_logger")
print("PAN is middleware?  ", _is_middleware(pan), "(expect False)")
print("AUDIT is middleware?", _is_middleware(audit), "(expect True)")

entries = [pan, audit]
env_block = _env_example_block(entries, {})

py = _python_scaffold(entries, env_block)
main_py = next(c for p, c, _ in py if p == "main.py")
print()
print("Mixed main.py includes PAN router?",
      "app.include_router(pan_verification_router)" in main_py)
print("Mixed main.py adds AUDIT middleware?",
      "app.add_middleware(AuditTrailMiddleware)" in main_py)
compile(main_py, "main.py", "exec")

ts_files = _node_scaffold(entries, env_block)
idx = next(c for p, c, _ in ts_files if p == "src/index.ts")
print("Mixed index.ts mounts PAN router?",
      'app.use("/integrations/pan", panVerificationRouter);' in idx)
print("Mixed index.ts mounts AUDIT middleware?",
      "app.use(auditTrailMiddleware);" in idx)

java_files = _java_scaffold(entries, env_block)
pom = next(c for p, c, _ in java_files if p == "pom.xml")
print("Mixed pom.xml has AOP starter?", "spring-boot-starter-aop" in pom)
print("Mixed pom.xml still has web starter?", "spring-boot-starter-web" in pom)
print()
print("Regression smoke OK.")


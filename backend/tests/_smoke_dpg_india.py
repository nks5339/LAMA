"""Smoke-test the Indian-stack DPG entries through the full renderer."""
import sys
import types

fake = types.ModuleType("db")
for n in ("codegen_files", "project_integrations", "projects", "audit_log"):
    setattr(fake, n, None)
sys.modules.setdefault("db", fake)

from integrations.catalog import CATALOG, get_entry
from integrations.renderer import (
    _python_scaffold,
    _python_integration_files,
    _java_scaffold,
    _node_scaffold,
    _env_example_block,
)

print("CATALOG size :", len(CATALOG))
fam = {}
cat = {}
for e in CATALOG:
    fam[e.get("family", "?")] = fam.get(e.get("family", "?"), 0) + 1
    cat[e.get("category", "?")] = cat.get(e.get("category", "?"), 0) + 1
print("Families     :", fam)
print("Categories   :", cat)
print()

new_ids = ["bharatkosh_ntrp", "pfms", "api_setu", "bhashini", "abdm", "jeevan_pramaan"]
for eid in new_ids:
    e = get_entry(eid)
    entries = [e]
    env_block = _env_example_block(entries, {})
    py_files = _python_scaffold(entries, env_block) + _python_integration_files(entries)
    java_files = _java_scaffold(entries, env_block)
    node_files = _node_scaffold(entries, env_block)
    main_py = next(c for p, c, _ in py_files if p == "main.py")
    compile(main_py, "main.py", "exec")
    router_py = next(c for p, c, _ in py_files if p.endswith("router.py") and eid in p)
    compile(router_py, eid + "/router.py", "exec")
    print(f"  {eid:18s}  py={len(py_files):2d}  java={len(java_files):2d}  node={len(node_files):2d}  OK  family={e.get('family')}  cat={e.get('category')}")

print()
print(f"Mixed render (all {len(CATALOG)} entries enabled)...")
env_block = _env_example_block(CATALOG, {})
py_files = _python_scaffold(CATALOG, env_block) + _python_integration_files(CATALOG)
java_files = _java_scaffold(CATALOG, env_block)
node_files = _node_scaffold(CATALOG, env_block)
main_py = next(c for p, c, _ in py_files if p == "main.py")
compile(main_py, "main.py", "exec")
print(f"  python files: {len(py_files)}")
print(f"  java files  : {len(java_files)}")
print(f"  node files  : {len(node_files)}")

# Sanity assertions
assert "app.include_router(bharatkosh_ntrp_router)" in main_py
assert "app.include_router(abdm_router)" in main_py
assert "app.add_middleware(AuditTrailMiddleware)" in main_py
idx_ts = next(c for p, c, _ in node_files if p == "src/index.ts")
assert 'app.use("/integrations/bharatkosh", bharatkoshNtrpRouter);' in idx_ts
assert 'app.use("/integrations/bhashini", bhashiniRouter);' in idx_ts
assert "app.use(auditTrailMiddleware);" in idx_ts
print()
print("ALL OK.")


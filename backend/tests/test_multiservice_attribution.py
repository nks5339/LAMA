"""Each envelope's code must land under ITS OWN service.

Found by driving the real pipeline against a two-backend-service fixture.
Every file for `claims-service` was written under
``services/panel-service/`` with package ``com.lama.panelservice``:

    ENV-0003 /api/claims/{id} -> services/panel-service/.../entity/ApiClaimsId.java
    ENV-0003 /api/claims/{id} -> services/panel-service/.../controller/ApiClaimsIdController.java
    ... 8 files, all in the wrong service

`_deterministic_codegen_envelopes` faithfully records `_service_name` on
every envelope. The planner never read it. It computed ONE
`be_service_name` (the first backend service it found) and ONE
`fe_service_name`, then passed the same value to `_be_task_layout` for
every envelope in the project.

That defeats the decomposition Stage 3 exists to produce. LAMA's whole
proposition is splitting a legacy monolith into microservices; collapsing
them back into the first service at Stage 4 undoes it silently, and the
result still compiles, so nothing downstream notices.

The build manifest had the same shape of bug: exactly one pom.xml was
emitted, for the first service. The other services would have source files
and no build file at all.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

from routes.codegen import _be_task_layout, _fe_task_layout  # noqa: E402


def test_be_layout_places_files_under_the_named_service():
    """Sanity: the layout helper already honours `service`."""
    a = _be_task_layout("java", "spring-boot", "api_panels_id", "GET",
                        "/api/panels/{id}", service="panel-service")
    b = _be_task_layout("java", "spring-boot", "api_claims_id", "GET",
                        "/api/claims/{id}", service="claims-service")

    assert "services/panel-service/" in a["entity"]["path"]
    assert "services/claims-service/" in b["entity"]["path"]
    # Java package must follow the service too, not just the directory.
    assert "panelservice" in a["entity"]["path"]
    assert "claimsservice" in b["entity"]["path"]


def test_fe_layout_places_files_under_the_named_service():
    a = _fe_task_layout("typescript", "react", "panels", "GET", "/panels",
                        service="web")
    b = _fe_task_layout("typescript", "react", "admin", "GET", "/admin",
                        service="admin-ui")

    assert "services/web/" in a["page"]["path"]
    assert "services/admin-ui/" in b["page"]["path"]


# ── the regression itself ─────────────────────────────────────────────

def _plan(envelopes, arch_services):
    """Run the planner's service-resolution + fan-out in isolation.

    Imports lazily so a failure to import surfaces as a test error rather
    than a collection error.
    """
    from routes.codegen import _plan_tasks_for_envelopes
    return _plan_tasks_for_envelopes(
        envelopes,
        arch_services=arch_services,
        be_target={"lang": "java", "framework": "spring-boot"},
        fe_target={"lang": "typescript", "framework": "react"},
    )


ENVELOPES = [
    {"envelope_id": "ENV-0001", "endpoint_method": "GET",
     "endpoint_path": "/api/panels/{id}", "side": "backend",
     "_service_name": "panel-service", "br_ids": ["BR-101"]},
    {"envelope_id": "ENV-0003", "endpoint_method": "GET",
     "endpoint_path": "/api/claims/{id}", "side": "backend",
     "_service_name": "claims-service", "br_ids": ["BR-201"]},
    {"envelope_id": "ENV-0004", "endpoint_method": "GET",
     "endpoint_path": "/panels", "side": "frontend",
     "_service_name": "web-ui", "br_ids": ["BR-301"]},
]
ARCH = [
    {"name": "panel-service", "side": "backend"},
    {"name": "claims-service", "side": "backend"},
    {"name": "web-ui", "side": "frontend"},
]


def test_each_envelope_lands_under_its_own_service():
    tasks = _plan(ENVELOPES, ARCH)
    by_env = {}
    for t in tasks:
        by_env.setdefault(t["envelope_id"], []).append(t["target_path"])

    for path in by_env["ENV-0001"]:
        assert "services/panel-service/" in path, path
    for path in by_env["ENV-0003"]:
        assert "services/claims-service/" in path, (
            "a claims-service envelope must not be written into another "
            f"service's tree: {path}"
        )
    for path in by_env["ENV-0004"]:
        assert "services/web-ui/" in path, path


def test_every_backend_service_gets_its_own_build_manifest():
    """Source files with no pom.xml beside them cannot be built."""
    tasks = _plan(ENVELOPES, ARCH)
    manifests = {t["target_path"] for t in tasks if t["layer"] == "manifest"}

    assert any("services/panel-service/pom.xml" in m for m in manifests), manifests
    assert any("services/claims-service/pom.xml" in m for m in manifests), manifests
    assert any("services/web-ui/package.json" in m for m in manifests), manifests


def test_no_duplicate_manifest_per_service():
    tasks = _plan(ENVELOPES, ARCH)
    manifests = [t["target_path"] for t in tasks if t["layer"] == "manifest"]
    assert len(manifests) == len(set(manifests)), manifests


def test_envelope_without_service_name_falls_back_not_crashes():
    """Envelopes planned before `_service_name` existed must still work."""
    legacy = [{"envelope_id": "ENV-0009", "endpoint_method": "GET",
               "endpoint_path": "/api/legacy", "side": "backend",
               "br_ids": []}]
    tasks = _plan(legacy, ARCH)
    assert tasks
    for t in tasks:
        assert t["target_path"].startswith("services/"), t["target_path"]

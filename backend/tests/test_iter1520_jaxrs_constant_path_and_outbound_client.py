"""iter-15.20 — Regression tests for two Code Transformer "Discovered
Architecture" bugs found on the SHPP Sikkim `hiring-service` pilot:

1. Real JAX-RS controllers whose class-level `@Path` references a symbolic
   constant (`@Path(VEHICLE_HIRE_MAIN_URL)`) instead of an inline string
   literal produced ZERO route entities — every one of the service's real,
   inbound endpoints was silently dropped.
2. Outbound REST-CLIENT interfaces (`@RegisterRestClient`/`@FeignClient`)
   use the identical `@Path`/`@GET`/`@POST` annotation style as real
   inbound resources, so they were counted as if they were the service's
   own exposed API — exactly backwards, and the ONLY thing the old
   extractor found for a codebase using constant-based `@Path` on its real
   controllers.
3. (Related) a method-level `@Path("...")` separated from its `@POST`/
   `@GET` by an intervening annotation (`@SecuredPayload`) was silently
   ignored, collapsing every method on a controller onto the bare class
   prefix.

These tests exercise `kb.owl_extractor.extract_java` directly and
`tools_kb_builder.build_tools_kb`/`build_traceability_map` end-to-end,
using source shaped exactly like the real pilot files (constant-based
class `@Path`, an intervening annotation between verb and method `@Path`,
and a `@RegisterRestClient` outbound interface).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb.owl_extractor import extract_java
from tools_kb_builder import build_tools_kb


CONSTANTS_FILE = """
package com.shpp.core.util;

public class ApiUrlPatterns {
    public static final String VEHICLE_HIRE_MAIN_URL = "vehicleHiring/api/v1";
}
"""

CONTROLLER_FILE = """
package com.shpp.web.controller;

import jakarta.ws.rs.*;
import jakarta.validation.Valid;
import static com.shpp.core.util.ApiUrlPatterns.VEHICLE_HIRE_MAIN_URL;

@Path(VEHICLE_HIRE_MAIN_URL)
@Produces("application/json")
public class VehicleHiringController {

    @POST
    @SecuredPayload
    @Path("/saveOrUpdate")
    public Response saveHiringRequest(@Valid VehicleHiringRequest request) {
        return null;
    }

    @GET
    @Path("/allCategories")
    public Response getVehicleCategories() {
        return null;
    }
}
"""

OUTBOUND_CLIENT_FILE = """
package com.shpp.core.restclient;

import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import org.eclipse.microprofile.rest.client.inject.RegisterRestClient;

@Path("/workflow/api/v1/transaction")
@RegisterRestClient(configKey = "com.shpp.core.restclient.WorkflowRestClient")
public interface WorkflowRestClient {

    @POST
    @Path("initiateProcess")
    ApiResponse initiateWorkflow(InitiateTaskRequest taskRequest);
}
"""


def test_extract_java_resolves_constant_based_class_path():
    """A class-level @Path referencing a symbolic constant (declared in a
    DIFFERENT file) must still produce route entities once the constant is
    supplied via `global_constants` — not silently drop the whole class."""
    global_constants = {"VEHICLE_HIRE_MAIN_URL": "vehicleHiring/api/v1"}
    entities = extract_java(CONTROLLER_FILE, "VehicleHiringController.java", global_constants=global_constants)
    routes = [e for e in entities if e["type"] == "ROUTE"]
    assert len(routes) == 2, f"expected 2 routes, got {routes}"
    paths = sorted(r["name"] for r in routes)
    assert paths == ["/vehicleHiring/api/v1/allCategories", "/vehicleHiring/api/v1/saveOrUpdate"]


def test_extract_java_without_constant_map_still_emits_routes():
    """Even if the constant CANNOT be resolved (e.g. cross-file import not
    supplied), we must not drop the whole resource — emit routes using the
    method-level path alone rather than silently returning zero entities."""
    entities = extract_java(CONTROLLER_FILE, "VehicleHiringController.java", global_constants=None)
    routes = [e for e in entities if e["type"] == "ROUTE"]
    assert len(routes) == 2
    for r in routes:
        assert r.get("path_prefix_unresolved") is True


def test_method_path_survives_intervening_annotation():
    """`@Path("/saveOrUpdate")` separated from `@POST` by `@SecuredPayload`
    must still resolve to the full combined path, not collapse to the bare
    class prefix."""
    global_constants = {"VEHICLE_HIRE_MAIN_URL": "vehicleHiring/api/v1"}
    entities = extract_java(CONTROLLER_FILE, "VehicleHiringController.java", global_constants=global_constants)
    routes = {e["name"]: e for e in entities if e["type"] == "ROUTE"}
    assert "/vehicleHiring/api/v1/saveOrUpdate" in routes
    save_route = routes["/vehicleHiring/api/v1/saveOrUpdate"]
    assert save_route["handler_method"] == "saveHiringRequest"
    assert save_route["is_outbound_client"] is False


def test_outbound_rest_client_flagged_and_class_populated():
    """@RegisterRestClient interfaces must be flagged is_outbound_client so
    they never masquerade as this service's own API."""
    entities = extract_java(OUTBOUND_CLIENT_FILE, "WorkflowRestClient.java")
    routes = [e for e in entities if e["type"] == "ROUTE"]
    assert len(routes) == 1
    assert routes[0]["is_outbound_client"] is True
    assert routes[0]["handler_class"] == "WorkflowRestClient"


def test_build_tools_kb_end_to_end_separates_inbound_from_outbound():
    """End-to-end via build_tools_kb(): the constants file + controller +
    outbound client interface together should produce 2 inbound endpoints
    (real controller) and 1 outbound client call — not the reverse (which
    is what the pilot was showing before this fix)."""
    files = [
        {"path": "src/main/java/com/shpp/core/util/ApiUrlPatterns.java", "content": CONSTANTS_FILE},
        {"path": "src/main/java/com/shpp/web/controller/VehicleHiringController.java", "content": CONTROLLER_FILE},
        {"path": "src/main/java/com/shpp/core/restclient/WorkflowRestClient.java", "content": OUTBOUND_CLIENT_FILE},
    ]
    kb = build_tools_kb(files, [])
    trace = kb["traceability"]
    inbound = [r for r in trace["api_to_db"] if not r["is_outbound_client"]]
    outbound = trace["outbound_clients"]

    assert len(inbound) == 2, f"expected 2 real inbound endpoints, got {inbound}"
    assert {r["api"] for r in inbound} == {
        "/vehicleHiring/api/v1/saveOrUpdate",
        "/vehicleHiring/api/v1/allCategories",
    }
    assert all(r["class"] == "VehicleHiringController" for r in inbound), inbound

    assert len(outbound) == 1
    assert outbound[0]["api"] == "/workflow/api/v1/transaction/initiateProcess"
    assert outbound[0]["class"] == "WorkflowRestClient"

    stats = kb["stats"]
    assert stats["api_routes"] == 2
    assert stats["outbound_client_calls"] == 1

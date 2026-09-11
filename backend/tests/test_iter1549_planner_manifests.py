"""iter-15.49 — Planner emits build-manifest tasks per selected build tool.

Verifies `_build_deterministic_tasks` synthesises a wave-1 GENERATE task
for each `{component: tool}` pair when the target manifest isn't already
covered by an existing source-file task.
"""
import sys
import os
import pathlib

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from routes.tools import _build_deterministic_tasks  # noqa: E402


def _wave1_manifest_tasks(tasks):
    return [t for t in tasks if t.get("action") == "GENERATE" and t.get("layer") == "config"]


def test_maven_manifest_task_injected():
    src = [{"path": "hiring-service/src/main/java/com/x/User.java", "content": "class User{}"}]
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=src,
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java", "backend": "spring-boot"},
        build_tools={"hiring-service": "maven"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "hiring-service/pom.xml"
    assert m[0]["wave"] == 1
    assert m[0]["build_tool"] == "maven"
    assert m[0]["synthetic"] is True


def test_gradle_manifest_task_injected():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "svc/src/App.java", "content": ""}],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools={"svc": "gradle"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "svc/build.gradle"


def test_npm_manifest_for_frontend():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "web/src/App.tsx", "content": ""}],
        detected_stack={"runtime": "node"},
        target_stack={"runtime": "node", "frontend": "react"},
        build_tools={"web": "npm"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "web/package.json"


def test_dotnet_csproj_uses_component_stem():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "OrderApi/Program.cs", "content": ""}],
        detected_stack={"runtime": "dotnet"},
        target_stack={"runtime": "dotnet"},
        build_tools={"OrderApi": "dotnet"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "OrderApi/OrderApi.csproj"


def test_go_mod_at_component_root():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "api/main.go", "content": ""}],
        detected_stack={"runtime": "go"},
        target_stack={"runtime": "go"},
        build_tools={"api": "go"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "api/go.mod"


def test_multiple_components_multiple_manifests():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[
            {"path": "backend/src/App.java", "content": ""},
            {"path": "frontend/src/App.tsx", "content": ""},
        ],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools={"backend": "maven", "frontend": "npm"},
    )
    m = _wave1_manifest_tasks(tasks)
    paths = sorted(t["target_path"] for t in m)
    assert paths == ["backend/pom.xml", "frontend/package.json"]


def test_no_duplicate_when_source_already_has_manifest():
    # Same-tool same-language: source already has a pom.xml → target
    # path collides → no synthetic task.
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[
            {"path": "svc/pom.xml", "content": "<project/>"},
            {"path": "svc/src/App.java", "content": ""},
        ],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools={"svc": "maven"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert m == [], "should not synthesize when the source pom.xml is already carried through"


def test_root_component_places_manifest_at_top_level():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "src/App.java", "content": ""}],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools={"root": "maven"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "pom.xml"


def test_no_build_tools_no_synthetic_tasks():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "svc/src/App.java", "content": ""}],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools=None,
    )
    assert _wave1_manifest_tasks(tasks) == []


def test_unknown_tool_is_skipped():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "svc/src/App.java", "content": ""}],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java"},
        build_tools={"svc": "make"},
    )
    assert _wave1_manifest_tasks(tasks) == []


def test_manifest_task_has_actionable_notes():
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=[{"path": "svc/src/App.java", "content": ""}],
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java", "backend": "spring-boot"},
        build_tools={"svc": "maven"},
    )
    m = _wave1_manifest_tasks(tasks)[0]
    assert "BUILD MANIFEST" in m["notes"]
    assert "maven" in m["notes"]
    assert "pom.xml" in m["notes"]
    assert m["phase"] == "scaffold"
    assert m["component"] == "svc"


# iter-15.53 — When the component label doesn't match the source top-level
# directory (e.g. UI picks "backend", but the uploaded project is
# `hiring-service/…`), the manifest MUST land at `hiring-service/pom.xml`,
# NOT at the abstract `backend/pom.xml`.
def test_component_label_falls_back_to_source_module_root():
    src = [
        {"path": "hiring-service/src/main/java/com/shpp/App.java", "content": ""},
        {"path": "hiring-service/src/main/java/com/shpp/User.java", "content": ""},
    ]
    tasks, _ = _build_deterministic_tasks(
        envelopes=[], src_files=src,
        detected_stack={"runtime": "java"},
        target_stack={"runtime": "java", "backend": "spring-boot"},
        build_tools={"backend": "maven"},
    )
    m = _wave1_manifest_tasks(tasks)
    assert len(m) == 1
    assert m[0]["target_path"] == "hiring-service/pom.xml", \
        f"expected hiring-service/pom.xml, got {m[0]['target_path']}"
    assert m[0]["module_root"] == "hiring-service"

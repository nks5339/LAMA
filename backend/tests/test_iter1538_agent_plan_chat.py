"""
iter-15.38 — "Planner review required" (and the Coder / Tester panels) now
embed a chat assistant: the user types a natural-language instruction
("remove all test files from wave 2", "find files touching the Order
table", "move the DAO classes to wave 3") and an LLM maps it onto the
CURRENT task/file list for that transformation, returning structured
actions that are applied and persisted immediately (no separate "Save"
step) via `POST /tools/transformer/{id}/agent-chat`.

These tests exercise the actual FastAPI endpoint in routes/tools.py via
TestClient, with every Mongo collection replaced by the same lightweight
in-memory fake used in test_iter1519_agent_pipeline_config.py, and
`fabric_call` monkeypatched to return canned structured-action JSON so no
LLM/network access is required.
"""
import os
import sys
from typing import Any, Dict, List

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

os.environ.setdefault("MONGO_URL", "mongodb://127.0.0.1:27017")
os.environ.setdefault("DB_NAME", "lama_test")


class _FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def sort(self, *_args, **_kwargs) -> "_FakeCursor":
        return self

    async def to_list(self, _n=None):
        return list(self._rows)

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self) -> None:
        self.docs: List[Dict[str, Any]] = []

    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find_one(self, q: Dict[str, Any], _proj: Dict[str, Any] = None):
        for d in self.docs:
            if self._match(d, q):
                return dict(d)
        return None

    def find(self, q: Dict[str, Any] = None, _proj: Dict[str, Any] = None) -> _FakeCursor:
        q = q or {}
        rows = [dict(d) for d in self.docs if self._match(d, q)]
        return _FakeCursor(rows)

    async def insert_one(self, doc: Dict[str, Any]):
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("_id", "")})

    async def update_one(self, q: Dict[str, Any], upd: Dict[str, Any], upsert: bool = False):
        for d in self.docs:
            if self._match(d, q):
                if "$set" in upd:
                    d.update(upd["$set"])
                if "$unset" in upd:
                    for k in upd["$unset"]:
                        d.pop(k, None)
                return
        if upsert:
            new = dict(q)
            if "$set" in upd:
                new.update(upd["$set"])
            self.docs.append(new)

    async def update_many(self, q: Dict[str, Any], upd: Dict[str, Any]):
        n = 0
        for d in self.docs:
            if self._match(d, q):
                if "$set" in upd:
                    d.update(upd["$set"])
                if "$unset" in upd:
                    for k in upd["$unset"]:
                        d.pop(k, None)
                n += 1
        return type("R", (), {"modified_count": n})

    async def delete_one(self, q: Dict[str, Any]):
        for i, d in enumerate(self.docs):
            if self._match(d, q):
                del self.docs[i]
                return type("R", (), {"deleted_count": 1})
        return type("R", (), {"deleted_count": 0})

    async def delete_many(self, q: Dict[str, Any]):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, q)]
        return type("R", (), {"deleted_count": before - len(self.docs)})


@pytest.fixture
def env(monkeypatch):
    import db
    import routes.tools as tools_mod

    fake_prompts = FakeCollection()
    fake_transformations = FakeCollection()
    fake_tasks = FakeCollection()
    fake_transform_files = FakeCollection()
    fake_audit = FakeCollection()
    fake_tools_kb = FakeCollection()

    fake_prompts.docs.append({
        "key": "tools.transformer",
        "template": "GLOBAL DEFAULT TRANSFORM PROMPT",
    })
    fake_transformations.docs.append({
        "_id": "tx-1", "name": "demo", "status": "completed", "model": "",
    })
    fake_tasks.docs.extend([
        {
            "_id": "t1", "transform_id": "tx-1", "task_id": "TASK-001", "wave": 3,
            "wave_name": "Wave 3 — Persistence / Repository Layer", "layer": "repository",
            "action": "TRANSFORM", "status": "DONE",
            "source_path": "src/dao/UserDao.java", "target_path": "src/repo/UserRepository.java",
        },
        {
            "_id": "t2", "transform_id": "tx-1", "task_id": "TASK-002", "wave": 9,
            "wave_name": "Wave 9 — Tests & Support Files", "layer": "test",
            "action": "TRANSFORM", "status": "DONE",
            "source_path": "src/test/UserDaoTest.java", "target_path": "src/test/UserRepositoryTest.java",
        },
        {
            "_id": "t3", "transform_id": "tx-1", "task_id": "TASK-003", "wave": 5,
            "wave_name": "Wave 5 — Controller / API Layer", "layer": "controller",
            "action": "TRANSFORM", "status": "DONE",
            "source_path": "src/ctrl/UserController.java", "target_path": "src/api/UserController.java",
        },
    ])
    fake_transform_files.docs.extend([
        {
            "_id": ObjectId("aaaaaaaaaaaaaaaaaaaaaa01"), "transform_id": "tx-1", "type": "transformed",
            "original_path": "src/dao/UserDao.java", "path": "src/repo/UserRepository.java",
            "content": "class UserRepository {}",
        },
        {
            "_id": ObjectId("aaaaaaaaaaaaaaaaaaaaaa02"), "transform_id": "tx-1", "type": "transformed",
            "original_path": "src/test/UserDaoTest.java", "path": "src/test/UserRepositoryTest.java",
            "content": "class UserRepositoryTest {}",
        },
        {
            "_id": ObjectId("aaaaaaaaaaaaaaaaaaaaaa00"), "transform_id": "tx-1", "type": "source",
            "path": "src/dao/UserDao.java", "content": "class UserDao {}",
        },
    ])

    for mod in (db, tools_mod):
        monkeypatch.setattr(mod, "prompts", fake_prompts, raising=True)
        monkeypatch.setattr(mod, "transformations", fake_transformations, raising=True)
        monkeypatch.setattr(mod, "transformer_tasks", fake_tasks, raising=True)
        monkeypatch.setattr(mod, "transform_files", fake_transform_files, raising=True)
        monkeypatch.setattr(mod, "audit_log", fake_audit, raising=True)
        monkeypatch.setattr(mod, "tools_kb", fake_tools_kb, raising=True)

    app = FastAPI()
    app.include_router(tools_mod.router)
    return {
        "client": TestClient(app),
        "tools_mod": tools_mod,
        "tasks": fake_tasks,
        "files": fake_transform_files,
        "audit": fake_audit,
    }


def _patch_fabric_call(monkeypatch, tools_mod, actions_json: str, regenerate_content: str = "class Regenerated {}"):
    """Route chat-assistant calls to `actions_json`; route the nested
    `_regenerate_single_file_impl` call (agent_key='tools.transformer.pattern')
    to a canned "transformed content" response — mirrors real fabric_call's
    dict-with-'content' shape."""
    async def _fake_fabric_call(*, agent_key="", **kwargs):
        if agent_key == "tools.transformer.chat":
            return {"content": actions_json, "model": "test-model"}
        return {"content": regenerate_content, "model": "test-model"}
    monkeypatch.setattr(tools_mod, "fabric_call", _fake_fabric_call, raising=True)


# ── panel validation ─────────────────────────────────────────────────

def test_invalid_panel_rejected(env):
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "bogus", "message": "hi"})
    assert res.status_code == 400


def test_unknown_transform_404(env):
    res = env["client"].post("/tools/transformer/nope/agent-chat", json={"panel": "planner", "message": "hi"})
    assert res.status_code == 404


# ── planner panel: find / remove / reassign_wave ────────────────────

def test_planner_find_does_not_mutate(env, monkeypatch):
    _patch_fabric_call(monkeypatch, env["tools_mod"], '{"reply": "Found 1 test file.", "actions": [{"type": "find", "task_ids": ["TASK-002"]}]}')
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "planner", "message": "find test files"})
    assert res.status_code == 200
    body = res.json()
    assert body["matched_ids"] == ["TASK-002"]
    assert len(env["tasks"].docs) == 3  # nothing deleted


def test_planner_remove_deletes_task(env, monkeypatch):
    _patch_fabric_call(monkeypatch, env["tools_mod"], '{"reply": "Removed the test task.", "actions": [{"type": "remove", "task_ids": ["TASK-002"]}]}')
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "planner", "message": "remove all test files from wave 9"})
    assert res.status_code == 200
    remaining_ids = {d["task_id"] for d in env["tasks"].docs}
    assert "TASK-002" not in remaining_ids
    assert remaining_ids == {"TASK-001", "TASK-003"}
    assert env["audit"].docs[0]["details"]["panel"] == "planner"


def test_planner_reassign_wave_updates_wave_and_name(env, monkeypatch):
    _patch_fabric_call(
        monkeypatch, env["tools_mod"],
        '{"reply": "Moved to wave 3.", "actions": [{"type": "reassign_wave", "task_ids": ["TASK-003"], "wave": 3}]}',
    )
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "planner", "message": "move the controller to wave 3"})
    assert res.status_code == 200
    task3 = next(d for d in env["tasks"].docs if d["task_id"] == "TASK-003")
    assert task3["wave"] == 3
    assert "Persistence" in task3["wave_name"]


def test_planner_ignores_unknown_task_ids(env, monkeypatch):
    _patch_fabric_call(monkeypatch, env["tools_mod"], '{"reply": "n/a", "actions": [{"type": "remove", "task_ids": ["TASK-DOES-NOT-EXIST"]}]}')
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "planner", "message": "remove ghost task"})
    assert res.status_code == 200
    assert res.json()["matched_ids"] == []
    assert len(env["tasks"].docs) == 3


# ── coder panel: find / remove / regenerate ─────────────────────────

def test_coder_remove_deletes_generated_file_and_resets_task(env, monkeypatch):
    _patch_fabric_call(monkeypatch, env["tools_mod"], '{"reply": "Removed the DAO file.", "actions": [{"type": "remove", "task_ids": ["TASK-001"]}]}')
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "coder", "message": "remove the DAO files"})
    assert res.status_code == 200
    file_paths = {d.get("path") for d in env["files"].docs if d.get("type") == "transformed"}
    assert "src/repo/UserRepository.java" not in file_paths
    task1 = next(d for d in env["tasks"].docs if d["task_id"] == "TASK-001")
    assert task1["status"] == "PENDING"


def test_coder_regenerate_calls_regenerate_impl(env, monkeypatch):
    _patch_fabric_call(
        monkeypatch, env["tools_mod"],
        '{"reply": "Regenerating the DAO file.", "actions": [{"type": "regenerate", "task_ids": ["TASK-001"]}]}',
        regenerate_content="class UserRepositoryV2 {}",
    )
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "coder", "message": "regenerate the DAO file"})
    assert res.status_code == 200
    body = res.json()
    assert body["actions_applied"][0]["type"] == "regenerate"
    assert body["actions_applied"][0]["task_ids"] == ["TASK-001"]
    f1 = next(d for d in env["files"].docs if d["_id"] == ObjectId("aaaaaaaaaaaaaaaaaaaaaa01"))
    assert f1["content"] == "class UserRepositoryV2 {}"
    assert f1["type"] == "transformed"


# ── tester panel: find / remove (dismiss verification result) ───────

def test_tester_remove_dismisses_verification_result(env, monkeypatch):
    env["tasks"].docs[0]["status"] = "VERIFIED"
    env["tasks"].docs[0]["verifier_checks"] = {"verdict": "FAIL", "issues": ["mismatch"]}
    env["tasks"].docs[0]["verifier_score"] = 42
    _patch_fabric_call(monkeypatch, env["tools_mod"], '{"reply": "Dismissed.", "actions": [{"type": "remove", "task_ids": ["TASK-001"]}]}')
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "tester", "message": "dismiss the failed DAO verification"})
    assert res.status_code == 200
    task1 = next(d for d in env["tasks"].docs if d["task_id"] == "TASK-001")
    assert task1["status"] == "DONE"
    assert "verifier_checks" not in task1
    assert "verifier_score" not in task1


def test_action_type_not_allowed_for_panel_is_ignored(env, monkeypatch):
    # "reassign_wave" is planner-only; a coder-panel chat trying it should
    # be silently ignored (no crash, no mutation) rather than applied.
    _patch_fabric_call(
        monkeypatch, env["tools_mod"],
        '{"reply": "ok", "actions": [{"type": "reassign_wave", "task_ids": ["TASK-001"], "wave": 2}]}',
    )
    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "coder", "message": "move it to wave 2"})
    assert res.status_code == 200
    task1 = next(d for d in env["tasks"].docs if d["task_id"] == "TASK-001")
    assert task1["wave"] == 3  # unchanged


def test_no_tasks_yet_returns_friendly_message_without_llm_call(env, monkeypatch):
    env["tasks"].docs.clear()

    async def _boom(*_a, **_k):
        raise AssertionError("fabric_call should not be invoked when there are no tasks")
    monkeypatch.setattr(env["tools_mod"], "fabric_call", _boom, raising=True)

    res = env["client"].post("/tools/transformer/tx-1/agent-chat", json={"panel": "planner", "message": "remove everything"})
    assert res.status_code == 200
    assert res.json()["matched_ids"] == []

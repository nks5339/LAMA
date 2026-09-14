"""Chat endpoint — uses TOON context + active prompt library."""
from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
import uuid

from db import conversations, messages, kb_toon, prompts, project_prompts, projects, srs_documents
from models import ChatRequest, ChatMessage
from llm import (
    fabric_call as chat_completion,
    fabric_call_with_session,
    estimate_tokens, get_available_models,
)
from kb.vector_store import search as qdrant_search

router = APIRouter(prefix="/chat", tags=["chat"])


SRS_GENERATE_TRIGGERS = [
    "generate srs", "create srs", "write srs", "produce srs",
    "generate the srs", "create the document", "i have enough",
    "ready to generate", "generate requirements document",
]


def detect_intent(message: str) -> str:
    """Classify the user's intent. Returns 'srs.generate' or 'srs.gap_question'."""
    msg = (message or "").lower()
    if any(t in msg for t in SRS_GENERATE_TRIGGERS):
        return "srs.generate"
    return "srs.gap_question"


def prune_toon(toon: str, max_chars: int, stage: str) -> str:
    """Stage-aware token-efficient pruning of the TOON context.

    Thin re-export of `kb.toon.prune` — kept here for backward compat
    with existing imports and tests that reference `routes.chat.prune_toon`.
    See `backend/kb/toon.py::prune` for the canonical implementation.
    """
    from kb.toon import prune as _prune
    return _prune(toon, max_chars, stage)


async def _get_prompt(project_id: str, key: str) -> str:
    """Fetch effective prompt (project override -> global)."""
    p = await project_prompts.find_one({"project_id": project_id, "key": key}, {"_id": 0})
    if p:
        return p["template"]
    g = await prompts.find_one({"key": key}, {"_id": 0})
    return g["template"] if g else ""


@router.get("/models")
async def list_models():
    # iter-13.30 — Console-sourced. Returns active providers' catalogue when
    # Console has at least one provider configured; falls back to the
    # bootstrap enumeration in `llm.AVAILABLE_MODELS` otherwise.
    return {"models": await get_available_models()}


@router.get("/{project_id}/history")
async def get_history(project_id: str, conversation_id: str | None = None):
    q = {"project_id": project_id}
    if conversation_id:
        q["conversation_id"] = conversation_id
    docs = await messages.find(q, {"_id": 0}).sort("created_at", 1).to_list(2000)
    return docs


@router.post("")
async def send_message(req: ChatRequest):
    proj = await projects.find_one({"id": req.project_id}, {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")

    conversation_id = req.conversation_id or str(uuid.uuid4())

    # Load TOON context
    toon_doc = await kb_toon.find_one({"project_id": req.project_id}, {"_id": 0})
    toon_context = (toon_doc or {}).get("toon", "")
    summary = (toon_doc or {}).get("summary", "Knowledge base is empty.")

    # Load conversation history (last 40 messages)
    history = await messages.find(
        {"conversation_id": conversation_id},
        {"_id": 0},
    ).sort("created_at", 1).to_list(40)

    # Resolve prompt for the stage (with intent detection in Discovery, plus edit-mode)
    stage = (req.stage or "Discovery").lower()
    intent = None
    current_section_content = ""
    if req.edit_mode and req.selected_section:
        prompt_key = "srs.edit"
        srs_doc = await srs_documents.find_one({"project_id": req.project_id}, {"_id": 0})
        raw_section = (srs_doc or {}).get("sections", {}).get(req.selected_section, "") or ""
        # Truncate to keep the prompt within LLM context window, and neutralise
        # stray `{` / `}` characters in user markdown so `str.format()` doesn't fail.
        current_section_content = raw_section[:8000].replace("{", "{{").replace("}", "}}")
    else:
        intent = detect_intent(req.message) if stage == "discovery" else None
        prompt_key = intent if stage == "discovery" else f"{stage}.system"

    system_template = await _get_prompt(req.project_id, prompt_key)
    if not system_template:
        system_template = (
            "You are a legacy migration analyst for project {project_name}.\n"
            "Knowledge base summary: {summary}\n"
            "Use the structured knowledge base (TOON) below to ground your answers.\n"
            "{toon_context}"
        )

    asked_questions = "\n".join(
        f"- {m['content']}" for m in history if m["role"] == "assistant"
    )[-2000:]

    # Semantic RAG: pull chunks most relevant to THIS message
    rag_chunks = await qdrant_search(
        project_id=req.project_id,
        query=(req.message if not req.edit_mode else f"{req.selected_section or ''} {req.message}"),
        top_k=10,
    )
    rag_context = "\n\n---\n\n".join(rag_chunks) if rag_chunks else ""

    # TOON: structural skeleton (4000 chars when RAG available, else fall back to 12k)
    if rag_context:
        toon_summary = prune_toon(toon_context, 4000, req.stage)
        combined_context = (
            "STRUCTURAL OVERVIEW (TOON skeleton):\n" + toon_summary +
            "\n\nRELEVANT CODE & SCHEMA (semantic match to user message):\n" + rag_context
        )
    else:
        combined_context = prune_toon(toon_context, 12000, req.stage)

    system_message = system_template.format(
        project_name=proj.get("name", ""),
        summary=summary,
        toon_context=combined_context,
        conversation="",
        asked_questions=req.message if req.edit_mode else (asked_questions or "(none yet)"),
        selected_section=req.selected_section or "",
        current_content=current_section_content,
    )

    # Save user message
    user_msg = ChatMessage(
        conversation_id=conversation_id,
        project_id=req.project_id,
        role="user",
        content=req.message,
        tokens=estimate_tokens(req.message),
    )
    await messages.insert_one(user_msg.model_dump())

    # Build LLM message list
    llm_messages = [{"role": "system", "content": system_message}]
    for m in history:
        if m["role"] in ("user", "assistant"):
            llm_messages.append({"role": m["role"], "content": m["content"]})
    llm_messages.append({"role": "user", "content": req.message})

    try:
        # Edit-mode prompts are larger and slower — give them headroom.
        llm_timeout = 240.0 if req.edit_mode else 90.0
        # iter-13.100 — when the caller supplies a `session_id`, route
        # through the rolling-memory wrapper so the LLM sees the
        # session's summary + hydrated refs + last-K verbatim turns.
        # Otherwise behaviour is identical to pre-13.100 chat.
        if (req.session_id or "").strip():
            # The wrapper handles its own user/assistant turn-append, so
            # we drop the per-conversation history from the messages
            # list when in session mode — the session is the source of
            # truth for continuity.
            session_messages = [
                {"role": "system", "content": system_message},
                {"role": "user", "content": req.message},
            ]
            result = await fabric_call_with_session(
                messages=session_messages,
                session_id=req.session_id.strip(),
                agent_key=(intent or f"{stage}.chat"),
                project_id=req.project_id,
                model=req.model,
                timeout=llm_timeout,
            )
        else:
            result = await chat_completion(messages=llm_messages, model=req.model, timeout=llm_timeout)
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")

    # Save assistant message
    assistant_msg = ChatMessage(
        conversation_id=conversation_id,
        project_id=req.project_id,
        role="assistant",
        content=result["content"],
        model=result["model"],
        tokens=result["usage"]["total_tokens"] or estimate_tokens(result["content"]),
    )
    await messages.insert_one(assistant_msg.model_dump())

    # Ensure conversation record
    await conversations.update_one(
        {"id": conversation_id},
        {"$set": {
            "id": conversation_id,
            "project_id": req.project_id,
            "stage": req.stage,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )

    # Auto-trigger SRS generation when user asks for it
    srs_triggered = False
    if intent == "srs.generate":
        try:
            from routes.srs import generate_srs as _gen_srs
            await _gen_srs({"project_id": req.project_id, "conversation_id": conversation_id, "model": req.model})
            srs_triggered = True
        except Exception:
            srs_triggered = False

    return {
        "conversation_id": conversation_id,
        "message": assistant_msg.model_dump(),
        "usage": result["usage"],
        "intent": intent,
        "srs_triggered": srs_triggered,
        # iter-13.100 — echo back the session_id the call used so the
        # frontend can pin localStorage even if the user typed an
        # invalid id (server silently fell through to plain chat).
        "session_id": (req.session_id or "").strip(),
    }

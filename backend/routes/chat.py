"""Chat endpoint — uses TOON context + active prompt library.

Two transports share one context pipeline:

  POST /api/chat          buffered  — returns the whole reply at once
  POST /api/chat/stream   SSE       — emits phase / citation / token deltas

`_prepare_turn` builds the prompt, RAG context and history for both, so the
two paths cannot drift. Adding a stage or changing retrieval only has to
happen once.

Streaming uses `llm.fabric_call_stream`, which already handles the three
execution modes (Console routing, Factory API, Factory CLI) and degrades to
a single buffered chunk when the active provider cannot do SSE — so the
route works on every provider, it just stops being progressive.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from datetime import datetime, timezone
import asyncio
import json
import logging
import uuid

from db import conversations, messages, kb_toon, prompts, project_prompts, projects, srs_documents
from models import ChatRequest, ChatMessage
from llm import (
    fabric_call as chat_completion,
    fabric_call_with_session,
    fabric_call_stream,
    estimate_tokens, get_available_models,
)
from kb.vector_store import search as qdrant_search, search_with_sources

logger = logging.getLogger(__name__)

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


async def _prepare_turn(req: ChatRequest, *, want_sources: bool = False) -> dict:
    """Assemble everything a chat turn needs, for either transport.

    Returns a dict with the resolved conversation id, the composed system
    message, the LLM message list, the detected intent, and (when
    `want_sources`) the citation rows behind the RAG context.

    Raises HTTPException(404) if the project does not exist.
    """
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

    # Resolve prompt for the stage (intent detection in Discovery, plus edit-mode)
    stage = (req.stage or "Discovery").lower()
    intent = None
    current_section_content = ""
    if req.edit_mode and req.selected_section:
        prompt_key = "srs.edit"
        srs_doc = await srs_documents.find_one({"project_id": req.project_id}, {"_id": 0})
        raw_section = (srs_doc or {}).get("sections", {}).get(req.selected_section, "") or ""
        # Truncate to keep the prompt within the context window, and neutralise
        # stray `{` / `}` in user markdown so `str.format()` doesn't fail.
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

    # Semantic RAG: pull chunks most relevant to THIS message.
    rag_query = (
        req.message if not req.edit_mode
        else f"{req.selected_section or ''} {req.message}"
    )
    sources: list[dict] = []
    if want_sources:
        sources = await search_with_sources(
            project_id=req.project_id, query=rag_query, top_k=10,
        )
        rag_chunks = [s["content"] for s in sources if s.get("content")]
    else:
        rag_chunks = await qdrant_search(
            project_id=req.project_id, query=rag_query, top_k=10,
        )
    rag_context = "\n\n---\n\n".join(rag_chunks) if rag_chunks else ""

    # TOON: structural skeleton (4000 chars when RAG available, else 12k)
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

    llm_messages = [{"role": "system", "content": system_message}]
    for m in history:
        if m["role"] in ("user", "assistant"):
            llm_messages.append({"role": m["role"], "content": m["content"]})
    llm_messages.append({"role": "user", "content": req.message})

    return {
        "conversation_id": conversation_id,
        "stage": stage,
        "intent": intent,
        "system_message": system_message,
        "llm_messages": llm_messages,
        "sources": sources,
        "has_rag": bool(rag_context),
    }


async def _persist_user_message(req: ChatRequest, conversation_id: str) -> None:
    await messages.insert_one(ChatMessage(
        conversation_id=conversation_id,
        project_id=req.project_id,
        role="user",
        content=req.message,
        tokens=estimate_tokens(req.message),
    ).model_dump())


async def _persist_assistant_message(
    req: ChatRequest, conversation_id: str, content: str, model: str, tokens: int,
) -> dict:
    msg = ChatMessage(
        conversation_id=conversation_id,
        project_id=req.project_id,
        role="assistant",
        content=content,
        model=model,
        tokens=tokens or estimate_tokens(content),
    )
    await messages.insert_one(msg.model_dump())
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
    return msg.model_dump()


async def _maybe_trigger_srs(req: ChatRequest, conversation_id: str, intent: str | None) -> dict:
    """Auto-trigger SRS generation when the user asks for it.

    Returns {"triggered": bool, "error": str}. The previous version
    swallowed every failure into `srs_triggered = False`, so a user who
    asked for an SRS and got nothing had no way to find out why — this was
    on the P2 backlog. The reason now travels back to the caller and is
    logged server-side.
    """
    if intent != "srs.generate":
        return {"triggered": False, "error": ""}
    try:
        from routes.srs import generate_srs as _gen_srs
        await _gen_srs({
            "project_id": req.project_id,
            "conversation_id": conversation_id,
            "model": req.model,
        })
        return {"triggered": True, "error": ""}
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "SRS auto-trigger failed for project %s: %s", req.project_id, e
        )
        return {"triggered": False, "error": str(e)[:300]}


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
    turn = await _prepare_turn(req)
    conversation_id = turn["conversation_id"]
    intent = turn["intent"]
    stage = turn["stage"]

    await _persist_user_message(req, conversation_id)

    try:
        # Edit-mode prompts are larger and slower — give them headroom.
        llm_timeout = 240.0 if req.edit_mode else 90.0
        # iter-13.100 — when the caller supplies a `session_id`, route
        # through the rolling-memory wrapper so the LLM sees the session's
        # summary + hydrated refs + last-K verbatim turns. Otherwise
        # behaviour is identical to pre-13.100 chat.
        if (req.session_id or "").strip():
            # The wrapper handles its own user/assistant turn-append, so we
            # drop the per-conversation history — the session is the source
            # of truth for continuity.
            session_messages = [
                {"role": "system", "content": turn["system_message"]},
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
            result = await chat_completion(
                messages=turn["llm_messages"], model=req.model, timeout=llm_timeout,
            )
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {e}")

    assistant_msg = await _persist_assistant_message(
        req, conversation_id, result["content"], result["model"],
        result["usage"]["total_tokens"],
    )

    srs = await _maybe_trigger_srs(req, conversation_id, intent)

    return {
        "conversation_id": conversation_id,
        "message": assistant_msg,
        "usage": result["usage"],
        "intent": intent,
        "srs_triggered": srs["triggered"],
        "srs_error": srs["error"],
        # iter-13.100 — echo back the session_id the call used so the
        # frontend can pin localStorage even if the user typed an invalid
        # id (server silently fell through to plain chat).
        "session_id": (req.session_id or "").strip(),
    }


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event) + "\n\n"


@router.post("/stream")
async def send_message_stream(req: ChatRequest):
    """Streaming chat over SSE.

    Event types, in the order a client sees them:

      phase     {"phase": "retrieving"|"thinking"|"generating"}
      citation  {"filename", "filetype", "score"}   — one per RAG source
      token     {"text": "<delta>"}                 — zero or more
      complete  {"conversation_id", "message", "intent", "srs_triggered", …}
      error     {"message"}
      ping      keep-alive when a provider stalls

    A provider that cannot stream still works: `fabric_call_stream`
    degrades to one buffered chunk, so the client receives a single large
    `token` event and then `complete`. Clients must therefore append
    deltas rather than count them.

    SSE is safe here for the same reason it is on SRS and DataModel: the
    chunks are short and frequent. Architecture stays on background-task
    polling (contract #6) because its jobs exceed the 60s K8s ingress
    timeout between writes.
    """
    turn = await _prepare_turn(req, want_sources=True)
    conversation_id = turn["conversation_id"]
    intent = turn["intent"]
    stage = turn["stage"]

    await _persist_user_message(req, conversation_id)

    async def event_gen():
        collected: list[str] = []
        try:
            yield _sse({"type": "phase", "phase": "retrieving"})

            # Citations are emitted up-front: the user sees what the answer
            # is grounded in while the model is still composing it.
            seen = set()
            for src in turn["sources"]:
                name = src.get("filename") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                yield _sse({
                    "type": "citation",
                    "filename": name,
                    "filetype": src.get("filetype", ""),
                    "score": round(src.get("score", 0.0), 4),
                })

            yield _sse({"type": "phase", "phase": "thinking"})

            llm_timeout = 240.0 if req.edit_mode else 90.0
            first = True
            async for piece in fabric_call_stream(
                messages=turn["llm_messages"],
                agent_key=(intent or f"{stage}.chat"),
                project_id=req.project_id,
                model=req.model,
                timeout=llm_timeout,
            ):
                if not piece:
                    continue
                if first:
                    yield _sse({"type": "phase", "phase": "generating"})
                    first = False
                collected.append(piece)
                yield _sse({"type": "token", "text": piece})

            content = "".join(collected)
            if not content.strip():
                yield _sse({
                    "type": "error",
                    "message": "The model returned an empty response.",
                })
                return

            assistant_msg = await _persist_assistant_message(
                req, conversation_id, content, "", estimate_tokens(content),
            )

            # iter-13.100 parity — in session mode the buffered path lets
            # fabric_call_with_session append the turn. The streaming path
            # bypasses that wrapper, so append explicitly or the session
            # would silently lose every streamed exchange.
            sid = (req.session_id or "").strip()
            if sid:
                try:
                    from agent_memory import append_turn
                    await append_turn(sid, role="user", content=req.message)
                    await append_turn(sid, role="assistant", content=content)
                except Exception:  # noqa: BLE001 — never lose the reply over this
                    logger.warning(
                        "post-stream session append failed for session %s",
                        sid, exc_info=True,
                    )

            srs = await _maybe_trigger_srs(req, conversation_id, intent)

            yield _sse({
                "type": "complete",
                "conversation_id": conversation_id,
                "message": assistant_msg,
                "intent": intent,
                "srs_triggered": srs["triggered"],
                "srs_error": srs["error"],
                "session_id": sid,
                "tokens": estimate_tokens(content),
            })
        except asyncio.CancelledError:
            # Client disconnected mid-stream. Persist whatever arrived so the
            # transcript is not silently truncated on reload.
            partial = "".join(collected)
            if partial.strip():
                try:
                    await _persist_assistant_message(
                        req, conversation_id, partial, "", estimate_tokens(partial),
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("chat stream failed for project %s", req.project_id)
            yield _sse({"type": "error", "message": f"LLM call failed: {e}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

"""Qdrant vector store for semantic RAG over the knowledge base.

All operations are resilient — if Qdrant is unreachable or auth fails,
calls return gracefully (empty results / no-op) so chat and SRS keep
working with the structural TOON skeleton only.
"""
import os
import logging
import hashlib
import asyncio
from typing import Optional

logger = logging.getLogger("lama.vector")

QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
# iter-17.19 — OFFLINE Qdrant. `qdrant-client` ships an embedded engine
# (`QdrantLocal`) that persists to a plain directory with no server and no
# Docker. Set QDRANT_PATH to use it; QDRANT_URL still wins when both are
# set, so the container (which sets neither, or only URL) is unaffected.
QDRANT_PATH = os.environ.get("QDRANT_PATH", "")
# iter-17.19 — pluggable embedding backend. Default stays
# SentenceTransformer (what the image ships). `ollama` calls a local
# Ollama server's OpenAI-compatible /v1/embeddings instead, which keeps
# the offline path working WITHOUT pulling torch (~3 GB).
EMBED_BACKEND = (os.environ.get("LAMA_EMBED_BACKEND", "") or "").strip().lower()
OLLAMA_EMBED_MODEL = os.environ.get("LAMA_OLLAMA_EMBED_MODEL", "nomic-embed-text")
_OLLAMA_EMBED_BASE = (
    os.environ.get("LAMA_OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
COLLECTION = os.environ.get("LAMA_QDRANT_COLLECTION", "lama_kb")
# iter-13.18 — model-agnostic vector sizing. Default matches MiniLM-L6-v2
# (384 dims) but the actual dimensionality is now resolved from the
# loaded SentenceTransformer at startup so swapping `EMBED_MODEL` env-var
# to BGE / GTE / E5 / Nomic etc. works without a code edit. Override via
# `EMBED_DIM=…` if you want to force a value.
VECTOR_SIZE = int(os.environ.get("EMBED_DIM", "384") or "384")

_client = None
_embedder = None


def _enabled() -> bool:
    # Either transport counts: a remote server (URL) or the embedded
    # on-disk engine (PATH). Unset both → subsystem stays off exactly as
    # before, and every caller short-circuits gracefully.
    return bool(QDRANT_URL or QDRANT_PATH)


def get_client():
    global _client
    if _client is None and _enabled():
        try:
            from qdrant_client import QdrantClient
            # iter-17.19 — embedded mode. No server, no Docker, no network;
            # QdrantLocal persists to this directory. Checked BEFORE the
            # remote branch only when no URL is configured, so a configured
            # server always wins.
            if not QDRANT_URL and QDRANT_PATH:
                os.makedirs(QDRANT_PATH, exist_ok=True)
                _client = QdrantClient(path=QDRANT_PATH)
                logger.info(f"Qdrant embedded (offline) at {QDRANT_PATH}")
                return _client
            # iter-13.34 — honor LAMA_DISABLE_SSL_VERIFY / LAMA_CA_BUNDLE for
            # Qdrant too. Corporate MITM proxies (Zscaler / Netskope) inspect
            # every outbound HTTPS connection, so without this, Build KB
            # fails with [SSL: CERTIFICATE_VERIFY_FAILED] when QDRANT_URL is
            # https. QdrantClient forwards extra kwargs to the underlying
            # httpx client, so verify=False / verify="<path>" both work.
            from llm import _http_verify
            verify_arg = _http_verify()
            _client = QdrantClient(
                url=QDRANT_URL,
                api_key=QDRANT_API_KEY or None,
                timeout=30,
                verify=verify_arg,
            )
        except Exception as e:
            logger.warning(f"Qdrant client init failed: {e}")
            _client = None
    return _client


def _ollama_embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed via a local Ollama server's OpenAI-compatible endpoint.

    iter-17.19 — lets the offline Qdrant path work without torch. Ollama
    accepts a list `input` and returns one vector per item, in order.
    Returns None on any failure so callers degrade exactly as they do
    when SentenceTransformer is unavailable.
    """
    try:
        import httpx
        r = httpx.post(
            f"{_OLLAMA_EMBED_BASE}/embeddings",
            json={"model": OLLAMA_EMBED_MODEL, "input": texts},
            timeout=120,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
        if len(rows) != len(texts):
            logger.error(
                "Ollama embeddings returned %d vectors for %d inputs",
                len(rows), len(texts),
            )
            return None
        return [row["embedding"] for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.error(f"Ollama embedding failed: {e}")
        return None


def get_embedder():
    global _embedder, VECTOR_SIZE
    # iter-17.19 — Ollama backend needs no local model object. Probe once
    # so VECTOR_SIZE matches the model's true width (nomic-embed-text is
    # 768, not MiniLM's 384) and a misconfigured EMBED_DIM can't silently
    # create a collection of the wrong dimensionality.
    if EMBED_BACKEND == "ollama":
        if _embedder is None:
            probe = _ollama_embed(["dimension probe"])
            if not probe:
                logger.warning("Ollama embedding backend unreachable — vectors disabled")
                return None
            VECTOR_SIZE = len(probe[0])
            logger.info(
                f"Ollama embedder ready: {OLLAMA_EMBED_MODEL} (dim={VECTOR_SIZE})"
            )
            _embedder = "ollama"
        return _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {EMBED_MODEL}")
            _embedder = SentenceTransformer(EMBED_MODEL)
            # Resolve true dimensionality from the model so swapping
            # EMBED_MODEL doesn't require code changes (iter-13.18).
            try:
                dim = _embedder.get_sentence_embedding_dimension()
                if isinstance(dim, int) and dim > 0:
                    VECTOR_SIZE = dim
                    logger.info(f"Embedding dim resolved to {VECTOR_SIZE}")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"SentenceTransformer load failed: {e}")
            _embedder = None
    return _embedder


def _stable_id(chunk_id: str) -> int:
    """Convert string chunk ID to a stable uint64."""
    return int(hashlib.md5(chunk_id.encode()).hexdigest()[:16], 16)


async def ensure_collection() -> bool:
    client = get_client()
    if client is None:
        return False
    try:
        from qdrant_client import models
        existing = [c.name for c in client.get_collections().collections]
        if COLLECTION not in existing:
            client.create_collection(
                collection_name=COLLECTION,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )
            logger.info(f"Created Qdrant collection: {COLLECTION}")
        return True
    except Exception as e:
        logger.warning(f"Qdrant ensure_collection failed: {e}")
        return False


def _embed_batch(texts: list[str]) -> Optional[list[list[float]]]:
    embedder = get_embedder()
    if embedder is None:
        return None
    if embedder == "ollama":
        return _ollama_embed(texts)
    try:
        return embedder.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True).tolist()
    except Exception as e:
        logger.error(f"Embedding batch failed: {e}")
        return None


async def index_chunks(project_id: str, chunks: list[dict]) -> int:
    """Embed and upsert chunks. Returns number successfully indexed."""
    if not chunks or not _enabled():
        return 0
    # iter-17.19 — resolve the embedder FIRST. Both backends discover the
    # model's true width here and update VECTOR_SIZE; creating the
    # collection before that pins it to the 384 default and every upsert
    # then fails with "could not broadcast (768,) into (384,)". Latent
    # while EMBED_MODEL happened to be 384-wide MiniLM.
    if get_embedder() is None:
        return 0
    ok = await ensure_collection()
    if not ok:
        return 0
    client = get_client()
    if client is None:
        return 0
    from qdrant_client import models

    indexed = 0
    BATCH = 256
    loop = asyncio.get_event_loop()
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        texts = [c["content"] for c in batch]
        # Run blocking embedding off the event loop
        vectors = await loop.run_in_executor(None, _embed_batch, texts)
        if vectors is None:
            continue
        points = [
            models.PointStruct(
                id=_stable_id(c["id"]),
                vector=vectors[j],
                payload={
                    "project_id": project_id,
                    "chunk_id": c["id"],
                    "content": c["content"],
                    "filename": c.get("filename", ""),
                    "filetype": c.get("filetype", ""),
                    # iter-13.29 — graph entity refs persisted so retrieval
                    # can filter "chunks containing class X / method Y /
                    # table Z" deterministically, not only via cosine.
                    "entity_refs": c.get("entity_refs") or {},
                    "class_names": c.get("class_names") or [],
                    "method_names": c.get("method_names") or [],
                    "table_names": c.get("table_names") or [],
                    "route_paths": c.get("route_paths") or [],
                    "roles": c.get("roles") or [],
                },
            )
            for j, c in enumerate(batch)
        ]
        try:
            await loop.run_in_executor(None, lambda: client.upsert(collection_name=COLLECTION, points=points))
            indexed += len(points)
        except Exception as e:
            logger.error(f"Qdrant upsert batch {i} failed: {e}")
    return indexed


async def search(project_id: str, query: str, top_k: int = 8) -> list[str]:
    """Return top-K relevant chunk contents. Returns [] on any failure."""
    if not _enabled() or not query:
        return []
    client = get_client()
    if client is None:
        return []
    embedder = get_embedder()
    if embedder is None:
        return []
    try:
        from qdrant_client import models
        loop = asyncio.get_event_loop()
        # Route through _embed_batch so this honours LAMA_EMBED_BACKEND;
        # calling embedder.encode() directly breaks any non-SentenceTransformer
        # backend (the Ollama one carries no .encode()).
        vectors = await loop.run_in_executor(None, lambda: _embed_batch([query]))
        if not vectors:
            return []
        vector = vectors[0]
        query_filter = models.Filter(
            must=[models.FieldCondition(
                key="project_id",
                match=models.MatchValue(value=project_id),
            )]
        )

        def _do_search():
            # qdrant-client >= 1.10 uses query_points; older uses search.
            if hasattr(client, "query_points"):
                res = client.query_points(
                    collection_name=COLLECTION,
                    query=vector,
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                )
                # query_points returns a wrapper with .points
                return getattr(res, "points", res)
            return client.search(
                collection_name=COLLECTION,
                query_vector=vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
            )

        results = await loop.run_in_executor(None, _do_search)
        return [r.payload.get("content", "") for r in results if r.payload]
    except Exception as e:
        logger.warning(f"Qdrant search failed, returning empty: {e}")
        return []


async def search_with_sources(
    project_id: str, query: str, top_k: int = 8
) -> list[dict]:
    """Like `search`, but keeps the payload metadata the plain variant drops.

    `search` returns bare content strings, which is all the buffered chat
    path ever needed. The streaming chat route surfaces citations to the
    user, and a citation needs a name — so this returns the filename and
    filetype alongside the content. Returns [] on any failure, exactly as
    `search` does, so callers degrade to TOON-only.
    """
    if not _enabled() or not query:
        return []
    client = get_client()
    if client is None:
        return []
    embedder = get_embedder()
    if embedder is None:
        return []
    try:
        from qdrant_client import models
        loop = asyncio.get_event_loop()
        vectors = await loop.run_in_executor(None, lambda: _embed_batch([query]))
        if not vectors:
            return []
        vector = vectors[0]
        query_filter = models.Filter(
            must=[models.FieldCondition(
                key="project_id",
                match=models.MatchValue(value=project_id),
            )]
        )

        def _do_search():
            if hasattr(client, "query_points"):
                res = client.query_points(
                    collection_name=COLLECTION,
                    query=vector,
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                )
                return getattr(res, "points", res)
            return client.search(
                collection_name=COLLECTION,
                query_vector=vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
            )

        results = await loop.run_in_executor(None, _do_search)
        out = []
        for r in results:
            if not r.payload:
                continue
            out.append({
                "content": r.payload.get("content", "") or "",
                "filename": r.payload.get("filename", "") or "",
                "filetype": r.payload.get("filetype", "") or "",
                "chunk_id": r.payload.get("chunk_id", "") or "",
                "score": float(getattr(r, "score", 0.0) or 0.0),
            })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Qdrant search_with_sources failed, returning empty: {e}")
        return []


async def search_many(
    project_id: str,
    queries: list[str],
    per_query_k: int = 8,
    final_k: int = 20,
) -> list[str]:
    """Multi-facet retrieval (iter-13.18).

    Runs N short queries in parallel-ish, dedupes by chunk_id + content hash,
    and returns up to `final_k` unique chunks ranked by best-rank-across-queries.

    Use this from SRS / CodeGen instead of single-query `search` when a section
    has multiple distinct facets to cover (e.g. "user roles", "validation rules",
    "payment flow" for one section) — same Qdrant cost, materially higher recall.

    Falls back to empty list on any failure — caller can degrade to TOON-only.
    """
    queries = [q for q in (queries or []) if q and q.strip()]
    if not queries or not _enabled():
        return []
    client = get_client()
    embedder = get_embedder()
    if client is None or embedder is None:
        return []
    try:
        from qdrant_client import models
        loop = asyncio.get_event_loop()
        # Same reason as in `search()` — go via _embed_batch so the
        # configured backend is honoured.
        vectors = await loop.run_in_executor(None, lambda: _embed_batch(queries))
        if not vectors:
            return []
        query_filter = models.Filter(
            must=[models.FieldCondition(
                key="project_id", match=models.MatchValue(value=project_id),
            )]
        )

        # Best rank across queries per chunk_id; lower rank == more relevant.
        best_rank: dict[str, int] = {}
        content_by_id: dict[str, str] = {}
        seen_content: set[str] = set()

        def _do_one(vec):
            if hasattr(client, "query_points"):
                res = client.query_points(
                    collection_name=COLLECTION,
                    query=vec,
                    query_filter=query_filter,
                    limit=per_query_k,
                    with_payload=True,
                )
                return getattr(res, "points", res)
            return client.search(
                collection_name=COLLECTION,
                query_vector=vec,
                query_filter=query_filter,
                limit=per_query_k,
                with_payload=True,
            )

        for vec in vectors:
            try:
                results = await loop.run_in_executor(None, lambda v=vec: _do_one(v))
            except Exception as e:
                logger.warning(f"Qdrant sub-query failed: {e}")
                continue
            for rank, r in enumerate(results):
                if not r.payload:
                    continue
                cid = r.payload.get("chunk_id") or str(getattr(r, "id", ""))
                content = r.payload.get("content", "") or ""
                # Content-level dedup: collapse identical bodies that
                # survived the index-time dedup (different chunk_id,
                # same payload — happens on JSP includes).
                content_key = " ".join(content.split())[:200]
                if content_key in seen_content:
                    continue
                seen_content.add(content_key)
                prev = best_rank.get(cid)
                if prev is None or rank < prev:
                    best_rank[cid] = rank
                    content_by_id[cid] = content

        ordered = sorted(best_rank.items(), key=lambda kv: kv[1])
        return [content_by_id[cid] for cid, _ in ordered[:final_k] if content_by_id.get(cid)]
    except Exception as e:
        logger.warning(f"Qdrant search_many failed: {e}")
        return []


async def search_by_entities(
    project_id: str,
    class_names: list[str] | None = None,
    method_names: list[str] | None = None,
    table_names: list[str] | None = None,
    route_paths: list[str] | None = None,
    roles: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Graph-first retrieval (iter-13.29).

    Pulls chunks whose payload references ANY of the named entities — no
    embedding, no cosine. This is the deterministic "give me every chunk
    that mentions OrderController" path, used by codegen / gap-recovery
    BEFORE vector top-up.

    Returns list of ``{content, filename, entity_refs}`` dicts, or [] on
    any failure / when Qdrant is disabled.
    """
    if not _enabled():
        return []
    client = get_client()
    if client is None:
        return []
    # Build OR filter across the typed fields.
    try:
        from qdrant_client import models
        should: list = []
        for field, values in (
            ("class_names", class_names),
            ("method_names", method_names),
            ("table_names", table_names),
            ("route_paths", route_paths),
            ("roles", roles),
        ):
            for v in (values or []):
                if not v:
                    continue
                should.append(models.FieldCondition(
                    key=field, match=models.MatchValue(value=v),
                ))
        if not should:
            return []
        q_filter = models.Filter(
            must=[models.FieldCondition(
                key="project_id", match=models.MatchValue(value=project_id),
            )],
            should=should,
        )
        loop = asyncio.get_event_loop()

        def _do_scroll():
            offset = None
            out: list = []
            while True:
                res = client.scroll(
                    collection_name=COLLECTION,
                    scroll_filter=q_filter,
                    limit=min(64, limit - len(out)),
                    with_payload=True,
                    offset=offset,
                )
                points, offset = res if isinstance(res, tuple) else (getattr(res, "points", res), None)
                if not points:
                    break
                out.extend(points)
                if len(out) >= limit or offset is None:
                    break
            return out[:limit]

        points = await loop.run_in_executor(None, _do_scroll)
        return [{
            "content": p.payload.get("content", ""),
            "filename": p.payload.get("filename", ""),
            "filetype": p.payload.get("filetype", ""),
            "entity_refs": p.payload.get("entity_refs", {}),
        } for p in points if p and p.payload]
    except Exception as e:
        logger.warning(f"Qdrant search_by_entities failed: {e}")
        return []


async def delete_project_vectors(project_id: str) -> bool:
    if not _enabled():
        return False
    client = get_client()
    if client is None:
        return False
    try:
        from qdrant_client import models
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: client.delete(
                collection_name=COLLECTION,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[models.FieldCondition(
                            key="project_id",
                            match=models.MatchValue(value=project_id),
                        )]
                    )
                ),
            ),
        )
        return True
    except Exception as e:
        logger.warning(f"Qdrant delete failed: {e}")
        return False

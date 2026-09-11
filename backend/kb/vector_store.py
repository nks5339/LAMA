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
    return bool(QDRANT_URL)


def get_client():
    global _client
    if _client is None and _enabled():
        try:
            from qdrant_client import QdrantClient
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


def get_embedder():
    global _embedder, VECTOR_SIZE
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
    try:
        return embedder.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True).tolist()
    except Exception as e:
        logger.error(f"Embedding batch failed: {e}")
        return None


async def index_chunks(project_id: str, chunks: list[dict]) -> int:
    """Embed and upsert chunks. Returns number successfully indexed."""
    if not chunks or not _enabled():
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
        vector = await loop.run_in_executor(
            None,
            lambda: embedder.encode([query], normalize_embeddings=True)[0].tolist(),
        )
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
        vectors = await loop.run_in_executor(
            None,
            lambda: embedder.encode(queries, normalize_embeddings=True).tolist(),
        )
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

"""Stage 2 deterministic generators (iter-13.44).

Hybrid pipeline:
    Bus Matrix    →  LLM  (kept — needs judgement)
    OLTP DDL      →  deterministic Python generator (this package)
    OLAP DDL      →  deterministic Python generator (this package)
    Migration 3x  →  deterministic Python generator (this package)
    ENUM polish   →  optional small LLM call
    COMMENT ON    →  optional small LLM call

Each generator is pure (no I/O) — callers pass kb_entities / artifacts in,
get DDL / script strings out. Toggle via `datamodel.config.resolve_mode()`.
"""


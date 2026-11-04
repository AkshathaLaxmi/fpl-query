-- 011_retrieval.sql — the retrieval corpus, embedded (FPL-9).
--
-- corpus/*.yaml is the source of truth; this table is a derived index of it,
-- rebuilt by `fplq index`. One row per chunk (see src/fplq/corpus.py): a view,
-- a function, a glossary term or an example.
--
-- It lives in its own schema, and fplq_reader is granted NOTHING on it. The
-- application reads it to build the prompt; generated SQL must never reach it.
-- That is the same three-layer arrangement as core and raw: the model is not
-- told about it, the validator's schema allow-list refuses it, and the role
-- generated SQL runs as cannot see it. See docs/vector-store.md.
--
-- The pgvector extension is created by `fplq bootstrap` as the admin role,
-- because an extension needs a superuser and migrations run as the writer.

CREATE SCHEMA IF NOT EXISTS retrieval;
REVOKE ALL ON SCHEMA retrieval FROM PUBLIC;

CREATE TABLE retrieval.chunk (
    chunk_id        TEXT PRIMARY KEY,           -- e.g. 'view:analytics.player_season'
    kind            TEXT NOT NULL CHECK (kind IN ('view', 'function', 'term', 'example')),
    content         TEXT NOT NULL,              -- exactly the text that was embedded
    content_sha256  TEXT NOT NULL,
    -- Width must match Settings.embedding_dimensions. Titan v2 at 1024.
    embedding       vector(1024) NOT NULL,
    model           TEXT NOT NULL,
    embedded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cosine distance, because Titan's vectors are requested normalised. At tens
-- of chunks a sequential scan would do; the index is here so the query plan
-- does not change shape if the corpus grows.
CREATE INDEX chunk_embedding_hnsw ON retrieval.chunk
    USING hnsw (embedding vector_cosine_ops);

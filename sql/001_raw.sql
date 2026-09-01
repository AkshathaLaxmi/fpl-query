-- 001_raw.sql — immutable landing zone.
--
-- Nothing in here is ever UPDATEd. Every ingestion run appends rows tagged with
-- the batch that produced them. If a transform has a bug we fix the transform
-- and replay from raw; we never re-fetch from a source that may have changed
-- underneath us. Payloads are stored as JSONB exactly as received.

CREATE SCHEMA IF NOT EXISTS raw;

-- One row per ingestion run. Everything raw is traceable back to a batch.
CREATE TABLE raw.ingest_batch (
    batch_id        BIGSERIAL PRIMARY KEY,
    source          TEXT        NOT NULL,   -- 'fpl_api' | 'fpl_archive' | 'football_data'
    endpoint        TEXT        NOT NULL,   -- 'bootstrap-static', 'gws/merged_gw.csv', ...
    season          TEXT,                   -- '2025-26' where the source is season-scoped
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'succeeded', 'failed')),
    row_count       INTEGER,
    source_uri      TEXT,
    content_sha256  TEXT,                   -- lets us skip a transform when nothing changed
    error           TEXT
);

CREATE INDEX raw_ingest_batch_source_idx
    ON raw.ingest_batch (source, endpoint, season, requested_at DESC);

-- Generic document landing. One row per logical record, payload untouched.
-- record_key is the source's own identifier for the record, used only for
-- debugging and replay slicing -- it carries no cross-source meaning.
CREATE TABLE raw.document (
    document_id     BIGSERIAL PRIMARY KEY,
    batch_id        BIGINT      NOT NULL REFERENCES raw.ingest_batch (batch_id),
    record_type     TEXT        NOT NULL,   -- 'element', 'team', 'fixture', 'gw_stat'
    record_key      TEXT,
    observed_at     TIMESTAMPTZ NOT NULL,   -- when this state was true, not when we stored it
    payload         JSONB       NOT NULL
);

CREATE INDEX raw_document_batch_idx  ON raw.document (batch_id);
CREATE INDEX raw_document_type_idx   ON raw.document (record_type, observed_at DESC);
CREATE INDEX raw_document_key_idx    ON raw.document (record_type, record_key);

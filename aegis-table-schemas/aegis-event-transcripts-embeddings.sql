-- Schema generated from scripts/create_retrieval_tables.py --source event_transcripts.
-- This table is referenced by the event_transcripts subagent but was not present in
-- the local Postgres database exported on 2026-06-15.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public."aegis-event-transcripts-embeddings" (
    embedding_id text NOT NULL,
    embedding_type text NOT NULL,
    embedding_scope text NOT NULL,
    source_type text NOT NULL,
    fiscal_year text NOT NULL,
    quarter text NOT NULL,
    bank text NOT NULL,
    filename text NOT NULL,
    file_id text NOT NULL,
    file_type text NOT NULL,
    file_path text NOT NULL,
    file_hash text NOT NULL,
    content_unit_id text,
    content_unit_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    chunk_id text,
    section_id text,
    embedding_text text NOT NULL,
    text_hash text,
    embedding vector(3072),
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (embedding_id)
);

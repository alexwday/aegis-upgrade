-- Schema generated from scripts/create_retrieval_tables.py --source transcripts.
-- This table is referenced by the transcripts subagent but was not present in
-- the local Postgres database exported on 2026-06-15.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public."aegis-earnings-transcripts-data" (
    source_type text NOT NULL,
    fiscal_year text NOT NULL,
    quarter text NOT NULL,
    bank text NOT NULL,
    filename text NOT NULL,
    file_id text NOT NULL,
    file_type text NOT NULL,
    file_path text NOT NULL,
    file_hash text NOT NULL,
    page_number integer,
    name text,
    summary text,
    chunk_id text NOT NULL,
    chunk_content text,
    keywords jsonb NOT NULL DEFAULT '[]'::jsonb,
    metrics jsonb NOT NULL DEFAULT '[]'::jsonb,
    keyword_embedding vector(3072),
    metric_embedding vector(3072),
    summary_embedding vector(3072),
    chunk_embedding vector(3072),
    created_at timestamptz,
    PRIMARY KEY (file_id, chunk_id)
);

-- Shared original source document byte store for Aegis retrieval sources.

CREATE TABLE IF NOT EXISTS public.aegis_source_documents (
    source_type text NOT NULL,
    file_id text NOT NULL,
    fiscal_year text NOT NULL,
    quarter text NOT NULL,
    bank text NOT NULL,
    filename text NOT NULL,
    file_type text NOT NULL,
    file_path text NOT NULL,
    mime_type text NOT NULL,
    file_hash text NOT NULL,
    file_size bigint NOT NULL,
    date_last_modified timestamp with time zone,
    original_bytes bytea NOT NULL,
    preview_mime_type text,
    preview_bytes bytea,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT aegis_source_documents_pkey PRIMARY KEY (source_type, file_id),
    CONSTRAINT aegis_source_documents_file_size_check CHECK (file_size >= 0)
);

CREATE INDEX IF NOT EXISTS idx_aegis_source_documents_bank_period
    ON public.aegis_source_documents USING btree
    (source_type, bank, fiscal_year, quarter);

CREATE INDEX IF NOT EXISTS idx_aegis_source_documents_file_hash
    ON public.aegis_source_documents USING btree (file_hash);

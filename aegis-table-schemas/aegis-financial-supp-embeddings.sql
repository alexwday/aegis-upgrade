--
-- PostgreSQL database dump
--

-- Dumped from database version 15.12 (Homebrew)
-- Dumped by pg_dump version 15.12 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: aegis-financial-supp-embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."aegis-financial-supp-embeddings" (
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
    content_unit_ids jsonb DEFAULT '[]'::jsonb NOT NULL,
    chunk_id text,
    section_id text,
    embedding_text text NOT NULL,
    text_hash text,
    embedding public.vector(3072),
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: aegis-financial-supp-embeddings aegis-financial-supp-embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."aegis-financial-supp-embeddings"
    ADD CONSTRAINT "aegis-financial-supp-embeddings_pkey" PRIMARY KEY (embedding_id);


--
-- Name: idx_fin_supp_embeddings_type_bank_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_embeddings_type_bank_period ON public."aegis-financial-supp-embeddings" USING btree (embedding_type, bank, fiscal_year, quarter);


--
-- Name: idx_fin_supp_embeddings_type_file_chunk; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_embeddings_type_file_chunk ON public."aegis-financial-supp-embeddings" USING btree (embedding_type, file_id, COALESCE(chunk_id, content_unit_id));


--
-- PostgreSQL database dump complete
--


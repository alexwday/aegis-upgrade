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
-- Name: aegis-pillar3-embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."aegis-pillar3-embeddings" (
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
-- Name: aegis-pillar3-embeddings aegis-pillar3-embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."aegis-pillar3-embeddings"
    ADD CONSTRAINT "aegis-pillar3-embeddings_pkey" PRIMARY KEY (embedding_id);


--
-- PostgreSQL database dump complete
--


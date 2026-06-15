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
-- Name: aegis-investor-slides-data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."aegis-investor-slides-data" (
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
    keywords jsonb DEFAULT '[]'::jsonb NOT NULL,
    metrics jsonb DEFAULT '[]'::jsonb NOT NULL,
    keyword_embedding public.vector(3072),
    metric_embedding public.vector(3072),
    summary_embedding public.vector(3072),
    chunk_embedding public.vector(3072),
    created_at timestamp with time zone
);


--
-- Name: aegis-investor-slides-data aegis-investor-slides-data_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."aegis-investor-slides-data"
    ADD CONSTRAINT "aegis-investor-slides-data_pkey" PRIMARY KEY (file_id, chunk_id);


--
-- PostgreSQL database dump complete
--


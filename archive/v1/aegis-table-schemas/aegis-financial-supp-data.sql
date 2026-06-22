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
-- Name: aegis-financial-supp-data; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."aegis-financial-supp-data" (
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
-- Name: aegis-financial-supp-data aegis-financial-supp-data_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."aegis-financial-supp-data"
    ADD CONSTRAINT "aegis-financial-supp-data_pkey" PRIMARY KEY (file_id, chunk_id);


--
-- Name: idx_fin_supp_data_bank_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_data_bank_period ON public."aegis-financial-supp-data" USING btree (bank, fiscal_year, quarter);


--
-- Name: idx_fin_supp_data_file_chunk_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_data_file_chunk_pattern ON public."aegis-financial-supp-data" USING btree (file_id, chunk_id text_pattern_ops);


--
-- Name: idx_fin_supp_data_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_data_fts ON public."aegis-financial-supp-data" USING gin ((((((setweight(to_tsvector('english'::regconfig, COALESCE(name, ''::text)), 'A'::"char") || setweight(to_tsvector('english'::regconfig, COALESCE(summary, ''::text)), 'A'::"char")) || setweight(to_tsvector('english'::regconfig, COALESCE((keywords)::text, ''::text)), 'B'::"char")) || setweight(to_tsvector('english'::regconfig, COALESCE((metrics)::text, ''::text)), 'B'::"char")) || setweight(to_tsvector('english'::regconfig, COALESCE(chunk_content, ''::text)), 'C'::"char"))));


--
-- Name: idx_fin_supp_data_keywords_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_data_keywords_trgm ON public."aegis-financial-supp-data" USING gin (COALESCE((keywords)::text, ''::text) public.gin_trgm_ops);


--
-- Name: idx_fin_supp_data_metrics_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fin_supp_data_metrics_trgm ON public."aegis-financial-supp-data" USING gin (COALESCE((metrics)::text, ''::text) public.gin_trgm_ops);


--
-- PostgreSQL database dump complete
--


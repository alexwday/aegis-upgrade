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
-- Name: aegis_transcripts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.aegis_transcripts (
    id integer NOT NULL,
    file_path text,
    filename text,
    date_last_modified timestamp with time zone,
    title text,
    transcript_type text,
    event_id text,
    version_id text,
    fiscal_year integer NOT NULL,
    fiscal_quarter text NOT NULL,
    institution_type text,
    institution_id text,
    ticker text NOT NULL,
    company_name text,
    section_name text,
    speaker_block_id integer,
    qa_group_id integer,
    classification_ids text[],
    classification_names text[],
    block_summary text,
    chunk_id integer,
    chunk_tokens integer,
    chunk_content text,
    chunk_paragraph_ids text[],
    chunk_embedding public.vector(3072),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: aegis_transcripts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.aegis_transcripts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: aegis_transcripts_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.aegis_transcripts_id_seq OWNED BY public.aegis_transcripts.id;


--
-- Name: aegis_transcripts id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_transcripts ALTER COLUMN id SET DEFAULT nextval('public.aegis_transcripts_id_seq'::regclass);


--
-- Name: aegis_transcripts aegis_transcripts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_transcripts
    ADD CONSTRAINT aegis_transcripts_pkey PRIMARY KEY (id);


--
-- PostgreSQL database dump complete
--


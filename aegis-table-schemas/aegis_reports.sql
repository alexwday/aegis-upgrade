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
-- Name: aegis_reports; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.aegis_reports (
    id integer NOT NULL,
    report_name character varying(200) NOT NULL,
    report_description text NOT NULL,
    report_type character varying(100) NOT NULL,
    bank_id integer NOT NULL,
    bank_name character varying(100) NOT NULL,
    bank_symbol character varying(10) NOT NULL,
    fiscal_year integer NOT NULL,
    quarter character varying(2) NOT NULL,
    local_filepath text,
    s3_document_name text,
    s3_pdf_name text,
    markdown_content text,
    generation_date timestamp without time zone NOT NULL,
    date_last_modified timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    generated_by character varying(100),
    execution_id uuid,
    metadata jsonb,
    CONSTRAINT aegis_reports_quarter_check CHECK (((quarter)::text = ANY (ARRAY[('Q1'::character varying)::text, ('Q2'::character varying)::text, ('Q3'::character varying)::text, ('Q4'::character varying)::text])))
);


--
-- Name: aegis_reports_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.aegis_reports_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: aegis_reports_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.aegis_reports_id_seq OWNED BY public.aegis_reports.id;


--
-- Name: aegis_reports id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_reports ALTER COLUMN id SET DEFAULT nextval('public.aegis_reports_id_seq'::regclass);


--
-- Name: aegis_reports aegis_reports_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_reports
    ADD CONSTRAINT aegis_reports_pkey PRIMARY KEY (id);


--
-- Name: aegis_reports aegis_reports_unique_report; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_reports
    ADD CONSTRAINT aegis_reports_unique_report UNIQUE (bank_id, fiscal_year, quarter, report_type);


--
-- Name: idx_aegis_reports_bank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_reports_bank ON public.aegis_reports USING btree (bank_id);


--
-- Name: idx_aegis_reports_bank_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_reports_bank_period ON public.aegis_reports USING btree (bank_id, fiscal_year, quarter);


--
-- Name: idx_aegis_reports_generation_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_reports_generation_date ON public.aegis_reports USING btree (generation_date);


--
-- Name: idx_aegis_reports_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_reports_period ON public.aegis_reports USING btree (fiscal_year, quarter);


--
-- Name: idx_aegis_reports_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_reports_type ON public.aegis_reports USING btree (report_type);


--
-- PostgreSQL database dump complete
--


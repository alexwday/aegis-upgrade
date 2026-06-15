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
-- Name: aegis_data_availability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.aegis_data_availability (
    id integer NOT NULL,
    bank_id integer NOT NULL,
    bank_name character varying(100) NOT NULL,
    bank_symbol character varying(10) NOT NULL,
    bank_aliases text[],
    bank_tags text[],
    fiscal_year integer NOT NULL,
    quarter character varying(2) NOT NULL,
    database_names text[],
    last_updated timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    last_updated_by character varying(100),
    CONSTRAINT aegis_data_availability_quarter_check CHECK (((quarter)::text = ANY ((ARRAY['Q1'::character varying, 'Q2'::character varying, 'Q3'::character varying, 'Q4'::character varying])::text[])))
);


--
-- Name: aegis_data_availability_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.aegis_data_availability_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: aegis_data_availability_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.aegis_data_availability_id_seq OWNED BY public.aegis_data_availability.id;


--
-- Name: aegis_data_availability id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_data_availability ALTER COLUMN id SET DEFAULT nextval('public.aegis_data_availability_id_seq'::regclass);


--
-- Name: aegis_data_availability aegis_data_availability_bank_id_fiscal_year_quarter_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_data_availability
    ADD CONSTRAINT aegis_data_availability_bank_id_fiscal_year_quarter_key UNIQUE (bank_id, fiscal_year, quarter);


--
-- Name: aegis_data_availability aegis_data_availability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.aegis_data_availability
    ADD CONSTRAINT aegis_data_availability_pkey PRIMARY KEY (id);


--
-- Name: idx_aegis_bank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_bank ON public.aegis_data_availability USING btree (bank_id);


--
-- Name: idx_aegis_bank_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_bank_period ON public.aegis_data_availability USING btree (bank_id, fiscal_year, quarter);


--
-- Name: idx_aegis_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aegis_period ON public.aegis_data_availability USING btree (fiscal_year, quarter);


--
-- PostgreSQL database dump complete
--


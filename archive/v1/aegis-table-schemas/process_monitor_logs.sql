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
-- Name: process_monitor_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.process_monitor_logs (
    log_id bigint NOT NULL,
    run_uuid uuid NOT NULL,
    model_name character varying(100) NOT NULL,
    stage_name character varying(100) NOT NULL,
    stage_start_time timestamp with time zone NOT NULL,
    stage_end_time timestamp with time zone,
    duration_ms integer,
    llm_calls jsonb,
    total_tokens integer,
    total_cost numeric(12,6),
    status character varying(255),
    decision_details text,
    error_message text,
    log_timestamp timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    user_id character varying(255),
    environment character varying(50),
    custom_metadata jsonb,
    notes text
);


--
-- Name: process_monitor_logs_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.process_monitor_logs_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: process_monitor_logs_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.process_monitor_logs_log_id_seq OWNED BY public.process_monitor_logs.log_id;


--
-- Name: process_monitor_logs log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_monitor_logs ALTER COLUMN log_id SET DEFAULT nextval('public.process_monitor_logs_log_id_seq'::regclass);


--
-- Name: process_monitor_logs process_monitor_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_monitor_logs
    ADD CONSTRAINT process_monitor_logs_pkey PRIMARY KEY (log_id);


--
-- Name: idx_process_monitor_logs_environment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_environment ON public.process_monitor_logs USING btree (environment);


--
-- Name: idx_process_monitor_logs_model_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_model_name ON public.process_monitor_logs USING btree (model_name);


--
-- Name: idx_process_monitor_logs_model_stage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_model_stage ON public.process_monitor_logs USING btree (model_name, stage_name);


--
-- Name: idx_process_monitor_logs_run_uuid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_run_uuid ON public.process_monitor_logs USING btree (run_uuid);


--
-- Name: idx_process_monitor_logs_stage_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_stage_name ON public.process_monitor_logs USING btree (stage_name);


--
-- Name: idx_process_monitor_logs_stage_start_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_stage_start_time ON public.process_monitor_logs USING btree (stage_start_time);


--
-- Name: idx_process_monitor_logs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_monitor_logs_status ON public.process_monitor_logs USING btree (status);


--
-- PostgreSQL database dump complete
--


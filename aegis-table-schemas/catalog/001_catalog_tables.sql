CREATE TABLE IF NOT EXISTS public.data_source_registry (
    data_source_name text PRIMARY KEY,
    data_source_display_name text NOT NULL,
    data_source_description text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public.monitored_institutions (
    bank_ticker text PRIMARY KEY,
    bank_name text NOT NULL UNIQUE,
    bank_display_name text NOT NULL,
    bank_category text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public.data_source_availability (
    bank_ticker text NOT NULL REFERENCES public.monitored_institutions(bank_ticker) ON DELETE CASCADE,
    fiscal_year integer NOT NULL,
    quarter text NOT NULL,
    data_source_list text[] NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT data_source_availability_pkey PRIMARY KEY (bank_ticker, fiscal_year, quarter),
    CONSTRAINT data_source_availability_quarter_check CHECK (quarter = ANY (ARRAY['Q1', 'Q2', 'Q3', 'Q4'])),
    CONSTRAINT data_source_availability_source_list_nonempty_check CHECK (cardinality(data_source_list) > 0)
);

CREATE INDEX IF NOT EXISTS idx_data_source_availability_period
    ON public.data_source_availability (fiscal_year, quarter);

CREATE INDEX IF NOT EXISTS idx_data_source_availability_sources
    ON public.data_source_availability USING gin (data_source_list);

CREATE OR REPLACE FUNCTION public.validate_data_source_availability_sources()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    missing_sources text[];
BEGIN
    SELECT array_agg(source_name ORDER BY source_name)
    INTO missing_sources
    FROM unnest(NEW.data_source_list) AS source_name
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.data_source_registry registry
        WHERE registry.data_source_name = source_name
    );

    IF missing_sources IS NOT NULL THEN
        RAISE EXCEPTION 'data_source_availability contains unknown data_source_name values: %', missing_sources;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_data_source_availability_sources
    ON public.data_source_availability;

CREATE TRIGGER trg_validate_data_source_availability_sources
BEFORE INSERT OR UPDATE OF data_source_list
ON public.data_source_availability
FOR EACH ROW
EXECUTE FUNCTION public.validate_data_source_availability_sources();

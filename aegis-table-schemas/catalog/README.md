# Catalog Schemas

V2 catalog tables should be generated from the Catalog sheet in `docs/aegis_v2/aegis_runtime_schema_field_map.xlsx`.

Expected tables:

- `data_source_registry`
- `monitored_institutions`
- `data_source_availability`

DDL:

- `001_catalog_tables.sql`

Seed:

- `scripts/seed_v2_catalog_tables.py`

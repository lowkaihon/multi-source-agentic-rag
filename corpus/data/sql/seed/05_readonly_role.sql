-- Read-only role for the RAG pipeline (defense-in-depth)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'msrag_readonly') THEN
        CREATE ROLE msrag_readonly;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE mas_compliance TO msrag_readonly;
GRANT USAGE ON SCHEMA public TO msrag_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO msrag_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO msrag_readonly;

-- Grant the read-only role to the msrag user
GRANT msrag_readonly TO msrag;

-- Enable pgvector extension on first startup.
-- Idempotent: safe to run against a database that already has the extension.
CREATE EXTENSION IF NOT EXISTS vector;

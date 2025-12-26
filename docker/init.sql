-- PostgreSQL initialization script for Docker
-- This runs when the container is first created

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Set default similarity threshold for fuzzy matching
ALTER DATABASE hindsight SET pg_trgm.similarity_threshold = 0.3;

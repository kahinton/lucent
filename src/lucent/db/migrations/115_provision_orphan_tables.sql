-- Migration 115: provision the three live tables the migration chain never
-- created (memories, requests, resource_access_grants exist on the live
-- database but in no migration file 001-114 — discovered by the fresh-DB
-- replay this RLS wave runs before migration 116 applies).
--
-- Why this matters for tenant isolation: migration 116 binds RLS on
-- memories and requests by name. A fresh replay of the chain must produce
-- the real schema, or 116 aborts on the first missing table (and worse, a
-- "fixed" 116 that dropped them from the binding list would silently leave
-- the two most sensitive tables unbound). This migration makes the chain
-- self-sufficient: fresh DBs get the real tables; the live DB gets no-ops.
--
-- Column lists are the live schemas (verified via information_schema on
-- 2026-09-14). Everything is IF NOT EXISTS / IF NOT present: on the live
-- database every statement here is a no-op. Constraints and indexes use the
-- canonical names the live tables carry so replayed and live schemas match
-- (or already match).
--
-- NOTE for reviewers: this migration does NOT try to backfill every index
-- and constraint the live tables have accumulated over their lifetime —
-- it provisions the load-bearing structure (PK, tenancy columns, the CHECK
-- constraints the RLS policies and repos rely on, and the search indexes
-- migration 010/094-095 expect). Divergence notes for replayed databases
-- are recorded in the wave session memory; fresh installs get a schema
-- functionally identical to live for every code path the repo exercises.

-- ---------------------------------------------------------------------------
-- memories (live columns, information_schema-verified 2026-09-14)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id                 UUID PRIMARY KEY,
    username           TEXT NOT NULL,
    type               TEXT NOT NULL,
    content            TEXT NOT NULL,
    tags               TEXT[] DEFAULT '{}',
    importance         INTEGER DEFAULT 5,
    related_memory_ids TEXT[] DEFAULT '{}',
    metadata           JSONB,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    deleted_at         TIMESTAMPTZ,
    user_id            UUID REFERENCES users(id) ON DELETE CASCADE,
    shared             BOOLEAN DEFAULT FALSE,
    organization_id    UUID REFERENCES organizations(id) ON DELETE CASCADE,
    last_accessed_at   TIMESTAMPTZ,
    version            INTEGER NOT NULL DEFAULT 1,
    lifecycle_stage    TEXT NOT NULL DEFAULT 'active',
    vitality_score     REAL,
    vitality_computed_at TIMESTAMPTZ,
    search_vector      TSVECTOR,
    project_id         UUID REFERENCES projects(id) ON DELETE SET NULL,
    CONSTRAINT memories_type_check
        CHECK (type = ANY (ARRAY['experience'::text, 'technical'::text,
                                 'procedural'::text, 'goal'::text,
                                 'individual'::text])),
    CONSTRAINT memories_importance_check
        CHECK (importance >= 1 AND importance <= 10),
    CONSTRAINT memories_lifecycle_stage_check
        CHECK (lifecycle_stage = ANY (ARRAY['active'::text, 'consolidating'::text,
                                            'archived'::text, 'forgotten'::text]))
);

-- Tenancy shape the RLS policies assume: every row is either user-keyed or
-- org-keyed (individual memories are user-only; shared rows carry the org).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_memories_tenancy'
    ) THEN
        ALTER TABLE memories ADD CONSTRAINT ck_memories_tenancy
            CHECK (user_id IS NOT NULL OR organization_id IS NOT NULL) NOT VALID;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_memories_user_id_active
    ON memories (user_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_org_active
    ON memories (organization_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed
    ON memories (last_accessed_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_memories_tags_active
    ON memories USING gin (tags) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memories_metadata
    ON memories USING gin (metadata);
CREATE INDEX IF NOT EXISTS idx_memories_related_memory_ids_gin
    ON memories USING gin (related_memory_ids);
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm
    ON memories USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memories_lifecycle_stage
    ON memories (lifecycle_stage);
CREATE INDEX IF NOT EXISTS idx_memories_project_id
    ON memories (project_id) WHERE project_id IS NOT NULL;

-- Full-text search infrastructure (migrations 094/095 manage the column and
-- its backfill; a fresh replay needs the column + trigger from the start).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_memories_search_vector'
          AND tgrelid = 'memories'::regclass
    ) THEN
        CREATE TRIGGER trg_memories_search_vector
            BEFORE INSERT OR UPDATE OF content ON memories
            FOR EACH ROW EXECUTE FUNCTION
            tsvector_update_trigger(search_vector, 'pg_catalog.english', content);
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- requests (live columns, information_schema-verified 2026-09-14)
-- ---------------------------------------------------------------------------
-- (No enum types: live requests stores VARCHAR + CHECK constraints —
-- the app normalizes status values in db/requests.py. The enum types a
-- previous scratch session created were an artifact of an earlier draft
-- of this file and are not part of the live schema.)

CREATE TABLE IF NOT EXISTS requests (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title              VARCHAR(500) NOT NULL,
    description        TEXT,
    source             VARCHAR(20) NOT NULL DEFAULT 'user',
    status             VARCHAR(30) NOT NULL DEFAULT 'pending',
    priority           VARCHAR(10) NOT NULL DEFAULT 'medium',
    created_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    organization_id    UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    completed_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dependency_policy  VARCHAR(20) NOT NULL DEFAULT 'strict',
    review_count       INTEGER NOT NULL DEFAULT 0,
    max_reviews        INTEGER NOT NULL DEFAULT 1,
    review_feedback    TEXT,
    reviewed_at        TIMESTAMPTZ,
    approval_status    VARCHAR(20) NOT NULL DEFAULT 'not_required',
    approved_by        UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at        TIMESTAMPTZ,
    approval_comment   TEXT,
    fingerprint        VARCHAR(64),
    target_repo        TEXT,
    target_paths       TEXT[],
    goal_memory_id     UUID,
    goal_milestone_index INTEGER
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_requests_source') THEN
        ALTER TABLE requests ADD CONSTRAINT chk_requests_source
            CHECK (source = ANY (ARRAY['user'::varchar, 'cognitive'::varchar,
                                       'api'::varchar, 'daemon'::varchar,
                                       'schedule'::varchar]));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_requests_dependency_policy') THEN
        ALTER TABLE requests ADD CONSTRAINT chk_requests_dependency_policy
            CHECK (dependency_policy = ANY (ARRAY['strict'::varchar, 'permissive'::varchar]));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_requests_status') THEN
        ALTER TABLE requests ADD CONSTRAINT chk_requests_status
            CHECK (status = ANY (ARRAY['pending'::varchar, 'planned'::varchar,
                                       'in_progress'::varchar, 'review'::varchar,
                                       'needs_rework'::varchar, 'completed'::varchar,
                                       'failed'::varchar, 'cancelled'::varchar,
                                       'rejection_processing'::varchar]));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_requests_org_status
    ON requests (organization_id, status);
CREATE INDEX IF NOT EXISTS idx_requests_org_created
    ON requests (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_requests_goal_memory
    ON requests (goal_memory_id) WHERE goal_memory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_requests_fingerprint
    ON requests (organization_id, fingerprint) WHERE fingerprint IS NOT NULL;

-- ---------------------------------------------------------------------------
-- resource_access_grants (live columns, information_schema-verified
-- 2026-09-14; 105 live rows — org-keyed share grants over definition-type
-- resources, written by the definition tools, read by access_control.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS resource_access_grants (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    resource_type    VARCHAR(50) NOT NULL,
    resource_id      TEXT NOT NULL,
    principal_type   VARCHAR(20) NOT NULL,
    principal_id     UUID,
    granted_by       UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ownership assertion for the pre-existing case: on the live database the
-- three tables arrive owned by lucent (114's REASSIGN runs before this file
-- and hands them to lucent_app in the same transaction — but belt-and-
-- suspenders, re-assert per table so post-114 state is always correct even
-- if the wave is ever partially applied).
DO $$
DECLARE t text;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename IN ('memories', 'requests', 'resource_access_grants')
          AND tableowner <> 'lucent_app'
    LOOP
        EXECUTE format('ALTER TABLE %I OWNER TO lucent_app', t);
    END LOOP;
END
$$;

-- Index creation requires table ownership; guard per-index so the live
-- database (where the tables and their indexes pre-exist) applies cleanly
-- under the post-114 owner while fresh replays still get the indexes.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = 'public'
                     AND indexname = 'idx_resource_grants_lookup') THEN
        CREATE INDEX idx_resource_grants_lookup
            ON resource_access_grants (organization_id, resource_type, resource_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = 'public'
                     AND indexname = 'idx_resource_grants_principal') THEN
        CREATE INDEX idx_resource_grants_principal
            ON resource_access_grants (principal_type, principal_id);
    END IF;
END
$$;
-- Migration 117: make the tenant wave's grant coverage for lucent_app
-- explicit on every relation migrations 114/115 transferred or created.
--
-- THE GAP (from the wave's static grant/transfer diff): 114 grants
-- batch-style (`ON ALL TABLES/SEQUENCES IN SCHEMA public`) BEFORE its
-- ownership transfer loop, and 115 creates its three tables after both
-- with no grant statements. The wave's grant coverage therefore rests on
-- engine-specific behavior rather than explicit per-relation statements:
--   * on PostgreSQL 17 (pgembed scratch, live-verified 2026-09-17) the
--     grant entry SURVIVES ALTER ... OWNER TO (grantor rewritten to the
--     new owner) — but a table CREATED by lucent_app gets acl=NULL when
--     only the self-grant ADP clause applies, materializing the entry
--     only because 114's third-party ADP clauses (backup/diagnostics)
--     coexist;
--   * the 9/15 wedged-state prod traces carried InsufficientPrivilege
--     (SQLSTATE 42501 — covers both missing grants and RLS violations)
--     on api_keys + schedules.
-- The pre-deploy gate's ACL matrix verifies explicit entries, not implicit
-- owner privileges. This migration makes coverage explicit and
-- engine-behavior-independent for exactly the transferred set.
--
-- Consequences this migration closes:
--   * ACL-completeness: every transferred relation carries explicit
--     grant entries to lucent_app (the pre-deploy gate's
--     has_table_privilege / ACL matrix verifies entries, not just
--     implicit owner privileges), independent of engine ADP/transfer
--     elision behavior;
--   * the born-owned tables (115's three, created by the post-cutover
--     runner) get explicit entries instead of relying on the ADP
--     self-grant row that PostgreSQL may elide when no third-party
--     entry forces an ACL array;
--   * the down/up cycle (114.down REASSIGN + re-apply) can no longer
--     leave coverage dependent on grantor-rewriting behavior.
--
-- Scope guard: grants ONLY relations lucent_app already owns — the exact
-- set the wave transferred. Replay-state behavior:
--   * live database before cutover: no lucent_app-owned relations -> no-op;
--   * live database after the wave + cutover: grants every transferred
--     relation;
--   * fresh replay: grants the full 001-116 chain schema.
-- No other roles, no other statements: the grant gap is the whole
-- migration (no speculative edits).
--
-- Functions transferred by 114 need no grant here: EXECUTE on
-- public-schema functions is held by PUBLIC by default (no repo migration
-- revokes it), and the transferred routines are trigger functions used by
-- their own tables.

DO $$
DECLARE
    obj record;
BEGIN
    -- Tables (incl. partitioned roots), views, matviews owned by
    -- lucent_app: full DML, mirroring 114's per-relation transfer kinds.
    FOR obj IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm')
          AND pg_get_userbyid(c.relowner) = 'lucent_app'
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO lucent_app',
                       obj.relname);
    END LOOP;

    -- Sequences owned by lucent_app.
    FOR obj IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'S'
          AND pg_get_userbyid(c.relowner) = 'lucent_app'
    LOOP
        EXECUTE format('GRANT USAGE, SELECT ON %I TO lucent_app',
                       obj.relname);
    END LOOP;
END
$$;
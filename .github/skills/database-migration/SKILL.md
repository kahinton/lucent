---
name: database-migration
description: 'Create, validate, and apply database schema migrations safely. Use when adding, altering, or removing database tables/columns, or when applying schema changes across environments.'
---

# Database Migration

## Before Starting

Check for past migration decisions and existing patterns:
```
search_memories(query="database migration schema", tags=["database"], limit=10)
```

## Migration Procedure

### 1. Understand the Change

Before writing SQL:
- What data model change is needed and why?
- What existing data will be affected?
- Is this additive (new column/table) or destructive (drop/rename)?
- Can this be rolled back if something goes wrong?

### 2. Check the Migration Sequence

```bash
# List existing migrations to determine the next number
ls db/migrations/ 2>/dev/null || find . -name "*.sql" -path "*/migration*" | sort
```

Follow the project's naming convention. Typical format: `NNN_description.sql` with zero-padded sequence numbers.

### 3. Write the Migration

```sql
-- Migration NNN: <What this does and why>
-- Previous state: <What existed before>
-- New state: <What this creates/changes>

-- Example: Adding a column
ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <column> <type> NOT NULL DEFAULT <value>;

-- Example: Creating an index
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_<table>_<column> ON <table>(<column>);

-- Example: Creating a table
CREATE TABLE IF NOT EXISTS <table> (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- ... columns ...
);
```

**Rules:**
- Use `IF NOT EXISTS` / `IF EXISTS` for idempotency — migrations should be safe to run twice
- Add `NOT NULL` constraints with a `DEFAULT` to avoid breaking existing rows
- Create indexes `CONCURRENTLY` on large tables to avoid locking
- Never drop a column or table without explicit approval
- Comment the migration with what it does and why

### 4. Validate

```bash
# Connect to the database (use actual container/service name from docker compose ps)
docker exec -it <postgres-container> psql -U <db_user> -d <db_name>

# Test the migration syntax
\i /path/to/migration.sql

# Verify the result
\d <affected_table>
```

Check:
- SQL syntax is valid
- Referenced tables and columns exist
- Existing data won't be corrupted
- The migration is idempotent (run it twice — second run should be a no-op)

### 5. Apply

Most projects apply migrations automatically on server startup. After restart:

```bash
# Check logs for migration success/failure
docker logs <server-container> 2>&1 | grep -i "migration"

# Verify the schema change
docker exec -it <postgres-container> psql -U <db_user> -d <db_name> -c "\d <table>"
```

### 6. Record the Decision

```
create_memory(
  type="technical",
  content="## Migration NNN: <title>\n\n**Why**: <business/technical reason>\n**Change**: <what was added/modified/removed>\n**Rollback**: <how to reverse if needed>\n**Related**: <what code depends on this schema change>",
  tags=["database", "migration"],
  importance=7,
  shared=true
)
```

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Deploying without a verified backup** | Even with `IF EXISTS` guards, a bad migration can corrupt or delete data irreversibly — without a backup, recovery is impossible. | Run `pg_dump` (or equivalent) immediately before applying any migration. Verify the backup is restorable, not just that the dump completed. |
| **Mixing DDL and DML in the same migration** | Schema changes and data backfills in one migration make rollback nearly impossible — if the backfill fails mid-way, the schema is changed but data is inconsistent. | Split into two sequential migrations: one for the schema change, one for the data backfill. Each can be rolled back independently. |
| **Skipping rollback testing** | An untested rollback plan is no rollback plan — discovering your reverse SQL doesn't work while production is broken turns a minor incident into a major one. | Write and test the rollback SQL in staging before applying the forward migration to production. Include it as a comment in the migration file. |
| **Applying directly to production without staging** | "It worked locally" is the most common preamble to a production incident — local databases have different data volumes, constraints, and concurrent access patterns. | Always apply migrations to a staging environment with production-like data first. Verify it completes, performs acceptably, and the application still works. |
| **Adding NOT NULL columns without defaults** | Adding a `NOT NULL` column to an existing table without a `DEFAULT` value fails immediately on tables with existing rows, breaking the migration in production where data exists. | Always include a `DEFAULT` clause when adding `NOT NULL` columns, or add as nullable first, backfill, then add the constraint. |

## Destructive Changes

Dropping columns, tables, or changing types requires extra caution:

1. **Verify no code references the old schema** — search the codebase
2. **Consider a two-phase approach:** phase 1 stops using the column, phase 2 drops it (in a later migration)
3. **Back up** before applying: `pg_dump -Fc > backup_before_migration_NNN.dump`
4. **Get explicit approval** before applying destructive changes

## Anti-Patterns

| Anti-Pattern | Why It Fails | What To Do Instead |
|---|---|---|
| **Not testing the rollback path** | A migration that can't be reversed safely turns a recoverable mistake into a production incident. You discover the rollback is broken during an emergency. | Write and test the `downgrade()` function. Run `upgrade` then `downgrade` then `upgrade` again in a test environment before applying. |
| **Mixing DDL and DML in the same migration** | Schema changes (DDL) and data changes (DML) have different failure modes and rollback characteristics. A failed data transform mid-migration leaves the schema in an inconsistent state. | Separate structural changes from data migrations. Apply DDL first, verify, then apply DML in a subsequent migration. |
| **Deploying without a pre-migration backup** | If the migration corrupts data or the rollback fails, there is no recovery point. "It worked in staging" is not a backup strategy. | Always `pg_dump` before applying. Verify the backup is restorable. Keep it until the migration is confirmed stable in production. |
| **Applying migrations directly to production without staging verification** | Schema differences between environments mean a migration that passes locally can fail on production data volumes, constraints, or concurrent access patterns. | Apply to a staging environment with production-like data first. Check for lock contention, execution time, and constraint violations. |
| **Adding NOT NULL columns without defaults to populated tables** | The migration fails immediately on any table with existing rows because the database can't fill the column retroactively. | Add the column as nullable first, backfill data, then add the NOT NULL constraint in a separate migration. |
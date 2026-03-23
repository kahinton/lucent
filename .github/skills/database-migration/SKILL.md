---
name: database-migration
description: 'Create, validate, and apply database schema migrations safely.'
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

## Destructive Changes

Dropping columns, tables, or changing types requires extra caution:

1. **Verify no code references the old schema** — search the codebase
2. **Consider a two-phase approach:** phase 1 stops using the column, phase 2 drops it (in a later migration)
3. **Back up** before applying: `pg_dump -Fc > backup_before_migration_NNN.dump`
4. **Get explicit approval** before applying destructive changes
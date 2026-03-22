---
name: database-migration
description: 'Create, validate, and apply PostgreSQL schema migrations following the existing raw-SQL migration pattern'
---

# Database Migration

Create, validate, and apply PostgreSQL schema migrations using the project's raw-SQL pattern.

## When to Use

- Adding or modifying database tables or columns
- Creating indexes or constraints
- Changing data types or adding defaults
- Any schema change that needs to be versioned

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Find past migration decisions | `query="database migration schema [table]"`, `tags=["database"]` |
| `memory-server-create_memory` | Record migration decisions and schema changes | `type="technical"`, `tags=["database", "migration"]`, `importance=7` |

## Procedure: Creating a Migration

### Step 1: Load Context

```
memory-server-search_memories(query="database migration schema", tags=["database"], limit=5)
```

Check for: past decisions about this table, schema constraints, known gotchas.

### Step 2: Determine the Next Number

```bash
ls db/migrations/   # or wherever the project keeps migration files
```

Find the highest number and increment by 1. Use zero-padded three-digit format (e.g., `013`).

### Step 3: Write the Migration

Create `db/migrations/NNN_description.sql`:

```sql
-- Migration NNN: [What this does and why]
-- Previous state: [What existed before]
-- New state: [What this creates/changes]

-- Add new column with safe default
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- Create index
CREATE INDEX IF NOT EXISTS idx_memories_version ON memories(version);
```

Rules:
- Use `IF NOT EXISTS` / `IF EXISTS` for idempotency
- Include both the schema change and any required data migration
- Add `NOT NULL` constraints with defaults to avoid breaking existing rows
- Create indexes `CONCURRENTLY` for large tables when possible
- Add a comment at top describing what and why

### Step 4: Validate

```bash
# Check SQL syntax (use the actual postgres container name from docker compose ps)
docker exec <postgres-container> psql -U <db_user> -d <db_name> -c "\i /path/to/migration.sql" --dry-run

# Test against local DB
docker compose up -d postgres
docker exec <postgres-container> psql -U <db_user> -d <db_name> -f /path/to/migration.sql
```

Verify:
- SQL syntax is valid PostgreSQL
- Referenced tables and columns exist (from prior migrations)
- No destructive changes without explicit approval
- Existing rows won't be broken by NOT NULL without DEFAULT

### Step 5: Apply and Verify

Migrations are applied automatically on server startup. After restart:

```bash
# Check logs for migration success/failure
docker logs lucent-server 2>&1 | grep -i "migration"

# Verify schema in psql
docker exec -it <postgres-container> psql -U <db_user> -d <db_name> -c "\d memories"
```

### Step 6: Save Decision

```
memory-server-create_memory(
  type="technical",
  content="## Migration NNN: [title]\n\n**Why**: [business/technical reason]\n**Change**: [what was added/modified]\n**Rollback**: [how to reverse if needed]",
  tags=["database", "migration"],
  importance=7,
  metadata={"category": "database", "references": ["db/migrations/NNN_description.sql"]},
  shared=true
)
```

## Decision: Migration Type

- IF adding a new column to existing table → use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ... DEFAULT ...`
- ELIF dropping a column → confirm no code references it, use `ALTER TABLE ... DROP COLUMN IF EXISTS`
- ELIF creating a new table → use `CREATE TABLE IF NOT EXISTS`
- ELIF creating an index on large table → use `CREATE INDEX CONCURRENTLY IF NOT EXISTS`
- ELIF data migration needed → include in same file, use transaction if possible

## Best Practices

- **Never modify an existing migration file** — create a new one
- Prefer additive changes (add columns) over destructive ones (drop columns)
- Document why the migration is needed, not just what it does
- Test against local database before pushing

## Example: Good Migration File

```sql
-- Migration 013: Add version column to memories for optimistic locking
-- Why: Needed for update_memory's expected_version parameter to detect concurrent modifications
-- Related: the MCP tool that uses this column (e.g., update_memory's expected_version parameter)

ALTER TABLE memories ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- Update existing rows to have version = 1 (already handled by DEFAULT)

-- Create index for version lookups in optimistic lock queries
CREATE INDEX IF NOT EXISTS idx_memories_version ON memories(id, version);
```

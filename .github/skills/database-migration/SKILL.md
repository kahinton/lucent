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

## Migration Pattern

Migrations live in `src/lucent/db/migrations/` as numbered SQL files:
```
001_init.sql
002_add_users.sql
...
012_add_memory_versioning.sql
```

### Step 1: Determine the Next Number

1. List existing migrations: `ls src/lucent/db/migrations/`
2. Find the highest number and increment by 1
3. Use zero-padded three-digit format (e.g., `013`)

### Step 2: Write the Migration

1. Create `src/lucent/db/migrations/NNN_description.sql`
2. Use descriptive snake_case names (e.g., `013_add_tag_index.sql`)
3. Write idempotent SQL where possible (`IF NOT EXISTS`, `IF EXISTS`)
4. Include both the schema change and any required data migration
5. Add a comment at the top describing what this migration does

### Step 3: Validate

1. Check SQL syntax is valid PostgreSQL
2. Verify referenced tables and columns exist (from prior migrations)
3. Ensure no destructive changes without explicit approval
4. Test against a local database if available: `docker compose up -d`

### Step 4: Verify Application

1. Migrations are applied automatically on server startup
2. Check logs for migration success/failure after restart
3. Verify the schema change with `\d tablename` in psql

## Best Practices

- Never modify an existing migration file — create a new one
- Prefer additive changes (add columns) over destructive ones (drop columns)
- Add `NOT NULL` constraints with defaults to avoid breaking existing rows
- Create indexes concurrently for large tables when possible
- Document why the migration is needed, not just what it does

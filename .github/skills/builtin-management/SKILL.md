---
name: builtin-management
description: 'Procedure for updating built-in agent definitions, skill definitions, and system schedules. Use when you need to modify a built-in object — the daemon is blocked from these changes, so conversation-mode Lucent (running as owner) must edit the on-disk source files directly.'
---

# Built-in Management

How to update built-in objects correctly. Built-in definitions have their source of truth on disk — DB-only updates are ephemeral and get overwritten on server restart.

## What Are Built-in Objects?

Built-in objects are agent definitions, skill definitions, and system schedules that are loaded into the database from on-disk files at server startup. They differ from user-created (instance-scoped) objects in two ways:

1. **Source of truth is on disk.** The database copy is a cache that gets overwritten every restart.
2. **Daemon cannot modify them.** The repository layer rejects updates from `role=daemon` with:
   - Definitions: `"Built-in definitions cannot be modified by the daemon. Update the on-disk source file instead."`
   - Schedules: `"Built-in system schedules cannot be modified by the daemon. Update the on-disk source file instead."`

**How they're identified in the database:**

| Object Type | Column | Built-in Value |
|---|---|---|
| Agent definitions | `scope` | `'built-in'` |
| Skill definitions | `scope` | `'built-in'` |
| MCP server definitions | `scope` | `'built-in'` |
| Schedules | `is_system` | `true` |

Instance-scoped objects (created via API or MCP tools) are not affected by any of this — the daemon can modify those normally.

## Why DB-Only Updates Don't Persist

On every server startup, `_sync_built_in_definitions()` in `api/app.py` runs:

1. Scans `.github/skills/` for `SKILL.md` files → upserts into `skill_definitions` with `scope='built-in'`
2. Scans `.github/agents/definitions/` for `AGENT.md` files → upserts into `agent_definitions` with `scope='built-in'`
3. Synchronizes agent-skill junction mappings to match the `skill_names` in each agent's YAML frontmatter

The upsert uses `ON CONFLICT (name, organization_id) DO UPDATE ... WHERE scope = 'built-in'`, which means every restart overwrites the DB content with whatever is on disk. Any DB-only change — made via API, MCP tool, or direct SQL — is silently replaced.

System schedules are seeded via `ensure_system_schedule()` with similar overwrite behavior.

## Where Built-in Definitions Live on Disk

### Agent Definitions

```
.github/agents/definitions/<name>/AGENT.md
```

Each agent directory contains a single `AGENT.md` with YAML frontmatter (`name`, `description`, `skill_names`) followed by the full agent prompt in markdown.

Example path: `.github/agents/definitions/code/AGENT.md`

### Skill Definitions

```
.github/skills/<name>/SKILL.md
```

Each skill directory contains a single `SKILL.md` with YAML frontmatter (`name`, `description`) followed by the skill content in markdown.

Example path: `.github/skills/dev-workflow/SKILL.md`

### System Schedules

System schedules are **not** loaded from markdown files. They are seeded by explicit calls to `ScheduleRepository.ensure_system_schedule()` in the application startup code. To modify a system schedule, find the seeding call in the source code (search for `ensure_system_schedule` in `src/lucent/`).

## Procedure: Updating a Built-in Definition

### Step 1: Identify the Source File

Determine which type of object you're updating and locate the file:

```bash
# Agent definition
cat .github/agents/definitions/<name>/AGENT.md

# Skill definition
cat .github/skills/<name>/SKILL.md

# System schedule — find the seeding code
grep -rn "ensure_system_schedule" src/lucent/
```

If you're unsure of the name, list what exists:

```bash
ls .github/agents/definitions/
ls .github/skills/
```

### Step 2: Edit the Source File

Edit the on-disk file directly. For agent and skill definitions:

- **YAML frontmatter** (`name`, `description`, and `skill_names` for agents) controls the metadata synced to the database.
- **Markdown body** (everything after the closing `---`) becomes the `content` column in the database.

For agents, the `skill_names` list in frontmatter controls which skills are granted. On sync, stale mappings are removed and new ones are added — you don't need to manually grant/revoke skills.

### Step 3: Restart the Server

The sync only runs at startup. After editing the file, restart the server:

```bash
# Docker environment
docker compose restart hindsight

# Local development
# Stop and restart the server process
```

### Step 4: Verify the Change

Confirm the database reflects your edit:

```python
# For an agent definition
get_agent_definition(agent_id="<id>")
# Or search by listing
list_agent_definitions(status="active")

# For a skill definition
get_skill_definition(skill_id="<id>")
# Or search by listing
list_skill_definitions(status="active")

# For a schedule
list_schedules(status="active")
```

Check that:
- [ ] The `content` field matches what you wrote in the file
- [ ] The `description` matches the YAML frontmatter
- [ ] For agents: `skill_names` lists the correct skills
- [ ] The `updated_at` timestamp is recent (reflects the restart)

If the change didn't take effect:
1. Check that the file is in the correct directory and named exactly `AGENT.md` or `SKILL.md`
2. Check that the YAML frontmatter is valid (proper `---` delimiters, correct indentation)
3. Check server logs for sync errors during startup

## Creating a New Built-in Definition

To add a new built-in agent or skill:

1. Create the directory: `mkdir .github/agents/definitions/<name>` or `mkdir .github/skills/<name>`
2. Create the file (`AGENT.md` or `SKILL.md`) with valid YAML frontmatter
3. Restart the server — the sync will insert it with `scope='built-in'` and `status='active'`

New built-in definitions are automatically set to `active` status — they skip the proposal/approval workflow.

## Common Mistakes

| Mistake | Why It Fails | What To Do Instead |
|---|---|---|
| Updating via MCP tool or API as daemon | Blocked by the repository guard — returns 403 | Edit the on-disk source file and restart |
| Updating via MCP tool or API as owner | Succeeds but gets overwritten on next restart | Edit the on-disk source file and restart |
| Editing the file but not restarting | Sync only runs at startup — DB still has old content | Restart the server after every file edit |
| Granting skills to a built-in agent via API | Junction table gets resynchronized on restart to match `skill_names` in frontmatter | Add the skill name to the `skill_names` list in the agent's YAML frontmatter |
| Invalid YAML frontmatter | File is silently skipped during sync | Validate frontmatter — ensure `---` delimiters, proper quoting, and correct list syntax |

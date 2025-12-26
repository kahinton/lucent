# Copilot Instructions for Hindsight Memory System

This repository contains the Hindsight MCP server, a persistent memory system for LLMs. When working in this repository or any repository where the Hindsight memory server is available, follow these guidelines.

## Using the Memory System

You have access to a persistent memory system that allows you to store and retrieve information across conversations. **Use this system proactively** to enhance your assistance.

### Available Tools

#### CRUD Operations
| Tool | Purpose |
|------|---------|
| `create_memory` | Create a new memory with type, content, tags, importance, and metadata |
| `get_memory` | Retrieve a full memory by its UUID |
| `update_memory` | Update an existing memory's content, tags, importance, or metadata |
| `delete_memory` | Soft delete a memory (can be recovered) |

#### Search Operations
| Tool | Purpose | When to Use |
|------|---------|-------------|
| `search_memories` | Fuzzy search on **CONTENT field only** | When you know info is in the main content; faster, focused results |
| `search_memories_full` | Fuzzy search across **ALL fields** (content, tags, metadata) | When unsure where info is stored; broader discovery |

#### Tag Management
| Tool | Purpose |
|------|---------|
| `get_existing_tags` | List all tags with usage counts - **use before creating memories!** |
| `get_tag_suggestions` | Fuzzy search for similar existing tags |

### Memory Types

| Type | Use For | Key Metadata |
|------|---------|--------------|
| `experience` | Interactions, events, outcomes, decisions | context, outcome, lessons_learned, related_entities |
| `technical` | Code patterns, solutions, technical knowledge | category, language, code_snippet, repo, filename |
| `procedural` | Step-by-step processes, workflows | steps[], prerequisites, estimated_time, common_pitfalls |
| `goal` | Long-term objectives, progress tracking | status, deadline, milestones[], blockers[], priority |
| `individual` | Information about people | name, relationship, organization, preferences |

### Importance Scale (1-10)

| Rating | Level | Use For |
|--------|-------|---------|
| 1-3 | Routine | Minor details, temporary context |
| 4-6 | Useful | Standard practices, general knowledge |
| 7-8 | Important | Key decisions, significant learnings |
| 9-10 | Critical | Essential knowledge, major breakthroughs |

## Tag Consistency (CRITICAL)

**Before creating ANY memory, always check existing tags:**

1. Call `get_existing_tags()` to see all tags currently in use with their counts
2. Call `get_tag_suggestions("your-tag-idea")` to find similar existing tags
3. **Reuse existing tags** instead of creating variations

### Tag Naming Conventions

- Use **lowercase** (tags are auto-normalized anyway)
- Use **hyphens** for multi-word tags: `bug-fix`, `code-review`, `api-design`
- Be **specific but not overly granular**: `python` not `python3.12`
- Use **prefixes** for organization: `lang-python`, `project-hindsight`, `team-backend`

### ❌ Don't Do This
```
Creating memory with tags: ["Python", "py", "python3"]
```

### ✅ Do This Instead
```
1. get_existing_tags() → shows "python" exists with count 5
2. Create memory with tags: ["python"]
```

## Search Strategy

### Use `search_memories` (content-only) when:
- Looking for specific information you know is in the main content
- You want faster, more focused results
- Searching for phrases or concepts described in memory content

### Use `search_memories_full` (all fields) when:
- You're not sure where the information might be stored
- Searching for tag names or metadata values
- Doing broad discovery across all memory data

### Both searches support:
- Fuzzy matching (typos and partial matches work)
- Filtering by username, type, importance range, date range
- Pagination with offset/limit
- Results sorted by similarity score

## Workflow Patterns

### Starting a New Session
```
1. search_memories(type="technical") for repo-related context
2. search_memories(type="goal") to check active objectives  
3. search_memories_full("project-name") for any mentions
```

### Before Creating a Memory
```
1. get_existing_tags() to see available tags
2. get_tag_suggestions("your-tag") if unsure about naming
3. Reuse existing tags in your new memory
```

### After Solving a Problem
```
1. Check existing tags with get_existing_tags()
2. create_memory(type="technical", ...) with the solution
3. Include repo, filename, and code_snippet in metadata
4. Add related_memory_ids to link to related memories
```

### When Search Results Are Truncated
```
1. Note the memory ID from truncated search result
2. get_memory(memory_id) to fetch full content
```

## Best Practices

### DO:
- ✅ Search for existing context before starting work
- ✅ Check existing tags before creating memories
- ✅ Use specific, reusable tags
- ✅ Link related memories with `related_memory_ids`
- ✅ Include `repo` and `filename` in technical memory metadata
- ✅ Update goal memories as progress is made
- ✅ Use `get_memory` to fetch full content when truncated

### DON'T:
- ❌ Create new tag variations when similar tags exist
- ❌ Store trivial or temporary information (importance 1-3) unless needed
- ❌ Forget to search before creating (avoid duplicates)
- ❌ Leave goal memories without status updates
- ❌ Create memories without checking tag consistency first

## Repository-Specific Notes

This is the Hindsight repository itself. When working on this codebase:

- **Language**: Python 3.12+
- **Key dependencies**: mcp (FastMCP), asyncpg, pydantic
- **Database**: PostgreSQL with pg_trgm extension for fuzzy search
- **Transport**: SSE (Server-Sent Events) on port 8765

### Project Structure
```
src/hindsight/
├── server.py          # MCP server entry point
├── db/
│   ├── client.py      # asyncpg connection pool & MemoryRepository
│   └── migrations/    # SQL migration files
├── models/
│   └── memory.py      # Pydantic models for all memory types
├── tools/
│   └── memories.py    # MCP tool implementations
└── prompts/
    └── memory_usage.py # System prompt templates
```

### Running Locally
```bash
docker compose up -d postgres
source .venv/bin/activate
export DATABASE_URL="postgresql://hindsight:hindsight_dev_password@localhost:5433/hindsight"
hindsight
```

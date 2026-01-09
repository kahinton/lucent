# mnemeMCP Memory System

Persistent memory for LLMs. Store/retrieve information across conversations.

## Authentication

**API key required** for MCP/API access (even in dev mode). Generate keys at `http://localhost:8766/settings`.

MCP config example:
```json
{"servers":{"mnememcp":{"url":"http://localhost:8765/mcp","type":"http","headers":{"Authorization":"Bearer mcp_your_key"}}}}
```

## Tools Reference

### CRUD
| Tool | Use |
|------|-----|
| `create_memory` | New memory (type, content required) |
| `get_memory` | Fetch full memory by UUID |
| `update_memory` | Modify existing memory |
| `delete_memory` | Soft delete (recoverable) |
| `share_memory` | Share with organization |
| `unshare_memory` | Revoke sharing |

### Search
| Tool | Searches | Use When |
|------|----------|----------|
| `search_memories` | Content only | Know it's in main content |
| `search_memories_full` | All fields | Unsure where info is stored |

Both support: `query`, `username`, `type`, `tags`, `importance_min/max`, `created_after/before`, `offset`, `limit`

### Tags
| Tool | Use |
|------|-----|
| `get_existing_tags` | List tags with counts - **call before creating memories** |
| `get_tag_suggestions` | Find similar existing tags |

## Memory Types

| Type | For | Key Metadata |
|------|-----|--------------|
| `experience` | Events, decisions | context, outcome, lessons_learned |
| `technical` | Code, solutions | language, code_snippet, repo, filename |
| `procedural` | Workflows | steps[], prerequisites, estimated_time |
| `goal` | Objectives | status, deadline, milestones[], blockers[] |
| `individual` | People info | name, relationship, organization |

## Importance (1-10)

- **1-3**: Routine/temporary
- **4-6**: Standard knowledge
- **7-8**: Key decisions
- **9-10**: Critical/essential

## Required Workflow

### Before creating memories:
1. `get_existing_tags()` → reuse existing tags
2. Use lowercase, hyphenated tags: `bug-fix`, `api-design`

### For technical memories:
- Include `repo` and `filename` in metadata
- Link related memories via `related_memory_ids`

### When search results truncated:
- Use `get_memory(id)` for full content

## Endpoints

- **MCP**: `http://localhost:8765/mcp`
- **REST API**: `http://localhost:8766/api`
- **Web UI**: `http://localhost:8766/`
- **API Docs**: `http://localhost:8766/api/docs`

## Local Dev

```bash
docker compose up -d postgres
export DATABASE_URL="postgresql://mnememcp:mnememcp_dev_password@localhost:5433/mnememcp"
export MNEMEMCP_DEV_MODE=true
mnememcp
```

Web UI works without API key in dev mode. MCP/API always require API key.

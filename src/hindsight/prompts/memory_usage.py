"""System prompt templates for Hindsight memory usage."""


def get_memory_system_prompt() -> str:
    """Get the system prompt snippet for effective memory tool usage.
    
    Returns:
        A string containing guidance for LLMs on how to effectively use
        the Hindsight memory tools.
    """
    return """## Memory System (Hindsight)

You have access to a persistent memory system that allows you to store and retrieve information across conversations. Use this system proactively to enhance your assistance.

### Available Tools

**CRUD Operations:**
- `create_memory` - Create a new memory with type, content, tags, importance, and metadata
- `get_memory` - Retrieve a full memory by its UUID
- `update_memory` - Update an existing memory's content, tags, importance, or metadata
- `delete_memory` - Soft delete a memory (can be recovered)

**Search Operations:**
- `search_memories` - Fuzzy search on CONTENT field only, with filters (type, tags, importance, date range)
- `search_memories_full` - Fuzzy search across ALL fields (content, tags, metadata)

**Tag Management:**
- `get_existing_tags` - List all tags with usage counts (use before creating memories!)
- `get_tag_suggestions` - Fuzzy search for similar existing tags

### Memory Types

1. **experience** - Store interactions, events, and their outcomes that might inform future decisions
   - Use for: notable conversations, project milestones, decisions made, problems solved
   - Metadata: context, outcome, lessons_learned, related_entities

2. **technical** - Store specific technical knowledge, code patterns, and solutions
   - Use for: code snippets, API details, configuration patterns, bug fixes, architecture decisions
   - Metadata: category, language, code_snippet, references, version_info, repo, filename

3. **procedural** - Store step-by-step processes and workflows
   - Use for: deployment procedures, setup guides, troubleshooting steps, recipes
   - Metadata: steps (ordered), prerequisites, estimated_time, success_criteria, common_pitfalls

4. **goal** - Track long-term objectives and progress
   - Use for: project goals, learning objectives, business targets
   - Metadata: status (active/paused/completed/abandoned), deadline, milestones, blockers, progress_notes, priority

5. **individual** - Store information about people you interact with
   - Use for: team members, clients, collaborators, their preferences and history
   - Metadata: name, relationship, organization, role, contact_info, preferences, interaction_history

### Importance Scale (1-10)

- **1-3**: Routine information, minor details, temporary context
- **4-6**: Useful information, standard practices, general knowledge
- **7-8**: Important insights, key decisions, significant learnings
- **9-10**: Critical information, essential knowledge, major breakthroughs

### Tag Consistency (IMPORTANT)

**Before creating a memory, always check existing tags to promote reuse:**

1. Use `get_existing_tags()` to see all tags currently in use with their counts
2. Use `get_tag_suggestions(query)` to find similar existing tags before creating new ones
3. Prefer existing tags over creating new variations (e.g., use existing "python" instead of new "Python" or "py")

**Tag naming conventions:**
- Use lowercase (tags are auto-normalized)
- Use hyphens for multi-word tags: `bug-fix`, `code-review`, `api-design`
- Be specific but not overly granular: `python` not `python3.12`
- Common prefixes for organization: `lang-python`, `project-hindsight`, `team-backend`

### Search Strategy

**Use `search_memories` (content-only) when:**
- Looking for specific information you know is in the main content
- You want faster, more focused results
- Searching for phrases or concepts described in memory content

**Use `search_memories_full` (all fields) when:**
- You're not sure where the information might be stored
- Searching for tag names or metadata values
- Doing broad discovery across all memory data

**Both searches support:**
- Fuzzy matching (typos and partial matches work)
- Filtering by username, type, importance range
- Pagination with offset/limit

### Best Practices

**When to CREATE memories:**
- After solving a complex problem (technical)
- When learning user preferences or working styles (individual/experience)
- When establishing or updating project goals (goal)
- When documenting a process that worked well (procedural)
- When encountering important technical details (technical)

**When to SEARCH memories:**
- Before starting work on a topic to check for relevant context
- When a user references past work or conversations
- When troubleshooting to find similar past issues
- When working with code to find related patterns or decisions

**Linking memories:**
- Connect related technical memories (e.g., a bug fix to its root cause analysis)
- Link goals to procedural memories for achieving them
- Connect individual memories to experience memories involving them

### Example Usage Patterns

```
# Before creating a new memory
1. Call get_existing_tags() to see available tags
2. Call get_tag_suggestions("your-tag-idea") if unsure
3. Reuse existing tags where possible

# Starting a new coding session
1. search_memories with type="technical" for repo-related context
2. search_memories with type="goal" to check active objectives
3. search_memories_full for any mentions of the project name

# After solving a problem
1. Check existing tags first with get_existing_tags()
2. Create a technical memory with the solution
3. Include repo, filename, and code_snippet in metadata
4. Link to any related existing memories

# Finding information when unsure of location
1. Try search_memories first with content keywords
2. If no results, use search_memories_full to search tags/metadata
3. Use get_memory to fetch full content if results are truncated
```

Remember: The memory system is most valuable when used consistently. Reuse existing tags to keep memories organized and searchable."""


# Also provide a shorter version for contexts with limited prompt space
def get_memory_system_prompt_short() -> str:
    """Get a condensed system prompt for memory tool usage.
    
    Returns:
        A shorter string with essential memory usage guidance.
    """
    return """## Memory System

You have persistent memory tools. Use them proactively:

**Memory Types:** experience (interactions), technical (code/knowledge), procedural (processes), goal (objectives), individual (people)

**Importance:** 1-3 routine, 4-6 useful, 7-8 important, 9-10 critical

**Search Tools:**
- `search_memories` - Search CONTENT field only (faster, focused)
- `search_memories_full` - Search ALL fields including tags and metadata (broader)

**Tag Consistency (IMPORTANT):**
- ALWAYS call `get_existing_tags()` before creating memories to see available tags
- Use `get_tag_suggestions(query)` to find similar existing tags
- Reuse existing tags instead of creating variations

**Best Practices:**
- Search before starting work to find relevant context
- Create memories after solving problems or learning preferences
- Use existing tags and link related memories together
- Include repo/filename in technical memories for code-related knowledge
- Use get_memory to fetch full content if search results are truncated"""

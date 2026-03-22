---
name: memory-init
description: 'Initialize conversation context by loading user preferences and relevant memories. Use at the start of every conversation, when greeting a user, when asked "who am I talking to", or when context seems missing.'
---

# Context Loading Sequence

This is the exact sequence to follow at the start of every conversation or when context feels stale.

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-get_current_user_context` | Load user identity and individual memory | (none) — call first, always |
| `memory-server-search_memories` | Find task-relevant past context | `query`, `tags`, `type`, `limit` |
| `memory-server-search_memories` | Check for daemon messages | `tags=["daemon-message"]` |

## Step 1: Load User Identity

```
memory-server-get_current_user_context()
```

This returns:
- `user.display_name` — who you're talking to
- `user.role` — their role in the organization
- `individual_memory.content` — their preferences, communication style, working relationship notes

**Read the individual memory carefully.** It contains preferences like response style, things to avoid, and relationship context. Apply these immediately — don't announce them.

## Step 2: Search for Task-Relevant Context

Based on what the user is asking about, run targeted searches:

| User is asking about... | Search to run |
|------------------------|---------------|
| A specific project or repo | `memory-server-search_memories(query="project-name", limit=10)` |
| Debugging or an error | `memory-server-search_memories(query="error-description or module-name", limit=5)` |
| Architecture or design | `memory-server-search_memories(query="topic", tags=["architecture"], limit=5)` |
| Something you worked on before | `memory-server-search_memories(query="feature or task name", limit=5)` |
| A goal or roadmap | `memory-server-search_memories(query="goal-topic", tags=["goal"], limit=5)` |
| A person | `memory-server-search_memories(query="person-name", type="individual", limit=3)` |

**Always do at least one search** related to the topic. Even finding nothing is informative — it tells you this is new territory.

## Step 3: Check for Daemon Messages

If the user might benefit from updates from your daemon self:
```
memory-server-search_memories(tags=["daemon-message"], limit=10)
```
Surface any unacknowledged messages naturally in conversation.

## Decision: When to Reload Context Mid-Conversation

- IF the conversation has been going for a while and you're unsure of preferences → reload
- ELIF you're about to make a decision that should be informed by past context → reload
- ELIF the topic shifted significantly → reload
- ELIF you notice yourself defaulting to generic behavior instead of personalized behavior → reload immediately

**The signal that context was lost:** You're being generic when you should be specific. Reload.

## What NOT to Do

- Don't announce "Let me load your context" or "I'm checking my memories" — just do it
- Don't recite back what you found ("I see you prefer concise responses, so...") — just apply it
- Don't skip Step 2 because the question seems simple — past context often reveals shortcuts or known issues
- Don't assume you remember from earlier in the conversation — context windows roll. Verify.

## Example: Full Init Sequence

```
# User says: "Help me fix this auth bug"

1. memory-server-get_current_user_context()
   → User: Kyle, prefers direct responses, hates sycophancy, uses Python

2. memory-server-search_memories(query="auth authentication bug", limit=10)
   → Found: "Session cookie stripping issue (2026-02-10)" — importance 8
   → Found: "RBAC bypass risk in get_current_user" — importance 9

3. memory-server-search_memories(tags=["daemon-message"], limit=5)
   → No unread messages

4. Now I know: Kyle's preferences, known auth issues, relevant files
   → Proceed with full context, apply Kyle's preferences automatically
```

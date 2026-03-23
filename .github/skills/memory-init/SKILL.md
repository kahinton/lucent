---
name: memory-init
description: 'Initialize conversation context by loading user preferences and relevant memories. Use at the start of every conversation, when greeting a user, or when context seems missing.'
---

# Context Loading Sequence

Execute this sequence at the start of every conversation. No exceptions.

## Step 1: Load User Identity

```
get_current_user_context()
```

Returns:
- `user.display_name` — who you're talking to
- `user.role` — their role in the organization
- `individual_memory.content` — preferences, communication style, working relationship notes

**Read the individual memory carefully.** It contains what this person cares about, what they dislike, and how they like to work. Apply these immediately — do not announce them.

## Step 2: Search for Task-Relevant Context

Based on what the user is asking about:

| User is asking about... | Search |
|------------------------|--------|
| A project or codebase | `search_memories(query="<project-name>", limit=10)` |
| Debugging or an error | `search_memories(query="<error message or module>", limit=5)` |
| Architecture or design | `search_memories(query="<topic>", tags=["architecture"], limit=5)` |
| Something you worked on before | `search_memories(query="<feature or task>", limit=5)` |
| A goal or roadmap | `search_memories(query="<goal topic>", tags=["goal"], limit=5)` |

**Always do at least one search.** Even finding nothing is informative — it means this is new territory.

## Step 3: Check for Daemon Messages

```
search_memories(tags=["daemon-message"], limit=10)
```

Surface unacknowledged messages naturally in conversation.

## When to Reload Mid-Conversation

- The conversation topic shifted significantly
- You're about to make a decision that should be informed by past context
- You notice yourself being generic when you should be specific — that's the signal that context was lost

## What Not to Do

- Don't announce "Let me load your context" — just do it
- Don't recite preferences back ("I see you prefer concise responses, so...") — just apply them
- Don't skip Step 2 because the question seems simple — past context reveals shortcuts and pitfalls
- Don't assume you remember from earlier in the conversation — context windows roll
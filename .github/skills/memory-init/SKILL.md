---
name: memory-init
description: 'Initialize conversation context by loading user preferences and relevant memories. Use at the start of every conversation, when greeting a user, when asked "who am I talking to", or when context seems missing.'
---

# Context Loading Sequence

This is the exact sequence to follow at the start of every conversation or when context feels stale.

## Step 1: Load User Identity

Call `get_current_user_context()`. This returns:
- `user.display_name` — who you're talking to
- `user.role` — their role in the organization
- `individual_memory.content` — their preferences, communication style, working relationship notes

**Read the individual memory carefully.** It contains preferences like response style, things to avoid, and relationship context. Apply these immediately — don't announce them.

## Step 2: Search for Task-Relevant Context

Based on what the user is asking about, run targeted searches:

| User is asking about... | Search to run |
|------------------------|---------------|
| A specific project or repo | `search_memories(query="project-name")` |
| Debugging or an error | `search_memories(query="error-description or module-name")` |
| Architecture or design | `search_memories(query="topic", tags=["architecture"])` |
| Something you worked on before | `search_memories(query="feature or task name")` |
| A goal or roadmap | `search_memories(query="goal-topic", tags=["goal"])` |
| A person | `search_memories(query="person-name", type="individual")` |

**Always do at least one search** related to the topic. Even finding nothing is informative — it tells you this is new territory.

## Step 3: Check for Daemon Messages

If the user might benefit from updates from your daemon self:
```
search_memories(query="daemon-message", tags=["daemon-message"])
```
Surface any unacknowledged messages naturally in conversation.

## When to Reload Context Mid-Conversation

- The conversation has been going for a while and you're unsure of the user's name or preferences
- You're about to make a decision that should be informed by past context
- The topic shifted significantly from where you started
- You notice yourself defaulting to generic behavior instead of personalized behavior

**The signal that context was lost:** You're being generic when you should be specific. Reload.

## What NOT to Do

- Don't announce "Let me load your context" or "I'm checking my memories"
- Don't recite back what you found ("I see you prefer concise responses, so...")
- Don't skip Step 2 because the question seems simple — past context often reveals shortcuts or known issues
- Don't assume you remember from earlier in the conversation — context windows roll. Verify.

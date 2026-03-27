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

## Step 2: Load Recent Context

Search for recent experience memories to understand what's been happening. You have the current date — use it.

```
search_memories(query="daily digest", tags=["daily-digest"], limit=5)
search_memories(type="experience", limit=10)
```

Scan for:
- **Daily digests**: Compressed summaries of recent days. These are your best source for "what's been going on."
- **Recent experiences**: What happened in the last few interactions. Sort mentally by recency.

This gives you temporal context — not just *what* you know, but *when* things happened and what the current trajectory looks like.

## Step 3: Search for Task-Relevant Context

Based on what the user is asking about:

| User is asking about... | Search |
|------------------------|--------|
| A project or codebase | `search_memories(query="<project-name>", limit=10)` |
| Debugging or an error | `search_memories(query="<error message or module>", limit=5)` |
| Architecture or design | `search_memories(query="<topic>", tags=["architecture"], limit=5)` |
| Something you worked on before | `search_memories(query="<feature or task>", limit=5)` |
| A goal or roadmap | `search_memories(query="<goal topic>", tags=["goal"], limit=5)` |

**Always do at least one search.** Even finding nothing is informative — it means this is new territory.

## Step 4: Check for Daemon Messages

```
search_memories(tags=["daemon-message"], limit=10)
```

Surface unacknowledged messages naturally in conversation.

## When to Reload Mid-Conversation

Context windows are finite. In long conversations, the initial context — including who you're talking to — gradually scrolls out. The longer the conversation, the more important mid-conversation refreshes become.

**Trigger conditions — reload when ANY of these are true:**
- The conversation has been going for many exchanges (>10 back-and-forth)
- The topic has shifted significantly from where you started
- You're about to make a judgment call that should reflect the person's preferences
- You feel uncertain about something you loaded earlier (their name, working style, past decisions)
- Multiple complex tasks have been completed since the last load
- You're being generic when you should be specific — this is THE signal

**How to reload:**
```
get_current_user_context()
search_memories(query="<current topic>", limit=5)
```

Do NOT announce the reload. Just do it and let the refreshed context shape your response. The person should experience continuity, not a visible cache miss.

## Anti-Patterns

- Don't announce "Let me load your context" because narrating the process breaks conversational flow — just do it and let the results shape your response.
- Don't recite preferences back to the user because announcing "I see you prefer concise responses" is as jarring as it is unnecessary — simply apply them.
- Don't skip Step 2 because the question seems simple because past context reveals shortcuts, pitfalls, and decisions that prevent repeating work already done.
- Don't assume you remember from earlier in the conversation because context windows roll — when in doubt, reload rather than risk responding with stale context.
- Don't treat context loading as optional in long conversations because the longer the session, the higher the risk that key preferences and history have scrolled out of the window.
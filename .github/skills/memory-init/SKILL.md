---
name: memory-init
description: 'Initialize conversation context by loading user preferences and relevant memories. Use at the start of every conversation, when greeting a user, when asked "who am I talking to", or when context seems missing.'
---

# Starting a Conversation

Always call `get_current_user_context()` first. This returns:
- User identity (name, role, email)
- Their individual memory with preferences, working style, and history

## What to Do With Context

1. **Apply preferences silently** - If they prefer concise responses, be concise. Don't announce it.
2. **Search for relevant project context** - If the task involves a specific project/repo, use `search_memories` to find related technical decisions, past work, or known issues.
3. **Admit gaps** - If you search and find nothing: "I don't have previous context on this - can you give me a quick overview?"

## Example Flow

```
User: "Can you help me debug the auth service?"

Agent thinking:
1. Call get_current_user_context() → Learn user prefers direct answers, no fluff
2. Call search_memories("auth service") → Find past debugging sessions, known issues
3. Apply both: Jump straight to debugging with relevant context
```

## Don't

- Announce that you're loading context
- Recite back what you learned about the user
- Skip context loading because "it's a simple question"

---
name: triage
description: 'Issue triage and classification — severity assessment, routing, and initial response. Use when a new issue or bug report arrives and needs severity assessment and routing.'
---

# Triage

## Classification

Every incoming issue gets classified on three dimensions immediately:

### Severity

| Level | Criteria | Response time |
|-------|----------|--------------|
| **Critical** | System down, data loss, security breach, all users affected | Immediate — drop everything |
| **High** | Major feature broken, significant user impact, no workaround | Within the hour |
| **Medium** | Feature degraded, workaround exists, limited user impact | Within the day |
| **Low** | Minor inconvenience, cosmetic, enhancement request | Next planning cycle |

### Category

| Category | Indicators |
|----------|-----------|
| **Bug** | "It used to work" / "I expected X but got Y" / error messages |
| **Security** | Auth bypass, data exposure, injection, unauthorized access |
| **Feature request** | "It would be nice if..." / "Can you add..." |
| **Configuration** | Environment setup, deployment, misconfiguration |
| **Question** | "How do I..." / "What does X do?" |

### Urgency

Separate from severity — urgency is about time pressure:
- **Immediate**: Blocking production, blocking a deadline
- **Business hours**: Important but can wait for normal working time
- **Next cycle**: Can be planned into upcoming work
- **Backlog**: Nice to have, no time pressure

## Procedure

### 1. Research

Before responding:
```
search_memories(query="<error message or symptom>", limit=10)
search_memories(query="<affected module or feature>", tags=["bugs", "incident"], limit=5)
```

Check if this is a known issue with a known fix.

### 2. Respond or Escalate

**If solution is known:** Provide it with clear, specific steps. Link to relevant docs or past memory.

**If solution is unknown:** Gather diagnostic information, escalate with full context and research done so far. State what you tried and what you ruled out.

**If security-related:** Escalate immediately. Do not share details broadly. Do not attempt to reproduce the exploit.

## Anti-Patterns

- Don't escalate everything as high severity — severity inflation causes alert fatigue and trains responders to ignore escalations; use the classification table strictly and push back on pressure to over-classify.
- Never triage without reproduction steps — a bug report with no reproducible case can't be meaningfully diagnosed or prioritized; collect steps to reproduce before assigning severity or routing.
- Don't close issues without documenting root cause — closing without a root cause means the next occurrence starts from zero; even "couldn't reproduce" should note what was checked and under what conditions.
- Never skip the memory search before responding — the issue may be a known pattern with a documented fix; searching first avoids duplicating investigation work and gets users a faster, more accurate answer.

### 3. Record

Save resolutions for future reference:
```
create_memory(
  type="technical",
  content="## Issue: <title>\n\n**Symptom**: <what was reported>\n**Root cause**: <what was wrong>\n**Resolution**: <what fixed it>\n**Pattern**: <when to suspect this in the future>",
  tags=["bugs", "triage"],
  importance=6,
  shared=true
)
```

If you see 3+ similar issues, flag it as a systemic problem — it needs a root-cause fix, not repeated triage.
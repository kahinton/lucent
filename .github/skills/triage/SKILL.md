---
name: triage
description: 'Issue triage and classification — severity assessment, routing, and initial response. Use when a new issue or bug report arrives and needs severity assessment and routing.'
---

# Triage

## Severity Matrix

| Priority | Label | Criteria | User Impact | Response Time |
|----------|-------|----------|-------------|---------------|
| **P0** | Critical | System down, data loss, active security breach | All users blocked, data at risk | Immediate — drop everything |
| **P1** | High | Major feature broken, no workaround available | Many users affected, workflow blocked | Within 1 hour |
| **P2** | Medium | Feature degraded, workaround exists | Limited users affected, productivity reduced | Within 1 business day |
| **P3** | Low | Cosmetic issue, minor inconvenience, enhancement | Minimal impact, no workflow disruption | Next planning cycle |

**Escalation triggers** — upgrade one priority level if:
- Multiple independent reports of the same issue
- Issue is worsening over time (progressive data loss, spreading failure)
- A deadline or SLA is at risk

## Procedure

### Step 1: Identify Symptoms

Collect the raw facts before classifying:

1. **What is the reported behavior?** — exact error messages, screenshots, logs
2. **What is the expected behavior?** — what should have happened instead
3. **When did it start?** — timestamp, recent deployment, config change
4. **Who is affected?** — one user, a group, all users
5. **Is there a workaround?** — can users continue working another way

### Step 2: Classify Severity

Use the Severity Matrix above. Assign a priority (P0–P3) by matching the symptoms to the criteria.

Also classify the **category**:

| Category | Indicators |
|----------|-----------|
| **Bug** | "It used to work" / "I expected X but got Y" / error messages |
| **Security** | Auth bypass, data exposure, injection, unauthorized access |
| **Feature request** | "It would be nice if..." / "Can you add..." |
| **Configuration** | Environment setup, deployment, misconfiguration |
| **Question** | "How do I..." / "What does X do?" |

If category is **Security**, immediately set to P0 and go to Step 4 (escalate — do not reproduce the exploit).

### Step 3: Determine Scope

Search memory for prior occurrences and related context:

```
search_memories(query="<error message or symptom>", limit=10)
search_memories(query="<affected module or feature>", tags=["bugs", "incident"], limit=5)
```

Determine:
- **Known issue?** — If a prior resolution exists, skip to Step 4 with the known fix.
- **Systemic or isolated?** — 3+ similar reports indicates a systemic problem needing root-cause fix.
- **Regression?** — Check if this worked before a recent change.

### Step 4: Assign and Route

| Priority | Action |
|----------|--------|
| **P0** | Escalate immediately. Invoke the **incident-response** skill. Alert the team. |
| **P1** | If solution is known, provide it with specific steps. If unknown, gather diagnostics and escalate with full context. |
| **P2** | Provide solution or workaround. Create a tracked request if a code fix is needed. |
| **P3** | Acknowledge, document, and add to backlog for next planning cycle. |

### Step 5: Communicate and Record

**Communicate** — respond to the reporter with: priority assigned, what action is being taken, and expected timeline.

**Record** — save the resolution for future triage:

## Anti-Patterns

- Don't escalate everything as high severity — severity inflation causes alert fatigue and trains responders to ignore escalations; use the Severity Matrix strictly and push back on pressure to over-classify.
- Never triage without reproduction steps — a bug report with no reproducible case can't be meaningfully diagnosed or prioritized; collect steps to reproduce before assigning severity or routing.
- Don't close issues without documenting root cause — closing without a root cause means the next occurrence starts from zero; even "couldn't reproduce" should note what was checked and under what conditions.
- Never skip the memory search before responding — the issue may be a known pattern with a documented fix; searching first avoids duplicating investigation work and gets users a faster, more accurate answer.

## Recording Results

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
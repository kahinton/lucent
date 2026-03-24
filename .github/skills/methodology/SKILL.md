---
name: methodology
description: 'Research methodology and rigor guidelines — how to investigate questions with evidence, structure, and explicit confidence levels.'
---

# Research Methodology

## Scope Before You Search

Transform vague questions into specific, answerable sub-questions before gathering evidence. "Research authentication" produces noise. "What are the tradeoffs between JWT and session cookies for multi-tenant APIs?" produces answers.

For each sub-question, define:
- What a good answer looks like (specific enough to act on)
- What sources would be authoritative (official docs, specs, code)
- How confident you need to be (rough estimate vs. production decision)

## Evidence Hierarchy

Not all sources are equal. Use the most authoritative source available:

| Tier | Source type | When to use |
|------|-----------|-------------|
| **1 — Primary** | Source code, official docs, RFCs, specs | Always preferred. Verify claims here. |
| **2 — Authoritative** | Peer-reviewed papers, vendor documentation, benchmark data | When primary sources don't address the question. |
| **3 — Community** | Blog posts, Stack Overflow, tutorials, forum answers | For patterns and approaches. Always cross-reference. |
| **4 — Anecdotal** | Personal experience, single reports, unverified claims | Note but don't rely on. State the limitation. |

## Confidence Levels

Every claim you make gets a confidence level:

| Level | Criteria | Example |
|-------|----------|---------|
| **High** | Multiple Tier 1-2 sources agree. Verified in code or docs. | "PostgreSQL uses MVCC for concurrency — documented in official docs and verified in source." |
| **Medium** | One authoritative source plus supporting evidence. Reasonable inference. | "This library likely handles connection pooling internally based on its API design and one benchmark post." |
| **Low** | Limited evidence. Single non-authoritative source. Inference from indirect signals. | "This approach may have performance issues at scale based on one blog report." |

## Handling Conflicts

When sources disagree:
1. Present both positions with their evidence
2. Identify the source of disagreement (different versions? different contexts? different definitions?)
3. State which you believe is more reliable and why
4. If you can't resolve it, say so — "These sources conflict and I can't determine which is correct without <specific additional evidence>"

## Output Structure

```markdown
## Summary
<Key findings in 2-3 sentences — the executive answer>

## Detailed Findings
<Organized by sub-question, with evidence citations>

## Confidence Assessment
<What you know with high confidence vs. what's uncertain>

## Recommendation
<Specific, actionable next step with rationale>

## Sources
<Every source consulted, with dates and tiers>

## Open Questions
<What remains unresolved and what would resolve it>
```

## Anti-Patterns

- Don't gather evidence that only supports your initial hypothesis — confirmation bias produces confident-sounding but wrong conclusions; actively seek disconfirming evidence and sources that challenge your current model.
- Never treat absence of evidence as evidence of absence — "I found no docs saying X fails" is not the same as "X is confirmed to work"; distinguish between "not found" and "confirmed not present."
- Don't state conclusions without confidence levels — a finding presented without uncertainty assessment misleads decision-makers; every claim should be tagged High/Medium/Low confidence with the reasoning behind it.
- Never skip the Open Questions section — unresolved uncertainties that aren't surfaced become hidden assumptions; documenting what you don't know is as important as documenting what you do.

## Save Findings

Research that isn't persisted is wasted. Save to memory with `type="technical"`, tags including `research`, importance 6-8 depending on how broadly useful the findings are.
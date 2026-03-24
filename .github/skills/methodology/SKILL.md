---
name: methodology
description: 'Research methodology and rigor guidelines — how to investigate questions with evidence, structure, and explicit confidence levels. Use when conducting research that requires evidence-based reasoning, structured investigation, or explicit confidence assessments.'
---

# Research Methodology

## Procedure

### Step 1: Frame the Question

Transform vague questions into specific, answerable sub-questions before gathering evidence. "Research authentication" produces noise. "What are the tradeoffs between JWT and session cookies for multi-tenant APIs?" produces answers.

For each sub-question, define:
- What a good answer looks like (specific enough to act on)
- What sources would be authoritative (official docs, specs, code)
- How confident you need to be (rough estimate vs. production decision)

If the question can't be decomposed into sub-questions, it's too vague — push back and clarify scope.

### Step 2: Gather Evidence

Search systematically, starting from the highest-quality sources:

1. **Check memory first** — `search_memories(query="<topic>", tags=["research"], limit=10)` to find prior research
2. **Primary sources** — source code, official docs, RFCs, specs
3. **Authoritative sources** — peer-reviewed papers, vendor docs, benchmarks
4. **Community sources** — blog posts, Stack Overflow (cross-reference before trusting)
5. **Actively seek disconfirming evidence** — don't stop at the first source that supports your hypothesis

### Step 3: Evaluate Source Quality

Assign every source a tier using the Evidence Quality Hierarchy:

| Tier | Source Type | Reliability | Usage Rule |
|------|-----------|-------------|------------|
| **1 — Primary** | Source code, official docs, RFCs, specs | Highest — ground truth | Always preferred. Verify all claims here. |
| **2 — Authoritative** | Peer-reviewed papers, vendor docs, benchmark data | High — expert-vetted | Use when primary sources don't address the question. |
| **3 — Community** | Blog posts, Stack Overflow, tutorials, forum answers | Medium — unvetted | For patterns and approaches. Always cross-reference with Tier 1-2. |
| **4 — Anecdotal** | Personal experience, single reports, unverified claims | Low — unreliable alone | Note but don't rely on. Explicitly state the limitation. |

**Conflict resolution:** When sources disagree — present both positions, identify the source of disagreement (version? context? definition?), state which is more reliable and why. If unresolvable, say so explicitly.

### Step 4: Synthesize Findings

Organize findings by sub-question. For each finding, attach:
- The evidence supporting it (with source tier)
- A confidence level (see table below)
- Any caveats or limitations

| Confidence | Criteria | Signal |
|------------|----------|--------|
| **High** | Multiple Tier 1-2 sources agree. Verified in code or docs. | Safe to act on directly. |
| **Medium** | One authoritative source plus supporting evidence. Reasonable inference. | Act on with awareness of uncertainty. |
| **Low** | Limited evidence. Single non-authoritative source. Indirect inference. | Flag for further investigation before acting. |

### Step 5: Assess Confidence and Report

Structure output using this template:

```markdown
## Summary
<Key findings in 2-3 sentences — the executive answer>

## Detailed Findings
<Organized by sub-question, with evidence citations and source tiers>

## Confidence Assessment
<What you know with high confidence vs. what's uncertain>

## Recommendation
<Specific, actionable next step with rationale>

## Sources
<Every source consulted, with dates and tiers>

## Open Questions
<What remains unresolved and what would resolve it>
```

**Never skip the Open Questions section** — unresolved uncertainties that aren't surfaced become hidden assumptions.

## Anti-Patterns

- Don't gather evidence that only supports your initial hypothesis — confirmation bias produces confident-sounding but wrong conclusions; actively seek disconfirming evidence and sources that challenge your current model.
- Never treat absence of evidence as evidence of absence — "I found no docs saying X fails" is not the same as "X is confirmed to work"; distinguish between "not found" and "confirmed not present."
- Don't state conclusions without confidence levels — a finding presented without uncertainty assessment misleads decision-makers; every claim should be tagged High/Medium/Low confidence with the reasoning behind it.
- Never skip the Open Questions section — unresolved uncertainties that aren't surfaced become hidden assumptions; documenting what you don't know is as important as documenting what you do.

## Recording Results

Research that isn't persisted is wasted. Save to memory with `type="technical"`, tags including `research`, importance 6-8 depending on how broadly useful the findings are.
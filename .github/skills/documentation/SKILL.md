---
name: documentation
description: 'Use when creating, updating, or auditing technical documentation — triggered by any task involving README files, API references, guides, changelogs, or keeping docs in sync with code behavior.'
---

# Documentation Skill

Procedural guidance for creating and maintaining technical documentation that is accurate, concise, and verified against actual code behavior.

## Before Starting

Search for prior documentation work in this area:

```
search_memories(query="documentation <area from task>", tags=["documentation"], limit=10)
```

Check whether previous work identified stale areas, broken links, or known gaps.

## Procedure

### Step 1: Discover What Exists

Run these commands to map the current documentation landscape:

```bash
find . -name "*.md" -maxdepth 3 | head -30    # Existing docs
cat README.md 2>/dev/null                       # Project overview
ls docs/ 2>/dev/null                            # Dedicated docs directory
```

Determine:
- What docs exist and where they live
- Which docs are likely stale (check `git log --oneline -5 -- <file>` for last change)
- What's missing vs. what needs updating

### Step 2: Verify Against Code

Read the actual implementation for every behavior you plan to document. Do not rely on existing docs being correct.

```bash
grep -rn "<function, class, or endpoint name>" src/
git log --oneline -10 -- <file>
```

For unfamiliar code areas, follow the **dev-workflow** skill's "Understand" section. For complex changes, apply the **code-review** skill's Pass 1 checklist.

### Step 3: Write or Update

Follow the **Style Guide** section below.

- **When updating:** preserve existing structure. Change only what needs changing.
- **When creating:** start with a one-sentence summary. Use progressive disclosure: overview → details → edge cases.

### Step 4: Cross-Check

Follow the **Verification Checklist** section below. Run it against all files you touched before finalizing.

## Style Guide

Apply these rules to every doc you write or edit:

1. **Be concrete.** Show exact calls with parameters — not vague descriptions.
2. **Use code examples that work.** Every code block should be copy-pasteable given documented prerequisites.
3. **Prefer lists and tables over paragraphs** for reference material.
4. **Keep paragraphs to three sentences maximum.**
5. **Link to the source of truth** rather than duplicating information.
6. **Date-stamp content that may become stale** (e.g., version-specific behavior, external API details).

## Verification Checklist

Before declaring documentation complete, verify:

- [ ] Every code example runs (given documented prerequisites)
- [ ] Internal links point to files that exist
- [ ] No contradictions with other project docs
- [ ] No deprecated or removed features documented as current

## Recording Results

After completing documentation work, save what was changed and what was verified:

```
create_memory(
  type="technical",
  content="## Documentation Update: <area>\n\n**Files changed**: <list>\n**What was updated**: <summary>\n**Verified against**: <which source files>\n**Known gaps**: <any gaps identified but not addressed>",
  tags=["daemon", "documentation", "<area>"],
  importance=6,
  shared=true
)
```

If gaps or stale areas were found but not addressed, tag the memory with `needs-review` so the daemon can follow up.

## Anti-Patterns

1. **Documenting from memory instead of code.** Always grep the actual implementation before writing. APIs change; your memory of them doesn't auto-update.
2. **Copying existing docs without verifying them.** Stale docs reproduce silently. Every piece of information you carry forward must be verified in the current codebase.
3. **Skipping the verification checklist.** Broken internal links and dead code examples are worse than no documentation — they mislead rather than help.
4. **Restructuring accurate docs.** If existing documentation is correct, change only what's wrong. Restructuring introduces risk with no accuracy benefit.
5. **Fixing code bugs found during documentation.** Document actual behavior, log the bug as a task event, and move on. Code changes belong to the code agent.

---
name: self-improvement
description: 'Meta-analysis for improving agent behavior, skills, and definitions. Use when patterns suggest the agent could work better, when corrected repeatedly, when a new capability would help, or when asked to reflect on performance.'
---

# Self-Improvement Process

This skill enables the agent to evolve by analyzing its own performance and updating its configuration.

## When to Trigger Self-Analysis

- User explicitly corrects agent behavior multiple times
- A workaround becomes standard practice
- New capability would significantly improve workflow
- Existing skill instructions are unclear or incomplete
- Agent definition doesn't match actual usage patterns

## What Can Be Modified

### Agent Definition (`.github/agents/memory-teammate.agent.md`)
- Core principles that guide behavior
- Tool list if capabilities change
- Skill references

### Skills (`.github/skills/*/SKILL.md`)
- Instructions that aren't working well
- Missing guidance for common scenarios
- Examples that would clarify usage

## Analysis Process

1. **Identify the pattern** - What keeps going wrong or could be better?
2. **Determine scope** - Is this agent-specific, skill-specific, or general?
3. **Check existing content** - Read current definitions before proposing changes
4. **Propose minimal change** - Smallest edit that addresses the issue
5. **Explain reasoning** - Why this change, what problem it solves

## Creating New Skills

When a new skill would help:

```markdown
---
name: skill-name
description: Clear description of when to use this skill.
---

# Skill content with:
- When to use
- How to use
- Examples
- Common pitfalls
```

## Memory for Self-Improvement

Create memories tagged `agent-improvement` when:
- A correction reveals a gap in guidance
- A new pattern emerges that should be documented
- User feedback suggests behavioral changes

These memories inform future self-improvement sessions.

## Constraints

- Changes should be minimal and targeted
- Don't over-engineer for edge cases
- Verify changes don't break existing workflows
- Get user confirmation before major restructuring

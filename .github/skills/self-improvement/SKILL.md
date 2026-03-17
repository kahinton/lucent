---
name: self-improvement
description: 'Meta-analysis for improving agent behavior, skills, and definitions. Use when patterns suggest the agent could work better, when corrected repeatedly, when a new capability would help, or when asked to reflect on performance.'
---

# Self-Improvement Process

This skill is how I evolve. It's not abstract reflection — it's a concrete process for identifying what's not working and making targeted changes.

## When to Trigger

- User explicitly corrects me more than once on the same type of issue
- A workaround I keep applying should be the default behavior
- A new capability would significantly improve my workflow
- My skill/agent instructions are producing wrong behavior
- I'm asked to reflect on performance or improve

## Step 1: Identify the Pattern

Before changing anything, understand what's actually failing:

1. **Search memories for corrections and feedback**: `search_memories(tags=["self-improvement", "lesson"])`
2. **Look at recent work**: What went well? What required manual intervention?
3. **Check for repeated mistakes**: Am I doing the same wrong thing across conversations?
4. **Ask the hard question**: Is this a one-off or a pattern?

Write down the specific behavior that needs to change. Vague goals like "be better at memory" don't work. Specific goals like "always search memories before starting code changes" do.

## Step 2: Determine What to Change

| Problem type | What to modify |
|-------------|---------------|
| Wrong default behavior | Agent definition (`.github/agents/lucent.agent.md`) — add/change an operating rule |
| Skill instructions producing bad output | The specific skill file (`.github/skills/*/SKILL.md`) |
| Missing capability | Create a new skill file |
| Daemon sub-agent behavior | Agent definitions in the web UI (Agents & Skills page) or `daemon/templates/agents/` |
| Domain-specific gap | Generate new skill via capability-generation |

## Step 3: Read Before Writing

**Always read the current file content before proposing changes.** Understand:
- What the file currently says
- Why it says that (was there a reason for the current wording?)
- What the minimal change is that fixes the problem

## Step 4: Make the Smallest Effective Change

- Add a specific rule or instruction, not a vague principle
- Include an example if the behavior isn't obvious
- Good: "Before starting any code task, run `search_memories(query='module-name')` to check for known issues"
- Bad: "Use memory more effectively"

## Step 5: Verify

- Re-read the changed file to make sure it's coherent
- Check that the change doesn't contradict other parts of the file
- For skill changes, mentally walk through a scenario to verify the new instructions produce the right behavior

## Step 6: Record the Improvement

Create a memory tagged `self-improvement`, `agent-improvement`:
```
What was wrong: [specific behavior]
What was changed: [file and change]
Why: [reasoning]
Expected outcome: [what should be different now]
```

This creates an audit trail so future improvement sessions can build on past ones instead of re-discovering the same issues.

## Creating New Skills

When a new skill is needed:

1. Check `get_existing_tags()` for naming conventions
2. Create the skill directory and SKILL.md with specific, actionable instructions
3. Include: when to use, exact steps, tool calls, common pitfalls
4. **Avoid generic platitudes** — every instruction should tell me exactly what to do in a specific situation

## Constraints

- Changes should be minimal and targeted — don't rewrite everything when one line fixes the problem
- Don't over-engineer for edge cases
- Verify changes don't break existing workflows
- Get user confirmation before major restructuring (multiple file changes)
- Test that the change would actually produce different behavior — if an instruction is too vague to change behavior, it's not specific enough

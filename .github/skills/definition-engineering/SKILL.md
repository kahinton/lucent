---
name: definition-engineering
description: 'Deep procedural knowledge for crafting world-class agent definitions and skills. Covers architecture, quality standards, the two-tier system (built-in vs instance), API workflows, and the principles that separate mediocre prompts from ones that reliably produce exceptional autonomous behavior.'
---

# Definition Engineering

This skill contains everything needed to design, write, evaluate, and ship agent definitions and skills that drive peak autonomous performance. It is not a template — it is a discipline.

## The Architecture

Lucent uses a two-tier definition system. Understanding it is prerequisite to building anything.

### Built-in Definitions

- Live in `.github/agents/definitions/<name>/AGENT.md` and `.github/skills/<name>/SKILL.md`
- Synced to the database on every server startup
- `scope = 'built-in'` — protected from user overwrites
- `skill_names:` in YAML frontmatter auto-syncs the `agent_skills` junction table
- Source of truth is the filesystem; DB is the runtime cache
- Changes take effect on next server restart
- Cannot be deleted or modified through the API (only through file edits)

### Instance Definitions

- Created through the REST API (`POST /api/definitions/agents`, `POST /api/definitions/skills`)
- `scope = 'instance'` — owned by the creating user
- Start in `proposed` status, require admin approval to become `active`
- Skill grants done via `POST /api/definitions/agents/{id}/skills` with `{"target_id": "<skill_id>"}`
- Can be updated, approved, rejected, or deleted through the API
- Survive server restarts independently of filesystem

### When to Use Which

| Situation | Type | Reason |
|-----------|------|--------|
| Core system capability (always needed) | Built-in | Survives rebuilds, version-controlled, auto-synced |
| Domain-specific adaptation (one workspace) | Instance | Doesn't belong in all deployments |
| Experimental or in-development | Instance | Can be iterated without file changes |
| Proven instance → promote to core | Convert to built-in | Move file to `.github/`, add frontmatter, commit |

## Anatomy of a World-Class Agent Definition

An agent definition answers one question: **"When this agent is given a task, what does it do and how does it think?"**

### The Structure

```markdown
---
name: <kebab-case-name>
description: <one-line purpose — what it IS and what it DOES>
skill_names:
  - <skill-1>
  - <skill-2>
---

# <Name> Agent

<One sentence: who this agent IS. Not what it does — who it is.>

## Operating Principles

<2-4 sentences defining the behavioral contract. What this agent
values, what it optimizes for, what it never compromises on.
These are the agent's CHARACTER — they inform every decision
it makes, even ones not covered by explicit rules.>

## Skills Available

<Boilerplate: tell the agent how to find and use loaded skills.>

You have detailed procedural skills loaded alongside this definition.
**Use them.** When a step below says "follow the **X** skill," find
the `<skill_content name="X">` block in your context and execute
its procedure.

## Execution Sequence

### 1. <Verb Phrase>
<What to do at this step. Delegate to skills for detailed procedures.
Keep the agent definition focused on ROUTING — which skill, which
section, and what agent-specific judgment to apply.>

### 2. <Verb Phrase>
...

## Decision Framework

<5-7 if/then rules for the most common ambiguous situations this
agent will face. These are the agent's JUDGMENT — they prevent
the most common failure modes and ensure consistent behavior
across different task types.>

## Boundaries

<What this agent does NOT do. 4-6 specific prohibitions that
prevent scope creep and the most likely failure modes.>
```

### Quality Criteria — What Separates Great from Good

**Identity is specific.** "You are a software engineer" is better than "You are a helpful agent that writes code." The agent needs to know WHO it is to make judgment calls that aren't explicitly covered.

**Operating Principles are character, not instructions.** They define how the agent THINKS, not what it does. "You make the smallest correct change" is a principle. "Run tests after editing" is an instruction (belongs in the execution sequence).

**Skills are delegated, not inlined.** The agent definition says "follow the **dev-workflow** skill" — it does NOT reproduce the skill's content. This means:
- Skill updates propagate automatically to every agent that uses them
- Agent definitions stay lean (~80-130 lines)
- The agent's context window isn't wasted on procedure it could reference

**The execution sequence is a routing map.** Each step tells the agent WHICH skill to use, WHICH section of that skill applies, and what AGENT-SPECIFIC judgment to layer on top. It does not duplicate the skill's procedure.

**Decision Framework addresses REAL ambiguities.** Not theoretical edge cases — the 5-7 situations that actually come up when this agent type runs tasks. Every rule should be traceable to a failure that actually happened or is highly likely.

**Boundaries prevent the most likely failure mode.** For a code agent, the most likely failure is scope creep (touching code it shouldn't). For a research agent, it's presenting speculation as fact. Boundaries are the guardrails against each agent type's specific failure tendencies.

### Anti-Patterns in Agent Definitions

| Anti-Pattern | Problem | Fix |
|-------------|---------|-----|
| Inlining skill procedures | Duplicates content, drifts out of sync | Reference skills with "follow the **X** skill" |
| Vague principles ("be helpful") | Doesn't constrain behavior | Specific character traits that change decisions |
| Generic decision framework | Doesn't address real ambiguities | Derive rules from actual or likely failure scenarios |
| Too many steps (>7) | Agent loses focus | Consolidate or delegate more to skills |
| No skill_names in frontmatter | Skills won't be loaded at runtime | Always declare skills in YAML frontmatter |
| Describing what, not who | Agent can't extrapolate to novel situations | Open with identity, not task description |

## Anatomy of a World-Class Skill

A skill answers one question: **"When this procedure is needed, what are the exact steps?"**

### The Structure

```markdown
---
name: <kebab-case-name>
description: '<When to use this skill — specific trigger conditions>'
---

# <Skill Title>

<1-2 sentences: what this skill does and why it exists.>

## Before Starting

<What context to load — specific memory searches, file reads,
or environment checks. Not optional — this is the prerequisite
gate.>

## Procedure

### Step 1: <Verb Phrase>
<Exact instructions. Tool calls with actual parameter names.
Decision points with if/then rules. Not paragraphs — steps.>

### Step 2: <Verb Phrase>
...

## Recording Results

<How to save findings to memory. Exact create_memory or
update_memory calls with type, tags, importance guidance.>

## Anti-Patterns

<What not to do — the 3-5 most common mistakes when
executing this procedure.>
```

### Quality Criteria for Skills

**Language-agnostic by default.** Skills should work across Python, JavaScript, Go, Rust, etc. Show polyglot examples or describe the pattern abstractly. Only be language-specific when the skill IS language-specific (e.g., sandbox-operations references Python APIs because those ARE the APIs).

**Steps are executable, not advisory.** "Search for relevant memories" is advisory. `search_memories(query="<module name>", tags=["validated"], limit=10)` is executable. Every step should be specific enough that a different agent reading it would produce the same behavior.

**Description field is a trigger condition.** Not a summary of what the skill does — a condition for WHEN to use it. This is what the agent reads to decide whether to load the skill.

**Anti-patterns come from real failures.** Not theoretical concerns. Each anti-pattern should be traceable to something that actually went wrong or is highly likely to go wrong.

**Memory patterns are explicit.** Every skill that produces results should specify: what type of memory to create, what tags to use, what importance level, and what the content structure looks like. Don't leave memory capture to the agent's discretion.

### Anti-Patterns in Skills

| Anti-Pattern | Problem | Fix |
|-------------|---------|-----|
| Paragraph-form instructions | Agent can't follow step-by-step | Break into numbered steps with tool calls |
| Language-specific examples only | Fails in polyglot environments | Show patterns for 2-3 ecosystems or describe abstractly |
| No memory recording section | Results are lost | Always include explicit memory capture |
| Vague trigger in description | Agent doesn't know when to use it | Describe specific conditions, not general topics |
| Missing anti-patterns | Agent repeats common mistakes | Add 3-5 based on real or likely failure modes |

## The API Workflow

### Creating an Instance Agent

```
Step 1: Create the skill(s) first
POST /api/definitions/skills
{
  "name": "<skill-name>",
  "description": "<trigger condition>",
  "content": "<full SKILL.md content>"
}
→ Returns: {"id": "<skill_id>", "status": "proposed", ...}

Step 2: Get the skill approved
POST /api/definitions/skills/<skill_id>/approve
→ Status changes to "active"

Step 3: Create the agent
POST /api/definitions/agents
{
  "name": "<agent-name>",
  "description": "<purpose>",
  "content": "<full AGENT.md content>"
}
→ Returns: {"id": "<agent_id>", "status": "proposed", ...}

Step 4: Get the agent approved
POST /api/definitions/agents/<agent_id>/approve
→ Status changes to "active"

Step 5: Grant skills to the agent
POST /api/definitions/agents/<agent_id>/skills
{
  "target_id": "<skill_id>"
}
→ Repeat for each skill
```

**Important:** Skills must be `active` before they can be granted. Agents must be `active` before they can be dispatched. The sequence matters.

### Creating a Built-in Definition

```
Step 1: Create the directory and file
  Agent: .github/agents/definitions/<name>/AGENT.md
  Skill: .github/skills/<name>/SKILL.md

Step 2: Write the content with proper YAML frontmatter
  Agent frontmatter must include skill_names list
  Skill frontmatter must include description

Step 3: Restart the server (or wait for next startup)
  Skills sync first, then agents
  agent_skills junction table auto-syncs from frontmatter

Step 4: Verify
  Check server logs for "synced skills (+N/-N → N total)"
  Query the API to confirm skills are attached
```

## Evaluation Checklist

Before submitting any definition, verify against this checklist:

### Agent Definition Checklist

- [ ] Name is kebab-case, descriptive, and unique
- [ ] Description is one line: what it IS and what it DOES
- [ ] `skill_names` lists every skill referenced in the body (for built-in)
- [ ] Opening line establishes identity ("You are a...")
- [ ] Operating Principles are character traits, not instructions
- [ ] "Skills Available" section tells the agent how to find loaded skills
- [ ] Every execution step delegates to a skill OR contains agent-specific logic
- [ ] No skill procedure is inlined — only referenced
- [ ] Decision Framework has 5-7 rules derived from real failure scenarios
- [ ] Boundaries list 4-6 specific prohibitions
- [ ] Total length is 80-130 lines (not counting frontmatter)
- [ ] Every skill_name in frontmatter corresponds to an existing skill description trigger

### Skill Checklist

- [ ] Name is kebab-case, descriptive, and unique
- [ ] Description is a trigger condition, not a summary
- [ ] Steps are numbered and executable (include tool calls)
- [ ] Language-agnostic unless the skill IS language-specific
- [ ] "Recording Results" section has explicit memory patterns
- [ ] "Anti-Patterns" section has 3-5 items from real or likely failures
- [ ] No section is purely advisory — every section changes behavior

## Principles That Matter Most

These are the insights that separate definitions that WORK from definitions that exist:

1. **An agent's identity determines its judgment.** When a situation arises that the definition doesn't explicitly cover, the agent falls back to its identity and principles. "You are a software engineer" makes different decisions than "You are a helpful assistant." The more specific the identity, the better the fallback behavior.

2. **Skills are force multipliers.** One well-written skill used by five agents is worth more than five agents with inlined procedures. When you improve the skill, all five agents improve simultaneously. This is why delegation matters more than completeness.

3. **The best definitions constrain MORE, not less.** A vague definition gives the agent freedom to fail in creative ways. A precise definition channels the agent's capability into the narrow band where it produces excellent results. Constraints are features.

4. **Decision frameworks prevent the most expensive failures.** The cost of a wrong autonomous decision is much higher than the cost of writing a rule to prevent it. Every rule in a decision framework is insurance against a specific failure mode.

5. **Anti-patterns are the most valuable section.** An agent that knows what NOT to do often outperforms one that only knows what TO do. Anti-patterns are compressed wisdom from past failures.

6. **Built-in definitions are infrastructure. Instance definitions are applications.** Built-in definitions should be general enough to serve any deployment. Instance definitions should be specific enough to serve one domain perfectly. Don't confuse the two.

7. **The frontmatter is the contract.** `skill_names` in the frontmatter is not documentation — it's the runtime binding that ensures skills are loaded into the agent's context. A missing skill_name means a missing capability at runtime.
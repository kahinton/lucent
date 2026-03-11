---
name: placeholder-audit
description: 'Systematic audit and replacement of remaining placeholder skill content (daemon-debugging, mcp-protocol-testing, onboarding still have code-review template text)'
---

# Placeholder Audit

Systematically audit skill files for placeholder content and replace them with domain-specific content matching each skill's description.

## When to Use

- After generating new skills from templates (templates often produce generic placeholder content)
- When a skill's body content does not match its YAML frontmatter description
- During periodic quality checks of the `.github/skills/` directory
- When a skill behaves incorrectly because its instructions are from a different skill's template

## Placeholder Indicators

Content is likely placeholder if it contains any of these patterns:

- `"Code review skill for python/fastapi projects"` — default template text
- `"Reviewing pull requests or code changes"` — code-review template "When to Use"
- `"Evaluating code quality during development"` — code-review template
- Generic review steps (Understand the Change → Check Correctness → Check Style → Check Security → Check Performance → Summarize) that do not match the skill's stated purpose
- Body content that is identical across multiple unrelated skills

## Audit Process

### Step 1: Scan All Skill Files

```bash
find .github/skills -name "SKILL.md" -type f
```

For each file, extract the `name` and `description` from YAML frontmatter and the first few lines of body content.

### Step 2: Compare Content to Description

For each skill file:

1. Read the `description` field from the YAML frontmatter
2. Read the body content (everything after the closing `---`)
3. Check if the body content is relevant to the description
4. Flag the file if body content contains placeholder indicators or is clearly unrelated to the description

### Step 3: Flag Mismatches

Create a list of flagged files with:
- **File path**
- **Expected topic** (from description)
- **Actual topic** (from body content)
- **Placeholder indicators found**

### Step 4: Generate Replacement Content

For each flagged file:

1. Keep the YAML frontmatter (`---` block) exactly as-is
2. Write a new heading matching the skill name
3. Write a brief description paragraph aligned with the frontmatter description
4. Add a "When to Use" section with domain-relevant triggers
5. Add procedural steps specific to the skill's domain
6. Add a "Best Practices" section with domain-relevant guidance
7. Reference actual project files and directories where applicable

### Step 5: Validate Replacements

After replacing content:

1. Verify YAML frontmatter is unchanged (name and description intact)
2. Verify the new body content is relevant to the skill description
3. Verify no placeholder indicators remain
4. Check that file references (paths, commands) are accurate for the project

## Best Practices

- Never modify the YAML frontmatter during a placeholder audit — only replace body content
- Use the skill's `description` field as the primary guide for what the content should cover
- Reference real project paths (`src/lucent/`, `daemon/`, `tests/`) rather than generic examples
- After fixing placeholders, re-run the scan to confirm no placeholder indicators remain
- Keep a record of which skills were fixed and when, to track template generation quality

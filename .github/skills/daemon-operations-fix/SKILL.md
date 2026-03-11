---
name: daemon-operations-fix
description: 'Identify and replace placeholder skill content that was incorrectly generated from templates'
---

# Daemon Operations Fix

Procedure for identifying and fixing skills that contain placeholder content (usually copy-pasted code-review template text instead of domain-specific procedures).

## When to Use

- After running the adaptation pipeline (`adaptation.py`) which may generate skills from templates
- When a skill's content doesn't match its name/description
- During periodic skill quality audits

## Detection

### Signs of Placeholder Content

1. **Mismatched title**: Skill name says "daemon-operations" but content says "Code review skill for python/fastapi projects"
2. **Generic steps**: "Step 1: Understand the Change" / "Step 2: Check Correctness" pattern
3. **Code-review vocabulary**: "PR description", "approve / request-changes", "verdict"
4. **No domain specifics**: No mention of the actual domain (daemon cycles, memory operations, etc.)

### Quick Audit

```bash
# Find skills with code-review placeholder content
grep -rl "Code review skill for" .github/skills/*/SKILL.md
```

## Fix Procedure

1. **Read the skill description** in the frontmatter — it describes what the skill *should* contain
2. **Check for a specialized variant** — if `<skill>-specialized/SKILL.md` exists with good content, use it as reference
3. **Write domain-specific content** matching the skill's purpose:
   - "When to Use" should list actual use cases for the skill's domain
   - Procedures should be specific to the project architecture
   - Include concrete examples, commands, file paths
4. **Verify** the new content passes the detection checks above (no placeholder patterns remain)

## Prevention

The `adaptation.py` template system uses `_SKILL_TEMPLATE` which defaults to code-review content. When generating skills for non-code-review purposes:
- Override the template body with domain-appropriate content
- Or generate a minimal stub that clearly says "TODO: populate with [domain] procedures" rather than misleading placeholder content

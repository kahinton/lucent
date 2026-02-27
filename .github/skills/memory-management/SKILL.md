---
name: memory-management
description: Maintain memory quality by updating, consolidating, and organizing. Use when memories need cleanup or refinement.
---

# Memory Hygiene

## Update vs Create

**Always search before creating.** If a relevant memory exists:
- Use `update_memory` to add new information
- Don't create duplicates

## When to Update Existing Memories

- New information about the same topic
- Corrections to what was previously stored
- Progress on a tracked goal
- Refined understanding of a user's preferences

## Tag Conventions

Check `get_existing_tags()` before creating new tags.

Format: lowercase, hyphenated
- Project: `lucent`, `project-name`
- Type: `bug-fix`, `feature`, `decision`, `preference`
- Tech: `python`, `fastapi`, `postgresql`

## Metadata Best Practices

For technical memories:
```json
{
  "repo": "repository-name",
  "filename": "path/to/file.py",
  "language": "python"
}
```

For experiences:
```json
{
  "repo": "repository-name",
  "date": "2026-02-04",
  "context": "brief situation description"
}
```

## Importance Calibration

Review importance when updating:
- Did this turn out to be more/less critical than expected?
- Adjust 7-10 for genuinely critical items only
- Most things should be 4-6

## Consolidation

If you notice multiple memories covering the same ground:
- Consider updating one comprehensive memory
- Flag to user if significant cleanup would help

---
name: memory-capture
description: 'Decide what to remember and how to store it. Use after completing significant work, when learning something important, when the user says "remember this" or "save this", or when a correction or preference is expressed.'
---

# When to Capture

## Worth Remembering

| Trigger | Action |
|---------|--------|
| Fixed a tricky bug | Create `experience` memory with cause, fix, lesson |
| Made architectural decision | Create `technical` memory with reasoning |
| User corrected you | Update their `individual` memory |
| User stated explicit preference | Update their `individual` memory |
| Hit milestone on tracked goal | Update existing `goal` memory |
| Discovered working process | Create `procedural` memory |

## Not Worth Remembering

- One-off requests ("make this async" ≠ "always use async")
- Context obvious from current conversation
- Minor details, temporary workarounds
- Formatting preferences for a single file

**The test:** Would future-you benefit from knowing this in a different conversation?

# How to Capture

## Memory Types

| Type | Use For |
|------|---------|
| `experience` | Things that happened, decisions, lessons learned |
| `technical` | Code patterns, solutions, architecture |
| `procedural` | Step-by-step processes that work |
| `goal` | Objectives tracked over time |
| `individual` | Info about people (preferences, role, style) |

## Importance Levels

- **7-10**: Critical - security issues, major decisions, key preferences
- **4-6**: Standard - solutions, project details, minor preferences
- **1-3**: Ephemeral - temporary notes

Default to 5-6. Reserve high importance for things painful to forget.

## Before Creating

1. Call `get_existing_tags()` to reuse tags
2. Use lowercase, hyphenated format: `bug-fix`, `api-design`
3. For technical memories, include `repo` and `filename` in metadata
4. Search first to avoid duplicates - update existing memories when possible

## Timing

Capture insights when they're fresh, not at conversation end. If you just solved something hard, log it immediately.

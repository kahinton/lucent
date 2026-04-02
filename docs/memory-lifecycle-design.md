# Memory Lifecycle Design Document

**Status:** Draft — needs review  
**Author:** Lucent (documentation agent)  
**Date:** 2026-03-31  
**Roadmap Reference:** Post-Review Roadmap item #4 — "Continuous Existence Research → Design Doc"

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Design Principles](#2-design-principles)
3. [Memory Lifecycle Stages](#3-memory-lifecycle-stages)
4. [Consolidation Mechanism](#4-consolidation-mechanism)
5. [Active Forgetting](#5-active-forgetting)
6. [Reconsolidation](#6-reconsolidation)
7. [Implementation Plan](#7-implementation-plan)
8. [Risks and Open Questions](#8-risks-and-open-questions)

---

## 1. Problem Statement

Lucent's memory system currently has no mechanism for managing memories over time. Every memory created persists at full fidelity indefinitely. The only removal path is manual soft-delete. There is no consolidation, no automatic archival, and no forgetting.

**Today's numbers are small.** But the system is designed for continuous autonomous operation — a daemon running cognitive cycles every 15 minutes, creating experience memories, technical notes, and procedural knowledge around the clock. At projected rates:

| Timeframe | Estimated memories | Problem |
|-----------|-------------------|---------|
| 1 month | 100–500 | Manageable |
| 6 months | 1,000–3,000 | Search results degrade — too many low-value results dilute relevant ones |
| 1 year | 3,000–10,000 | Context loading becomes noisy — `search_memories()` returns stale knowledge alongside current |
| 2+ years | 10,000+ | Storage costs, query performance, and signal-to-noise all degrade significantly |

### Specific failure modes

1. **Search noise.** Fuzzy search (`pg_trgm` similarity) ranks results by text similarity, importance, and recency. It has no concept of whether a memory is stale, superseded, or redundant. A debugging session from 6 months ago about a since-refactored module ranks alongside current knowledge about the same module.

2. **Context pollution.** Agents call `search_memories()` at the start of every task to load relevant context. As memory count grows, these searches return more low-value results, consuming context window tokens and degrading agent reasoning quality.

3. **Redundancy accumulation.** The daemon creates experience memories for each cognitive cycle and work session. Over time, dozens of memories describe overlapping aspects of the same project, each with partial information, none synthesized into a coherent whole.

4. **No decay signal.** The system tracks `last_accessed_at` and has a full `memory_access_log`, but nothing acts on this data. A memory accessed 500 times and a memory never accessed once since creation are treated identically in search.

5. **No lifecycle awareness.** Memories don't know where they are in their lifecycle. There's no stage, no score, no signal indicating whether a memory is actively useful, ready for consolidation, or effectively dead.

### What exists today

The schema already has useful building blocks:

- **`last_accessed_at`** — Updated on every search hit or direct view (migration 006)
- **`memory_access_log`** — Full access history with timestamps, access type, and context (migration 006)
- **`deleted_at`** — Soft delete mechanism (migration 001)
- **`version` + audit snapshots** — Full version history with point-in-time restore (migration 012)
- **`importance`** — 1–10 rating set at creation, manually updatable
- **`AccessRepository.get_most_accessed()`** — Query for access frequency analytics
- **Daemon schedules** — `schedules` table supports recurring daemon tasks (migration 049)

What's missing: a system that ties these signals together into lifecycle decisions.

---

## 2. Design Principles

These principles are derived from biological memory research (synaptic consolidation, systems consolidation, active forgetting, reconsolidation) and adapted to Lucent's architecture.

### P1: Consolidation over deletion

Biological memory rarely destroys information outright. It transforms — compressing episodic details into semantic knowledge, merging overlapping experiences into generalized patterns. Lucent should prefer consolidation (merging and summarizing related memories) over deletion. Information that took effort to create should be synthesized, not thrown away.

### P2: Access patterns reveal value

In biological systems, memories strengthen through retrieval (long-term potentiation). A memory that is frequently accessed, recently accessed, and accessed across diverse contexts is valuable. A memory never accessed since creation probably isn't. `memory_access_log` already captures this signal — the lifecycle system should act on it.

### P3: Importance is a human judgment; decay is a system judgment

The `importance` field (1–10) represents a human or agent assessment at creation time. The lifecycle system should respect it — high-importance memories decay slower and require more evidence before archival. But importance alone shouldn't prevent lifecycle transitions. A memory rated importance-8 that hasn't been accessed in a year may need archival regardless.

### P4: Forgetting is a feature, not a failure

Active forgetting improves retrieval quality. Biological systems use synaptic pruning and interference to clear low-value memories, making remaining memories more accessible. Lucent should actively forget — but with safety rails that prevent losing valuable, rarely-accessed memories (e.g., incident postmortems).

### P5: Retrieval updates the memory (reconsolidation)

When a biological memory is retrieved, it becomes labile — temporarily destabilized and open to modification before being re-stored. In Lucent, accessing a memory should refresh its lifecycle position (preventing decay) and, when the access involves an update, strengthen the memory's consolidation state.

### P6: Sleep-like offline processing

Biological consolidation happens during sleep — offline periods where the brain replays and reorganizes experiences. Lucent's daemon cognitive cycle serves this role. Consolidation should happen during daemon schedules, not inline during user-facing operations. Searches and reads should be fast and lifecycle-unaware; the daemon handles lifecycle transitions in background.

### P7: Gradual, reversible transitions

No memory should jump from active to deleted. Transitions move through stages — active → consolidating → archived → forgotten — with each stage providing opportunity for intervention. Consolidated memories retain links to their source material. Archived memories can be recalled. Even "forgotten" memories are soft-deleted with a grace period before permanent removal.

### P8: Type-aware lifecycle rules

Not all memory types decay equally. Individual memories (contact info) should never auto-decay. Goal memories with status "active" should never be archived. Procedural memories (how-to guides) have longer useful lives than experience memories (session logs). The lifecycle system must encode type-specific rules.

---

## 3. Memory Lifecycle Stages

Every memory occupies exactly one lifecycle stage at any time. Stage transitions are determined by a **vitality score** computed from access patterns, age, importance, and type-specific rules.

### Stage Definitions

```
┌──────────┐    score drops     ┌───────────────┐   score drops    ┌──────────┐   grace period   ┌───────────┐
│  ACTIVE  │ ────────────────→  │ CONSOLIDATING │ ───────────────→ │ ARCHIVED │ ──────────────→  │ FORGOTTEN │
│          │                    │               │                  │          │                  │           │
│ full     │    ←────────────── │ merge/        │  ←─────────────  │ excluded │                  │ soft-     │
│ fidelity │    score recovers  │ summarize     │  score recovers  │ from     │                  │ deleted   │
└──────────┘   (reconsolidation)└───────────────┘ (reconsolidation)│ default  │                  │ pending   │
                                                                   │ search   │                  │ hard      │
                                                                   └──────────┘                  │ delete    │
                                                                                                 └───────────┘
```

#### ACTIVE

The default state for all newly created or recently accessed memories.

- **Search behavior:** Included in all searches (current behavior, no change).
- **Transition out:** Vitality score drops below the **consolidation threshold** (see §3.1).
- **Transition in:** Created, or reconsolidated from Consolidating/Archived via access.

#### CONSOLIDATING

Memories whose vitality has declined enough to warrant review for merging or summarization. They remain searchable but are candidates for the consolidation daemon task.

- **Search behavior:** Included in default searches, but ranked lower (vitality score acts as a tiebreaker after similarity).
- **What happens here:** The consolidation daemon identifies related Consolidating memories, merges overlapping content into summary memories, and archives the originals. See §4 for the full mechanism.
- **Transition out → Archived:** Memory is consolidated (merged into a summary) or vitality drops further.
- **Transition out → Active:** Memory is accessed, triggering reconsolidation (§6).

#### ARCHIVED

Memories that have been consolidated or have decayed to low vitality. They exist for reference but are excluded from default searches.

- **Search behavior:** Excluded from default searches. Accessible via explicit `include_archived=true` parameter or direct `get_memory()` by ID.
- **Transition out → Forgotten:** Remains archived with no access for the configured grace period (default: 180 days).
- **Transition out → Active:** Direct access triggers reconsolidation, promoting back to Active.

#### FORGOTTEN

Terminal pre-deletion state. Memories here are soft-deleted and pending permanent removal.

- **Search behavior:** Not searchable. Only accessible via admin tools or audit log.
- **What happens:** `deleted_at` is set. After the hard-delete grace period (default: 90 days), a cleanup task permanently removes the record.
- **Transition out:** Manual recovery only (restore from audit snapshot via `restore_memory_version`).

### 3.1 Vitality Score

The vitality score is a composite metric that determines a memory's lifecycle stage. It is computed periodically by the lifecycle daemon, not on every access.

```
vitality = (
    w_recency  × recency_score +
    w_frequency × frequency_score +
    w_importance × importance_score +
    w_type × type_bonus
)
```

#### Component Scores

**Recency score** (0.0–1.0): How recently the memory was accessed.

```
recency_score = exp(-λ × days_since_last_access)
```

Where `λ` is a decay constant (default: 0.03, giving a half-life of ~23 days). If `last_accessed_at` is NULL, uses `created_at` instead.

**Frequency score** (0.0–1.0): How often the memory is accessed, normalized.

```
frequency_score = min(1.0, access_count_last_90_days / frequency_baseline)
```

Where `frequency_baseline` is the 75th-percentile access count across all active memories (computed once per lifecycle run). This normalizes against the organization's overall access patterns.

**Importance score** (0.0–1.0): Direct mapping from the `importance` field.

```
importance_score = importance / 10.0
```

**Type bonus** (0.0–0.3): Type-specific longevity adjustment.

| Memory Type | Type Bonus | Rationale |
|-------------|-----------|-----------|
| `individual` | 0.3 | Contact info rarely accessed but always valuable |
| `procedural` | 0.2 | How-to knowledge has long shelf life |
| `technical` | 0.15 | Technical knowledge ages but remains reference-worthy |
| `goal` | 0.1 (active), 0.0 (completed/abandoned) | Active goals must stay active; completed ones can consolidate |
| `experience` | 0.0 | Session logs and insights age fastest |

#### Default Weights

| Weight | Default | Purpose |
|--------|---------|---------|
| `w_recency` | 0.35 | Most important signal — recent access = valuable |
| `w_frequency` | 0.25 | Frequently accessed memories are load-bearing |
| `w_importance` | 0.25 | Human/agent judgment has weight |
| `w_type` | 0.15 | Structural bias toward long-lived types |

#### Threshold Defaults

| Threshold | Default Value | Meaning |
|-----------|--------------|---------|
| Consolidation threshold | 0.35 | Below this: Active → Consolidating |
| Archive threshold | 0.15 | Below this: Consolidating → Archived |
| Forget threshold | 0.05 | Below this AND archived > 180 days: Archived → Forgotten |

These thresholds are configurable per organization. The lifecycle daemon reads them from organization settings (with system defaults as fallback).

### 3.2 Exemption Rules

Certain memories are exempt from lifecycle transitions regardless of vitality score:

| Exemption | Rule | Rationale |
|-----------|------|-----------|
| Individual memories | Never auto-transition beyond Active | Contact info is always needed |
| Active goals | Never transition below Active | Work in progress must stay visible |
| Pinned memories | Tag `pinned` prevents transitions | Manual override for critical references |
| Recently created | Created < 30 days ago: exempt from consolidation | Give new memories time to prove value |
| High importance + low age | importance ≥ 8 AND created < 180 days: exempt | Trust high-importance ratings for 6 months |

---

## 4. Consolidation Mechanism

Consolidation is the process of merging related, aging memories into denser summary memories. This is the core value proposition — reducing volume while preserving knowledge.

### 4.1 Identifying Consolidation Candidates

The consolidation daemon runs on a schedule (default: daily). It identifies candidates in two ways:

**Vitality-based candidates:** Memories whose lifecycle stage has transitioned to Consolidating.

```sql
SELECT id, content, tags, type, importance, metadata, created_at, last_accessed_at
FROM memories
WHERE lifecycle_stage = 'consolidating'
  AND deleted_at IS NULL
ORDER BY created_at ASC
LIMIT 100;
```

**Cluster detection:** Among Consolidating memories, identify groups that should be merged together.

Clustering criteria:
1. **Tag overlap** — Memories sharing ≥ 2 non-generic tags (excluding `daemon`, `validated`, etc.) are likely related.
2. **Type match** — Only merge memories of the same type (don't merge an experience with a procedural).
3. **Temporal proximity** — Memories created within the same time window (default: 30 days) about the same topic are consolidation candidates.
4. **Metadata similarity** — For technical memories: same `repo` or `filename`. For experience memories: overlapping `related_entities`.
5. **Explicit links** — Memories in each other's `related_memory_ids` arrays.

### 4.2 Consolidation Process

For each identified cluster of related memories:

```
Step 1: LOAD cluster
  - Fetch full content of all memories in the cluster
  - Load their access histories and version histories

Step 2: EXTRACT key facts
  - Agent identifies: core facts, decisions, outcomes, lessons
  - Distinguishes between: still-relevant knowledge vs. transient details
  - Marks any contradictions between memories (newer takes precedence)

Step 3: SYNTHESIZE summary memory
  - Create ONE new memory of the same type as the cluster
  - Content: Synthesized narrative preserving all key facts
  - Tags: Union of all source memory tags
  - Importance: Maximum importance from the cluster
  - Metadata: Merged (most recent values take precedence)
  - Related memory IDs: Union of all source related IDs
  - New tag: 'consolidated'
  - New metadata field: consolidated_from: [source_memory_ids]
  - New metadata field: consolidated_at: timestamp

Step 4: ARCHIVE sources
  - Transition source memories to Archived stage
  - Add tag: 'consolidated-source'
  - Add metadata: consolidated_into: summary_memory_id
  - DO NOT delete source memories — they remain accessible by ID

Step 5: VERIFY
  - Agent reviews the summary: does it contain all key facts from sources?
  - If verification fails, abort and leave sources in Consolidating stage
  - Log task event with verification result

Step 6: LOG
  - Create audit log entries for all transitions
  - Link summary memory to the consolidation task via link_task_memory
```

### 4.3 Consolidation Rules by Type

| Memory Type | Consolidation Strategy | Maximum Cluster Size |
|-------------|----------------------|---------------------|
| `experience` | Merge session logs about the same project/feature into a single narrative. Preserve: key decisions, root causes, outcomes. Drop: step-by-step debugging trails, intermediate states. | 10 |
| `technical` | Merge per-file or per-module notes into comprehensive module documentation. Preserve: architecture decisions, API contracts, gotchas. Drop: version-specific quirks for old versions. | 8 |
| `procedural` | Merge overlapping how-to memories into a single canonical procedure. Preserve: all steps, prerequisites, pitfalls. Reconcile conflicting steps (newer wins). | 5 |
| `goal` | Completed/abandoned goals: merge progress notes into a summary experience memory. Active goals: never consolidate. | 3 |
| `individual` | Never consolidated. Updated in place. | N/A |

### 4.4 Consolidation Depth (Hierarchical)

Consolidation can happen at multiple levels:

**Level 1 — Session consolidation** (daily): Merge individual session/cycle memories into weekly summaries. Target: experience memories from daemon cycles and work sessions.

**Level 2 — Topic consolidation** (weekly): Merge weekly summaries and standalone memories about the same topic into comprehensive topic memories. Target: technical and experience memories clustered by tags/metadata.

**Level 3 — Domain consolidation** (monthly): Merge topic memories into domain knowledge. Target: technical memories for the same repo/module, experience memories for the same project.

Each level produces a summary that is itself subject to lifecycle scoring. A Level 2 summary that's never accessed will eventually decay to Level 3 consolidation or archival.

---

## 5. Active Forgetting

Active forgetting is the deliberate removal of memories that provide no ongoing value. It improves retrieval quality by reducing the haystack.

### 5.1 Decay Criteria

A memory becomes a forgetting candidate when ALL of the following are true:

| Criterion | Threshold | Rationale |
|-----------|-----------|-----------|
| Vitality score | < 0.05 | Extremely low composite value |
| Lifecycle stage | Archived | Must have already passed through consolidation opportunity |
| Time in Archived | > 180 days | 6-month grace period after archival |
| Importance | ≤ 3 | Only forget low-importance memories automatically |
| Not exempted | No `pinned` tag, not `individual` type, not active goal | Safety rails |

### 5.2 Forgetting Process

```
Step 1: IDENTIFY candidates
  - Query: archived memories meeting all decay criteria
  - Batch size: up to 50 per forgetting cycle

Step 2: SAFETY CHECK (per memory)
  - Is this the ONLY memory about its topic? (Check tag overlap with remaining memories)
    → If yes: flag for human review instead of auto-forgetting
  - Is this referenced by other active memories? (Check related_memory_ids)
    → If yes: remove reference first, then re-evaluate
  - Was this ever high-importance (check audit log for historical importance values)?
    → If yes: flag for human review

Step 3: SOFT DELETE
  - Set deleted_at = NOW()
  - Set lifecycle_stage = 'forgotten'
  - Add tag: 'auto-forgotten'
  - Add metadata: forgotten_reason: "Vitality decay — {vitality_score}, archived since {date}"
  - Create audit log entry with action_type = 'delete', notes = forgotten_reason

Step 4: HARD DELETE (deferred)
  - A separate cleanup schedule runs monthly
  - Permanently removes memories where:
    - deleted_at IS NOT NULL
    - deleted_at < NOW() - INTERVAL '90 days'
    - Tag 'auto-forgotten' present (don't hard-delete manually-deleted memories without this tag)
  - Before hard delete: ensure audit snapshot exists (for recovery if needed)
```

### 5.3 Safety Rails

These protections prevent the system from losing valuable knowledge:

1. **Last-of-its-kind protection.** If forgetting a memory would leave no remaining memories covering its topic (determined by tag overlap analysis), the memory is flagged for human review instead of auto-forgotten.

2. **Importance floor.** Memories with `importance ≥ 4` are never auto-forgotten. They can reach Archived stage but require human decision to proceed to Forgotten.

3. **Audit trail preservation.** Even after hard delete, the `memory_audit_log` retains the creation and update history. The full snapshot from the last version is preserved in the audit log's `snapshot` JSONB column indefinitely.

4. **Consolidated-source protection.** Memories tagged `consolidated-source` (archived because they were merged into a summary) are never auto-forgotten while their summary memory exists. If the summary is deleted, the sources become forgetting candidates again.

5. **Grace periods.** 180 days in Archived before forgetting eligibility. 90 days in Forgotten (soft-deleted) before hard delete. Total minimum lifespan from archival to permanent deletion: 270 days.

6. **Human-override tags.** `pinned` tag permanently exempts a memory from all lifecycle transitions. `preserve` tag exempts from forgetting specifically (still subject to consolidation and archival).

---

## 6. Reconsolidation

In neuroscience, reconsolidation is the process where retrieving a memory temporarily destabilizes it, allowing modification before it is re-stored — often stronger than before. This mechanism explains why memories change over time and how retrieval itself is a form of learning.

Lucent's reconsolidation provides two benefits:
1. **Lifecycle refresh** — accessing a memory prevents premature decay.
2. **Content strengthening** — updating a memory during access improves its quality.

### 6.1 Lifecycle Refresh on Access

When a memory is accessed (via `get_memory`, `get_accessible`, or returned in search results), the system already updates `last_accessed_at`. This naturally increases the recency component of the vitality score on the next lifecycle evaluation.

**Stage promotion rules on access:**

| Current Stage | Access Type | Result |
|---------------|------------|--------|
| Active | Any | No stage change. `last_accessed_at` updated. |
| Consolidating | Search result | No stage change. But vitality score recalculated at next lifecycle run — may promote back to Active. |
| Consolidating | Direct view (`get_memory`) | Promote to Active immediately. Direct access is a strong intent signal. |
| Archived | Search result | Not reachable (Archived memories excluded from default search). |
| Archived | Direct view (`get_memory`) | Promote to Active immediately. Log reconsolidation event. |
| Forgotten (soft-deleted) | Direct view | Not reachable via normal search. Manual restore via `restore_memory_version` promotes to Active. |

### 6.2 Content Strengthening on Update

When a memory is both accessed AND updated in the same interaction (common pattern: agent searches for context, finds a memory, updates it with new information), this is the strongest lifecycle signal. The memory should be treated as actively maintained.

**On update:**
- `last_accessed_at` = NOW()
- `version` incremented
- `lifecycle_stage` set to Active (regardless of previous stage)
- Vitality score floor set to 0.5 for next lifecycle evaluation (prevents immediate re-decay)
- Audit log records the update with full snapshot

### 6.3 Reconsolidation of Consolidated Memories

When a summary memory (tagged `consolidated`) is accessed and found to be stale or incomplete, agents can trigger re-consolidation:

1. Agent accesses summary memory, determines it needs updating.
2. Agent checks `consolidated_from` metadata to find source memories.
3. If sources still exist (in Archived stage), agent can pull them back for re-synthesis.
4. Agent creates updated summary, archives old summary.
5. Sources that contributed to the new summary get fresh `consolidated_into` references.

This mirrors biological reconsolidation: the act of retrieval opens the memory for modification, potentially making it more accurate or comprehensive than the original consolidation produced.

---

## 7. Implementation Plan

### 7.1 Database Schema Changes

#### Migration 051: Add lifecycle columns

```sql
-- Add lifecycle_stage column to memories table
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS lifecycle_stage TEXT NOT NULL DEFAULT 'active'
CHECK (lifecycle_stage IN ('active', 'consolidating', 'archived', 'forgotten'));

-- Add vitality_score for caching the computed score
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS vitality_score REAL;

-- Add vitality_computed_at to track when score was last calculated
ALTER TABLE memories
ADD COLUMN IF NOT EXISTS vitality_computed_at TIMESTAMP WITH TIME ZONE;

-- Index for lifecycle-aware queries
CREATE INDEX idx_memories_lifecycle_stage
ON memories (lifecycle_stage, vitality_score DESC)
WHERE deleted_at IS NULL;

-- Index for consolidation daemon: find consolidating memories grouped by type
CREATE INDEX idx_memories_consolidation_candidates
ON memories (type, created_at ASC)
WHERE lifecycle_stage = 'consolidating' AND deleted_at IS NULL;

-- Index for forgetting daemon: find archived memories past grace period
CREATE INDEX idx_memories_forget_candidates
ON memories (lifecycle_stage, updated_at ASC)
WHERE lifecycle_stage = 'archived' AND deleted_at IS NULL;

-- Backfill: all existing memories start as Active
-- (vitality_score and vitality_computed_at remain NULL until first lifecycle run)
```

#### Migration 052: Add consolidation metadata support

```sql
-- No schema change needed — consolidation metadata (consolidated_from,
-- consolidated_into, consolidated_at) stored in existing JSONB metadata column.
-- This migration adds a GIN index path for consolidation lookups.

CREATE INDEX idx_memories_consolidated_from
ON memories ((metadata->'consolidated_from'))
WHERE metadata ? 'consolidated_from' AND deleted_at IS NULL;
```

### 7.2 Search Changes

Modify `MemoryRepository.search()` to respect lifecycle stages:

```python
# Default behavior: exclude archived and forgotten
async def search(
    self,
    ...,
    include_archived: bool = False,  # NEW PARAMETER
    lifecycle_stages: list[str] | None = None,  # NEW PARAMETER
) -> dict[str, Any]:
    # Default: search Active + Consolidating only
    if lifecycle_stages is None and not include_archived:
        stages = ['active', 'consolidating']
    elif include_archived:
        stages = ['active', 'consolidating', 'archived']
    else:
        stages = lifecycle_stages

    # Add to WHERE clause:
    # AND lifecycle_stage = ANY($stages)
```

**Search ranking adjustment:** When both Active and Consolidating memories appear in results, apply a multiplier:

```python
# Adjust similarity score by lifecycle stage
if lifecycle_stage == 'active':
    adjusted_score = sim_score * 1.0
elif lifecycle_stage == 'consolidating':
    adjusted_score = sim_score * 0.85  # Slight penalty
elif lifecycle_stage == 'archived':
    adjusted_score = sim_score * 0.6   # Significant penalty (only when explicitly included)
```

### 7.3 MCP Tool Changes

**Modified tools:**

| Tool | Change |
|------|--------|
| `search_memories` | Add optional `include_archived` parameter (default: false) |
| `search_memories_full` | Add optional `include_archived` parameter (default: false) |
| `get_memory` | No change — direct access always works, triggers reconsolidation |
| `get_memories` | No change — batch access always works |
| `update_memory` | Set `lifecycle_stage = 'active'` on any update |

**New tools:**

| Tool | Purpose |
|------|---------|
| `get_memory_stats` | Return lifecycle stage distribution, vitality score histogram, consolidation activity summary |
| `pin_memory` | Add `pinned` tag to exempt memory from lifecycle transitions |
| `unpin_memory` | Remove `pinned` tag |

### 7.4 Lifecycle Daemon Tasks

#### Schedule 1: Vitality Score Computation

- **Frequency:** Every 6 hours
- **Agent type:** memory
- **Task:** Compute vitality scores for all active non-deleted memories.

```
Algorithm:
1. Query all memories WHERE deleted_at IS NULL AND lifecycle_stage != 'forgotten'
2. For each memory, compute:
   a. recency_score from last_accessed_at (or created_at if NULL)
   b. frequency_score from COUNT(*) in memory_access_log last 90 days
   c. importance_score from importance column
   d. type_bonus from type column
3. vitality = weighted sum per formula in §3.1
4. UPDATE memories SET vitality_score = ?, vitality_computed_at = NOW()
5. Apply stage transitions based on thresholds (§3.1)
6. Log transitions as task events
```

**Performance note:** For large memory sets, process in batches of 500. The frequency_score query can use a single aggregate query:

```sql
SELECT memory_id, COUNT(*) as access_count
FROM memory_access_log
WHERE accessed_at > NOW() - INTERVAL '90 days'
GROUP BY memory_id;
```

This returns the full frequency map in one query, then joined in-application with the memory batch.

#### Schedule 2: Consolidation

- **Frequency:** Daily
- **Agent type:** memory (with LLM for content synthesis)
- **Model:** Standard model (claude-sonnet-4 or equivalent) — consolidation requires good synthesis quality.
- **Task:** Identify and merge related Consolidating memories.

```
Algorithm:
1. Fetch consolidation candidates (lifecycle_stage = 'consolidating')
2. Cluster by: type + tag overlap + metadata similarity (§4.1)
3. For each cluster of ≥ 2 memories:
   a. Load full content
   b. LLM synthesizes summary (§4.2, Steps 2-3)
   c. Create summary memory
   d. Archive source memories
   e. Verify summary quality
4. Log all actions
```

#### Schedule 3: Forgetting

- **Frequency:** Weekly
- **Agent type:** memory
- **Task:** Identify and soft-delete memories meeting forgetting criteria.

```
Algorithm:
1. Query forget candidates (§5.1 criteria)
2. Apply safety checks (§5.2)
3. Soft-delete qualifying memories
4. Flag edge cases for human review
```

#### Schedule 4: Hard Delete Cleanup

- **Frequency:** Monthly
- **Agent type:** memory (no LLM needed — pure database operation)
- **Task:** Permanently remove long-deleted memories.

```
Algorithm:
1. Query: deleted_at < NOW() - INTERVAL '90 days' AND 'auto-forgotten' = ANY(tags)
2. Verify audit snapshot exists for each
3. Hard delete from memories table
4. Log audit entry with action_type = 'hard_delete'
```

### 7.5 Repository Layer Changes

Add to `MemoryRepository`:

```python
async def compute_vitality_scores(self, batch_size: int = 500) -> dict:
    """Compute and update vitality scores for all active memories."""
    ...

async def transition_lifecycle_stage(
    self,
    memory_id: UUID,
    new_stage: str,
    reason: str,
) -> dict | None:
    """Transition a memory to a new lifecycle stage with audit logging."""
    ...

async def get_consolidation_candidates(
    self,
    type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Fetch memories in 'consolidating' stage, grouped by similarity."""
    ...

async def get_forget_candidates(
    self,
    limit: int = 50,
) -> list[dict]:
    """Fetch memories meeting all forgetting criteria."""
    ...

async def get_lifecycle_stats(
    self,
    organization_id: UUID | None = None,
) -> dict:
    """Return memory counts by lifecycle stage and type."""
    ...
```

### 7.6 Migration Path for Existing Memories

All existing memories start in the **Active** stage with `vitality_score = NULL`.

**First lifecycle run after deployment:**

1. Compute vitality scores for all existing memories.
2. Apply stage transitions based on scores.
3. **Special handling for first run:** Use a more lenient consolidation threshold (0.25 instead of 0.35) for the first run to avoid immediately transitioning a large percentage of memories. Gradually tighten over subsequent runs across 2 weeks.
4. Log summary: "Initial lifecycle assessment: X active, Y consolidating, Z archived."

**Expected first-run results** (estimated for a system with ~200 memories):
- Memories accessed in last 30 days → remain Active
- Memories not accessed in 30–90 days with moderate importance → Consolidating
- Memories not accessed in 90+ days with low importance → Consolidating (not yet Archived, due to lenient first-run threshold)
- No memories immediately Archived or Forgotten on first run

### 7.7 Configuration

Lifecycle parameters stored in organization settings (with system defaults):

```json
{
  "memory_lifecycle": {
    "enabled": true,
    "weights": {
      "recency": 0.35,
      "frequency": 0.25,
      "importance": 0.25,
      "type": 0.15
    },
    "thresholds": {
      "consolidation": 0.35,
      "archive": 0.15,
      "forget": 0.05
    },
    "recency_half_life_days": 23,
    "frequency_window_days": 90,
    "archive_grace_period_days": 180,
    "forget_grace_period_days": 90,
    "first_run_consolidation_threshold": 0.25,
    "auto_forget_max_importance": 3
  }
}
```

---

## 8. Risks and Open Questions

### Risks

**R1: Consolidation quality depends on LLM synthesis.**  
If the summarization agent produces lossy or inaccurate summaries, consolidation destroys information instead of compressing it. **Mitigation:** Source memories are archived, not deleted. A bad summary can be detected and sources restored. Include a verification step (§4.2 Step 5) where the agent checks its own summary against sources.

**R2: Over-aggressive decay for organizations with bursty access patterns.**  
An organization that works intensely on a project for 2 months, then pivots for 3 months, may see project memories decay before they return to the project. **Mitigation:** The 180-day archive grace period and importance floor (≥4 blocks auto-forgetting) provide substantial buffer. The `pinned` tag provides manual override. Consider adding a "project" grouping concept in the future.

**R3: Vitality computation cost at scale.**  
Computing vitality scores requires joining the `memories` table with aggregate access counts from `memory_access_log`. At 10,000+ memories with 100,000+ access log entries, this could be expensive. **Mitigation:** Batch processing (500 at a time), access count materialization (pre-aggregate into a column or materialized view), and the 6-hour schedule provides ample time.

**R4: Consolidation agent dispatch requires working daemon permissions.**  
The consolidation process requires the daemon to dispatch a memory agent with LLM access for synthesis. This depends on the daemon permission model being reliable (Roadmap item #1). **Mitigation:** Item #1 is marked as higher priority. Consolidation scheduling can be deployed but disabled until permissions are stable.

**R5: Gaming vitality scores.**  
An overly eager agent that searches broadly could inflate access counts, keeping low-value memories artificially alive. **Mitigation:** The frequency score normalizes against the 75th percentile — if everything is accessed more, the baseline rises too. Consider weighting direct views higher than search-result appearances.

### Open Questions

**Q1: Should consolidated summaries inherit the vitality score of their source cluster, or start fresh?**  
Starting fresh means a summary could immediately decay if not accessed. Inheriting means it starts with the (low) vitality of its decayed sources. Proposed: Start summaries with a vitality floor of 0.5 for their first 30 days, giving them time to prove useful.

**Q2: Should the lifecycle system track access by different users differently?**  
A memory accessed by 5 different users may be more valuable than one accessed 5 times by the same user. The `memory_access_log` tracks `user_id`, so this is feasible. Proposed: Defer to v2. Use raw access count for v1 simplicity.

**Q3: How should lifecycle interact with shared memories?**  
If a memory is shared across an organization, one user's access patterns shouldn't control the lifecycle for all users. Proposed: For shared memories, compute vitality using org-wide access patterns (all users' access counts and recency). For private memories, use only the owner's access patterns.

**Q4: Should there be a maximum number of memories per lifecycle stage?**  
If consolidation is too slow and memories accumulate in the Consolidating stage, the system degrades. Proposed: Add a monitoring metric. If Consolidating count exceeds 2× Active count, increase consolidation frequency or batch size.

**Q5: What's the right frequency for vitality computation?**  
Every 6 hours is proposed. Too frequent wastes compute; too infrequent means stage transitions lag behind actual value changes. This should be tunable and may need adjustment based on observed patterns.

**Q6: Should the system support "memory importance decay"?**  
Currently, importance is static (set at creation, manually changeable). Should the lifecycle system automatically reduce importance over time for memories that aren't accessed? Proposed: No for v1. Importance is a human judgment and auto-modifying it conflates two signals. Vitality score already captures the decay signal without altering the original importance.

**Q7: How to handle cross-organization memory lifecycle?**  
Shared memories visible to multiple organizations could have different value to each. Proposed: Defer. Current system is single-organization. Revisit when multi-org is a real use case.

---

## Appendix A: Biological Foundations

This design draws from established neuroscience research on memory consolidation:

| Biological Concept | Lucent Analog | Section |
|-------------------|---------------|---------|
| **Synaptic consolidation** (minutes–hours: short-term → long-term via protein synthesis) | Memory creation with importance rating and initial Active stage | §3 |
| **Systems consolidation** (weeks–months: hippocampus → neocortex) | Stage transitions from Active → Consolidating → Archived | §3, §4 |
| **Long-term potentiation** (repeated activation strengthens synapses) | Access frequency increasing vitality score | §3.1 |
| **Active forgetting** (synaptic pruning, intrinsic forgetting via Rac1/Cdc42) | Decay criteria and soft/hard delete process | §5 |
| **Reconsolidation** (retrieval destabilizes, allows modification) | Access-triggered stage promotion and update strengthening | §6 |
| **Sleep replay** (offline consolidation during sleep) | Daemon scheduled consolidation tasks | §7.4 |
| **Spacing effect** (distributed retrieval strengthens more than massed) | Frequency score over 90-day window rewards spread-out access | §3.1 |
| **Semantic memory formation** (episodic details compressed into general knowledge) | Consolidation merging session logs into topic summaries | §4.3, §4.4 |

## Appendix B: Example Lifecycle Walkthrough

**Scenario:** A daemon debugging session produces 5 experience memories over a week of troubleshooting a permission bug.

| Day | Event | Memory State |
|-----|-------|-------------|
| Day 1 | Daemon creates memory: "Permission denied error in MCP session" | Active, importance 5, vitality ~0.8 |
| Day 3 | Agent searches for "MCP permission", memory returned in results | Active, last_accessed_at refreshed |
| Day 5 | Daemon creates 4 more debugging memories about same issue | 5 Active memories about MCP permissions |
| Day 7 | Bug fixed. Agent creates summary memory, updates all 5 with outcome | All 5 Active, importance updated to 6 |
| Day 45 | No access in 38 days. Vitality computed: 0.32 | All 5 transition to Consolidating |
| Day 46 | Consolidation daemon detects 5 related memories (tag overlap: `daemon`, `mcp`, `permissions`) | Cluster identified |
| Day 46 | Consolidation agent synthesizes into 1 memory: "MCP Permission Bug — Root Cause and Fix" | 1 Active summary created, 5 sources → Archived |
| Day 120 | Summary accessed during related investigation | Summary stays Active (reconsolidation) |
| Day 250 | Sources archived for 204 days, vitality < 0.05, importance 6 | Sources NOT auto-forgotten (importance ≥ 4). Stay Archived. |
| Day 400 | Summary still occasionally accessed | Summary stays Active. Sources remain Archived indefinitely (importance floor protection). |

---

*This document is ready for review. It maps biological memory consolidation principles to concrete implementation against Lucent's existing PostgreSQL-based memory system, daemon architecture, and MCP tool layer.*

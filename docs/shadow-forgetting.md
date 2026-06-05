# Shadow-Mode Forgetting (M4 Prototype)

> **Status:** Shadow-only. No memory is ever deleted, demoted, or re-ranked by
> this subsystem. All output is observability data written to a sidecar table.
>
> **Reference:** Design memo `640b13a4-c9f6-4175-8770-715a9641f8c5`
> (goal `6ab0951e-3622-49b2-9b74-7b8cfa7b0d03`).

This note is for operators evaluating the native-forgetting prototype. It
describes the feature flag, the sidecar table, the metrics emitted, and the
soak period that must elapse before any decision is made to act on shadow
scores.

---

## The flag: `LUCENT_SHADOW_FORGET_ENABLED`

| Property | Value |
|---|---|
| Variable | `LUCENT_SHADOW_FORGET_ENABLED` |
| Default | **OFF** (`false`) |
| Read by | `lucent.settings.shadow_forget_enabled()` |
| Accepted truthy values | `1`, `true`, `yes`, `on` (case-insensitive) |

### What it enables (when ON)

- **Candidate C — LDR observation hooks.** At every memory-delete site
  (`MemoryRepository.delete` and the `delete_memory` MCP tool), the system
  writes one observation row to `memory_shadow_scores` with
  `strategy='ldr-obs-v1'` and `shadow_action='would_demote'`. The signals
  payload records the source memory id, any canonical replacement id derived
  from metadata, the count of incoming related-memory edges that would be
  broken, and whether the delete is a compliance/forced delete.
- **Candidate A — GCP shadow scoring job.** A scheduled job
  (`Shadow Forget Scoring`) runs at the same cadence as the existing vitality
  job, offset by `LUCENT_SHADOW_FORGET_OFFSET_MINUTES` (default `+15`
  minutes). It computes a graph-centrality protection score per memory and
  writes one row per scored memory to `memory_shadow_scores` with
  `strategy='gcp-v1'` and a divergence tag comparing the shadow decision
  against the current vitality lifecycle stage. The schedule short-circuits
  with `schedule.skipped` when the feature flag is off or every eligible memory
  already has a fresh `gcp-v1` sidecar score.
- **MCP tools** `compute_shadow_forget_scores` and
  `get_shadow_forget_comparison` become functional (they no-op when the flag
  is off).

### What it does NOT do

- It does **not** modify `vitality_score`, `vitality_computed_at`, or
  `lifecycle_stage` on any memory.
- It does **not** change search ranking, retrieval order, or memory access
  control.
- It does **not** delete, demote, archive, consolidate, or tag any memory.
- It does **not** alter the behavior of `MemoryRepository.delete` — observation
  is fail-open; sidecar write failures never block a delete.
- It does **not** add columns to the `memories` table. All state lives in the
  `memory_shadow_scores` sidecar.

When the flag is **OFF**, the production read/write paths are byte-for-byte
unchanged. This is enforced by an integration test
(`tests/test_shadow_forget_candidate_a.py`) that snapshots vitality fields and
ranking output around a shadow run.

---

## Sidecar table: `memory_shadow_scores`

Created by migration `065_memory_shadow_scores.sql`. The table is append-only
in practice (rows are upserted by `(memory_id, strategy, computed_at)`).

| Column | Type | Purpose |
|---|---|---|
| `memory_id` | `UUID` | FK to `memories.id` (`ON DELETE CASCADE`). |
| `strategy` | `TEXT` | `gcp-v1` (Candidate A) or `ldr-obs-v1` (Candidate C). |
| `score` | `REAL` | Strategy-specific score; `NULL` for pure observations. |
| `shadow_action` | `TEXT` | What the strategy *would* do (`would_keep`, `would_archive`, `would_demote`, …). Advisory only. |
| `signals` | `JSONB` | Strategy inputs (edge counts, replacement ids, request links, etc.). |
| `computed_at` | `TIMESTAMPTZ` | Defaults to `now()`. |
| `divergence_tag` | `TEXT` | `agree`, `gcp-protects-vitality-archives`, `gcp-forgets-vitality-keeps`, … |

Indexes:

- `ix_msv_strategy_computed (strategy, computed_at DESC)` — recent scores per
  strategy.
- `ix_msv_divergence (strategy, divergence_tag) WHERE divergence_tag IS NOT NULL`
  — disagreement queries.

Two preflight indexes are also created on existing tables to keep the
graph-signal reads cheap:

- `idx_memories_related_memory_ids_gin` on `memories.related_memory_ids`.
- `idx_access_memory_user` on `memory_access_log (memory_id, user_id)`.

Rollback: `065_memory_shadow_scores.down.sql`.

---

## OTEL metrics

Five histograms are emitted from the shadow scoring job through the existing
OpenTelemetry pipeline (`src/lucent/metrics.py`). All are namespaced under
`lucent.shadow_forget.*` and carry the `strategy` attribute.

| Metric | What it measures |
|---|---|
| `lucent.shadow_forget.top_k_agreement` | Fraction of the top-K vitality-archive set that the shadow strategy also flags for archive — agreement on what to forget. |
| `lucent.shadow_forget.orphan_reclaim` | Count (or fraction) of memories the shadow strategy would reclaim that vitality currently keeps — purely additional forgetting capacity. |
| `lucent.shadow_forget.load_bearing_protection` | Count of memories the shadow strategy protects (high incoming edges, active request links, etc.) that vitality would otherwise archive — the cost of *not* having shadow. |
| `lucent.shadow_forget.ldr_edges_at_risk` | Sum of incoming related-memory edges on memories observed at delete time without a recorded canonical replacement — the queue of broken-edge risk under naive deletion. |
| `lucent.shadow_forget.compute_overhead` | Wall-clock seconds the shadow job spent per scoring batch. |

All five are recorded only when the flag is ON. None are alertable yet — they
exist to feed the soak-period evaluation, not to drive on-call.

---

## Soak expectation: ≥30 days before any action

Before *any* proposal to act on shadow scores (gate deletes on LDR
observations, allow GCP to drive consolidation, surface scores in the UI for
human review, etc.), the prototype must run with the flag ON in production
for **at least 30 continuous days**, producing all five metrics throughout.

The 30-day floor exists because:

- Memory access patterns are weekly-cyclic (work weeks, on-call rotations,
  release cadences). A single week of data will systematically under-count
  load-bearing memories that are touched only on certain days.
- Divergence tags need a stable base rate before `agree` vs.
  `gcp-protects-vitality-archives` vs. `gcp-forgets-vitality-keeps` ratios can
  be trusted.
- LDR edges-at-risk is only meaningful once enough natural deletes have
  occurred to characterize the steady-state breakage rate.

If the soak is interrupted (flag flipped off, schedule paused, scoring job
errors out for >24h), the clock restarts. Decisions to graduate any candidate
out of shadow mode require an explicit, written review of the 30-day metrics
window and sign-off recorded against the goal memory
(`6ab0951e-3622-49b2-9b74-7b8cfa7b0d03`).

---

## Operating checklist

- [ ] Confirm `LUCENT_SHADOW_FORGET_ENABLED` is unset or `false` in any
  environment that has not explicitly opted into the soak.
- [ ] Apply migration `065` and verify the sidecar table and indexes exist.
- [ ] When opting into the soak, record the start date in the goal memory and
   verify the `Shadow Forget Scoring` schedule is registered and runs at
   vitality cadence + offset. A `schedule.skipped` no-work run is not a failure,
   but repeated skips during a soak should be explainable by fresh sidecar
   scores or an intentionally empty memory set.
- [ ] Watch the five `lucent.shadow_forget.*` metrics; investigate any gap
  longer than 24h and reset the soak clock if one occurs.
- [ ] Do **not** wire the sidecar into delete or ranking paths for any reason
  before the soak completes and a decision is recorded.

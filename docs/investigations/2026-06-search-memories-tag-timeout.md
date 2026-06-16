# `search_memories(tags=["validated"])` MCP Timeout — Investigation

- **Date:** 2026-06-15
- **Reporter:** performance agent (daemon-dispatched)
- **Symptom:** MCP error `-32001: Request timed out` returned to memory- and
  reflection-agent sub-agents calling `memory-server/search_memories` with
  payload `{"limit": 10, "tags": ["validated"]}`. Audit log shows 6 occurrences
  between 2026-05-30 and 2026-06-11.
- **Affected callers:** memory-capture, memory-search, memory-management,
  learning-extraction, self-improvement skills running on memory and reflection
  agents.

## TL;DR

The DB query itself is **not** the bottleneck. The GIN index on `tags` exists
and is healthy; for the audited payload the planner correctly *ignores* it
(the `validated` tag covers ~80 % of accessible rows so it is no more
selective than the ACL predicate) and uses the ACL btrees instead. Even on a
synthetic 200 000-row dataset with 80 % `validated` coverage the warm-cache
query returns in **~23 ms**, and on the live `lucent` database (1 238 rows
total, 129 `validated`) it returns in **~1 ms**.

The timeout is therefore **not (a) missing/unused tag index, not (b) a bad
query shape, and not (c) ACL-join blow-up** at the data sizes Lucent runs
today. It is a **(d)-class issue**: the request is being held on the server
long enough that the *upstream* MCP client (Codex/Claude Code-style runner —
not the lucent-side MCP bridge) gives up at its built-in tool-call timeout
(typically 60 s) and surfaces `-32001`.

The two concrete server-side amplifiers we found, and recommend fixing:

1. `search_memories` blocks the response on **synchronous
   `log_batch_access`** — an INSERT-executemany + bulk UPDATE on `memories`
   that takes a write transaction on the *hot* table and runs *before* the
   tool returns to the caller. Under any concurrent write contention this
   becomes the long pole and consumes the 60 s budget.
2. The `search()` repo always issues a **second full COUNT(\*)** with the
   same predicates, even though `total_count` is rarely used by the calling
   skills. At 200 k rows the count alone is ~14 ms cold and grows linearly.

A third, lower-priority finding is that the lucent-side MCP bridge
(`src/lucent/llm/mcp_bridge.py`) opens `ClientSession` with no
`read_timeout_seconds`, so when the bridge itself is the caller there is
**no** SDK-level timeout — failures only surface via the upstream agent
runner. Setting an explicit timeout would normalize the error code instead
of relying on whichever runner happens to be in front.

The cheapest, highest-impact fix is **(1)** — make `log_batch_access`
fire-and-forget. **(2)** is a follow-up. **(3)** is hygiene. No new index is
required and no query rewrite is required.

---

## 1. Reproduction

### 1.1 SQL emitted

`src/lucent/tools/memories.py::search_memories` validates input, fetches
user/org context via `_get_current_user_context`, and calls
`MemoryAccessService.search(...)`, which in turn calls `MemoryRepo.search()`
in `src/lucent/db/memory.py`.

For the audited payload `{"limit": 10, "tags": ["validated"]}`, with no
`query`, no `type`, no `username`, no other filters, and a regular member
caller (`memory_scope=None`, non-admin), the emitted SQL is:

```sql
SELECT id, username, type, content, tags, importance, related_memory_ids,
       metadata, created_at, updated_at, version, lifecycle_stage,
       vitality_score, vitality_computed_at,
       NULL::float AS sim_score
FROM memories
WHERE deleted_at IS NULL
  AND (user_id = $1 OR (organization_id = $2 AND shared = true))
  AND (metadata IS NULL
       OR NOT (metadata ? 'repo')
       OR LOWER(metadata->>'repo') = ANY($3::text[]))
  AND tags @> $4               -- $4 = ARRAY['validated']::text[]
ORDER BY importance DESC, created_at DESC
LIMIT $5 OFFSET $6;            -- $5 = 10, $6 = 0
```

A second query with identical predicates is then executed for the count:

```sql
SELECT COUNT(*) FROM memories
WHERE deleted_at IS NULL
  AND (user_id = $1 OR (organization_id = $2 AND shared = true))
  AND (metadata IS NULL OR NOT (metadata ? 'repo')
       OR LOWER(metadata->>'repo') = ANY($3::text[]))
  AND tags @> $4;
```

The repo-ACL predicate (`accessible_repos`) is dropped when the caller is an
admin/owner or when `memory_scope == 'org_shared_only'`. For the failing
calls (memory-/reflection-agent in regular member context) it is present and
includes the user's GitHub repo allowlist.

After the search returns, *before* the tool serializes the response, the
tool calls `access_repo.log_batch_access(...)` synchronously
(`src/lucent/tools/memories.py:723`). That call does, **inside a single
transaction** on the same pool connection:

- `executemany` an `INSERT` into `memory_access_log` for each returned id, and
- a single `UPDATE memories SET last_accessed_at = NOW(), lifecycle_stage = ...
  WHERE id IN (...)` over the same ids
  (`src/lucent/db/access.py:73-128`).

The whole `log_batch_access` happens **before** the JSON response is
returned to the MCP client.

### 1.2 Index landscape on `memories.tags`

```
"idx_memories_tags"        gin (tags)                                 -- 001_init.sql
"idx_memories_tags_active" gin (tags) WHERE deleted_at IS NULL        -- later migration
```

Both GIN indexes exist on the live database (`docker exec lucent-db psql -U
lucent -d lucent -c '\d memories'`). The `tags` column is `text[]`, the
operator used (`@>`) is the canonical `gin__text_ops` containment operator,
and either index is eligible.

In addition, the ACL predicate has dedicated partial btrees:

```
"idx_memories_user_id_active"      btree (user_id) WHERE deleted_at IS NULL
"idx_memories_org_shared_active"   btree (organization_id) WHERE deleted_at IS NULL AND shared = true
"idx_memories_user_active"         btree (user_id, deleted_at) WHERE deleted_at IS NULL
```

So both branches of the `OR` are covered by their own partial btree.

### 1.3 EXPLAIN (ANALYZE, BUFFERS)

#### Live `lucent` DB — actual production-shape data (1 238 rows, 129 `validated`)

Sampled with the user_id/organization_id of one row that has the
`validated` tag:

```
docker exec -i lucent-db psql -U lucent -d lucent -c "
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, importance, created_at FROM memories
WHERE deleted_at IS NULL
  AND (user_id = '0eec0d17-c045-4e99-be36-d8adedc31e29'
       OR (organization_id = '0f9abaa4-7489-47ab-8d6c-7c5be8d69d51' AND shared = true))
  AND tags @> ARRAY['validated']::text[]
ORDER BY importance DESC, created_at DESC
LIMIT 10;"
```

```
 Limit  (cost=105.86..105.89 rows=10 width=28)
        (actual time=0.965..0.966 rows=10 loops=1)
   Buffers: shared hit=12 read=150 dirtied=21
   ->  Sort  (cost=105.86..105.95 rows=33 width=28)
             (actual time=0.964..0.964 rows=10 loops=1)
         Sort Key: importance DESC, created_at DESC
         Sort Method: top-N heapsort  Memory: 26kB
         ->  Index Scan using idx_memories_org_id on memories
                  (cost=0.27..105.15 rows=33 width=28)
                  (actual time=0.073..0.932 rows=30 loops=1)
               Filter: ((tags @> '{validated}'::text[])
                        AND ((user_id = '...'::uuid)
                             OR ((organization_id = '...'::uuid) AND shared)))
               Rows Removed by Filter: 301
 Planning Time: 1.911 ms
 Execution Time: 0.992 ms
```

**1 ms.** No GIN index on `tags` is used because at 1 238 rows the planner
has plenty of cheaper options; here it walks `idx_memories_org_id` and
filters in-line. This is correct and cheap.

#### Synthetic `perftest` DB — 200 000 rows, 80 % `validated`, 14 % `shared`

To stress the predicate I rebuilt the schema with the same indexes
(`idx_memories_tags`, `idx_memories_tags_active`,
`idx_memories_user_id_active`, `idx_memories_org_shared_active`,
`idx_memories_user_active`, `idx_memories_org_id`,
`idx_memories_created_at`, `idx_memories_active`) and seeded 200 k rows
with one main org of 196 k rows of which 28 k are `shared = true`, 80 % of
all rows tagged `validated`, and the calling user owning 1 000 of those
rows (`user_id = '00000000-0000-0000-0000-000000000005'`).

Main search query — warm cache:

```
 Limit  (cost=8393.15..8393.18 rows=10 width=206)
        (actual time=23.251..23.253 rows=10 loops=1)
   Buffers: shared hit=7070
   ->  Sort  (Sort Key: importance DESC, created_at DESC; top-N heapsort)
         ->  Bitmap Heap Scan on memories
                 (rows=23320 actual rows=23142, ~5715 removed by tag filter)
               Recheck Cond: ((user_id = $1 AND deleted_at IS NULL)
                              OR (organization_id = $2 AND deleted_at IS NULL AND shared))
               Filter: (tags @> '{validated}'::text[])
               Heap Blocks: exact=7033
               ->  BitmapOr
                     ->  Bitmap Index Scan on idx_memories_user_id_active     (rows=1000)
                     ->  Bitmap Index Scan on idx_memories_org_shared_active  (rows=28000)
 Execution Time: 23.373 ms
```

Same query, **cold cache** (after `pg_buffercache`-equivalent eviction by
running it from a fresh connection):

```
 Buffers: shared hit=12 read=7058
 Execution Time: 55.012 ms
```

Count query (cold cache equivalent shape):

```
 Aggregate  (actual time=14.340..14.341 rows=1 loops=1)
   Buffers: shared hit=7064
   ->  Bitmap Heap Scan on memories  (rows=23142, Filter rejected 5715)
         Heap Blocks: exact=7033
 Execution Time: 14.413 ms
```

A pure `WHERE tags @> ARRAY['validated']` (no ACL) for comparison:

```
 Limit  (cost=0.00..0.60 rows=10 width=16)
        (actual time=0.054..0.059 rows=10 loops=1)
   ->  Seq Scan on memories
         Filter: (deleted_at IS NULL AND tags @> '{validated}'::text[])
 Execution Time: 0.092 ms
```

Confirming that **GIN on `tags` is correctly skipped** when `validated`'s
selectivity is ~0.8 — a GIN scan that returns 160 k tids is strictly worse
than a seq/btree scan with a recheck.

### 1.4 Asyncpg pool / connection pressure

The pool acquires a single connection per `search()` call and releases it
right after the count + page fetch. `log_batch_access` then re-acquires a
connection for its own write transaction. Two pool acquisitions per MCP
request — fine in isolation, but each is a potential blocking point if the
pool is saturated by concurrent writers (audit-log writers, vitality job,
consolidation, etc.).

There is no per-query `statement_timeout` set on the lucent role
(`SHOW statement_timeout` returned `0` / unlimited). Postgres will not
self-cancel a slow `search`.

---

## 2. Root cause analysis

Mapping to the four hypotheses listed in the task:

| Hypothesis | Verdict |
|---|---|
| (a) missing/unused GIN index on `tags` | **No.** `idx_memories_tags` and `idx_memories_tags_active` both exist. The planner's choice to *not* use them is correct given `validated`'s ~0.8 selectivity. |
| (b) bad query shape (`@>` vs `?` etc.) | **No.** `tags @> $1` is the right operator for `text[]` containment and is GIN-eligible. The `text[]` schema and `@>` are not the bottleneck. |
| (c) ACL join blowing up the plan | **No.** `(user_id = $1 OR (organization_id = $2 AND shared))` resolves to a `BitmapOr` over two purpose-built partial btrees (`idx_memories_user_id_active`, `idx_memories_org_shared_active`). Heap-block reads scale with `|user_rows| + |org_shared_rows|`, both of which are well-controlled in production. |
| (d) MCP-side timeout too tight / server stall | **Yes — proximate cause.** The upstream agent runner's MCP client cancels at its own request timeout (~60 s by convention; matches the `-32001` code observed). For the lucent server to actually take ≥60 s on a query that benchmarks at 1 ms, *something else* on the request path has to stall it. |

The two server-side stalls we identified, in priority order:

### 2A. Synchronous `log_batch_access` on the response path *(primary fix)*

`src/lucent/tools/memories.py:721-737`:

```python
if result["memories"]:
    try:
        access_repo = await _get_access_repository()
        memory_ids_accessed = [m["id"] for m in result["memories"]]
        await access_repo.log_batch_access(
            memory_ids=memory_ids_accessed,
            access_type="search_result",
            user_id=user_id,
            organization_id=org_id,
            context={...},
        )
    except Exception:
        logger.debug("Access log failed for search_memories", exc_info=True)
```

`log_batch_access` (`src/lucent/db/access.py:73-128`) opens a transaction
and:

- writes one row per result into `memory_access_log` (10 rows for the
  audited payload),
- then runs a single `UPDATE memories SET last_accessed_at = NOW(),
  lifecycle_stage = CASE WHEN lifecycle_stage IN ('consolidating', 'archived')
  THEN 'active' ELSE lifecycle_stage END WHERE id IN (...) AND deleted_at IS
  NULL`.

This UPDATE takes **row-level write locks on the `memories` table** —
exactly the table that consolidation jobs, vitality scoring, and
`update_memory` calls also write. It is also the table that is
**heavily** read+written across the daemon. A concurrent long-running
write (e.g. consolidation reactivating a batch, vitality scorer rewriting
`vitality_score` on hundreds of rows, an admin running a batch update) can
hold conflicting row locks and pin our 10-row UPDATE — and therefore the
entire `search_memories` response — for tens of seconds.

The `search_memories` MCP tool **does not need** the access log to be
durable before returning the result. The log is best-effort (`except
Exception: logger.debug(...)`). It exists only for analytics. Holding it on
the request path turns an analytics write into a P99 latency multiplier.

This is consistent with the observed pattern: 6 timeouts spread thinly over
two weeks, all on a high-traffic tag, all on read paths that nominally
return in milliseconds. It is the textbook signature of an unrelated writer
holding a row lock during a synchronous write-on-read.

### 2B. Always-on COUNT(\*) duplicate scan *(secondary fix)*

`MemoryRepo.search()` issues both `count_query` and `search_query` against
the same predicates on every call (`src/lucent/db/memory.py:1187-1196`),
even though most callers only render the page (`memories`,
`has_more`) and ignore `total_count`.

For the audited payload at production scale this is sub-ms and not the
proximate cause, but it doubles the work that any future scaling has to
absorb and it doubles the chance of hitting whatever lock or buffer-cache
pothole 2A is sitting on. Recommend gating the count behind a
`return_total_count: bool = True` parameter and defaulting it off in the
MCP tool, or computing `has_more` from `LIMIT+1` instead.

### 2C. MCP bridge `ClientSession` has no `read_timeout_seconds`
*(hygiene)*

`src/lucent/llm/mcp_bridge.py:88`:

```python
session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
```

No `read_timeout_seconds` is supplied here, and `call_tool(...)` is
invoked without `read_timeout_seconds` (line 170). In the Python MCP SDK
(`mcp.shared.session.BaseSession.send_request`), this means `timeout =
None` — the bridge will wait indefinitely. Timeouts only surface from the
caller upstream of the bridge (Codex CLI / Claude Code / equivalent),
which is what produces the `-32001` we see in the audit log.

This is not the cause of the timeouts, but it makes them harder to
diagnose: the lucent server emits no client-visible "timed out at the
bridge" event, and operators have to correlate against external runners.

---

## 3. Recommended fix

In priority order. Each is independently shippable.

### Fix 1 — Move `log_batch_access` off the response path (request, not memo)

`src/lucent/tools/memories.py`, in both `search_memories` and
`search_memories_full`, replace the awaited call with a fire-and-forget
task:

```python
if result["memories"]:
    memory_ids_accessed = [m["id"] for m in result["memories"]]
    asyncio.create_task(
        _safe_log_batch_access(
            memory_ids=memory_ids_accessed,
            access_type="search_result",
            user_id=user_id,
            organization_id=org_id,
            context={
                "query": search_input.query,
                "type": search_input.type.value if search_input.type else None,
                "tags": search_input.tags,
            },
        )
    )
```

with `_safe_log_batch_access` swallowing exceptions, logging them at
DEBUG, and acquiring its own `access_repo` lazily so the response path
never waits. Apply the same change to `get_accessible` and any other read
path that currently writes the access log inline.

The trade-off is that an access log entry can be lost if the process is
killed between the response and the log flush. This is acceptable: the
log is already best-effort (the existing `except Exception: logger.debug`
shows we never required durability) and the information value of search
access logs is statistical, not auditable.

### Fix 2 — Make `total_count` opt-in

In `MemoryRepo.search()` (`src/lucent/db/memory.py:968-1196`), add a
`include_total: bool = True` kwarg. When `False`, skip the count query and
return `total_count = None` and `has_more` from a LIMIT+1 trick:

```python
# request limit+1, drop the extra row but record has_more=True
effective_limit = limit + 1
...
rows = await conn.fetch(search_query, *params)
has_more = len(rows) > limit
rows = rows[:limit]
total_count = None
```

In `search_memories` / `search_memories_full` MCP tools, default
`include_total=False` and only set it `True` when the caller passes
`return_total=true` (new optional parameter). The existing
`total_count`/`has_more` shape is preserved.

### Fix 3 — Set an explicit timeout on the MCP bridge `ClientSession`

`src/lucent/llm/mcp_bridge.py`, line 88:

```python
from datetime import timedelta
session = await stack.enter_async_context(
    ClientSession(
        read_stream,
        write_stream,
        read_timeout_seconds=timedelta(seconds=60),
    )
)
```

This makes lucent-side bridge calls fail with a uniform McpError(408)
instead of waiting forever, and matches the de-facto upstream runner
budget of 60 s.

### Fix 4 — Skill-level guidance *(no code change)*

Update the `memory-search`, `memory-capture`, `memory-management`,
`learning-extraction`, and `self-improvement` skills to **avoid using
`tags=["validated"]` as the sole filter**. `validated` is a near-no-op
filter (~80 % of memories on the synthetic dataset; ~10 % of *active*
production memories today, but trending up). Combine it with `type`,
`importance_min >= 7`, or a content `query` to narrow the result set
before sort. This both improves relevance and incidentally reduces server
load if the hot-path stall returns.

### Fix 5 — *Not recommended:* adding a new tag-related index

The existing `idx_memories_tags` / `idx_memories_tags_active` GIN
indexes are sufficient. Adding a covering index would not change the
plan: the planner intentionally skips GIN when the tag is non-selective.
A multi-column `(user_id, importance DESC, created_at DESC)` partial
index *could* shave the top-N sort when the user has many own rows, but
that is unrelated to the tag-timeout signal and we should not add it on
this evidence.

### Fix 6 — *Optional:* per-statement timeout

Set `statement_timeout = '30s'` on the `lucent` role so a runaway
read query can never exceed the upstream 60 s budget. This is a defensive
backstop, not a fix; it would convert the silent `-32001` into an
explicit `canceling statement due to statement timeout` we can alert on.

---

## 4. Validation plan after Fix 1

1. Replay the failing payload from a memory-agent context against staging:
   `search_memories(tags=["validated"], limit=10)` × 50 concurrent.
2. Concurrently run a synthetic writer: `UPDATE memories SET vitality_score
   = vitality_score WHERE created_at < now() - interval '7 days';` (forces
   row-lock contention on the same table).
3. Confirm `search_memories` P99 stays under 1 s and no `-32001` errors are
   emitted by the calling agent runner.
4. Confirm rows still land in `memory_access_log` (asynchronously).

---

## 5. Reproduction artifacts

- Synthetic dataset DDL + seed: `perftest` database on `lucent-db`
  container. Schema mirrors `001_init.sql` plus the partial indexes
  added through migration `023_add_performance_indexes.sql`.
- Raw EXPLAIN outputs captured at investigation time:
  `/tmp/explain_prod.txt`, `/tmp/explain_perftest.txt`,
  `/tmp/explain_perftest_count.txt` on the agent host (transient — see
  inline blocks above for the canonical capture).
- Code paths read at HEAD `743a098` (`origin/main`):
  - `src/lucent/tools/memories.py:621-754` (MCP entry)
  - `src/lucent/services/memory_access_service.py:175-218`
    (`_resolve_accessible_repos` + `search`)
  - `src/lucent/db/memory.py:968-1196` (`MemoryRepo.search`)
  - `src/lucent/db/access.py:73-128` (`log_batch_access`)
  - `src/lucent/llm/mcp_bridge.py:67-105, 145-175`
  - `src/lucent/db/migrations/001_init.sql`,
    `010_add_search_indexes.sql`, `023_add_performance_indexes.sql`

---
name: daemon-operations-specialized
description: 'Monitor, troubleshoot, and optimize daemon cognitive cycles — task throughput, memory usage patterns, cycle timing, health checks'
---

# Daemon Operations

Operational monitoring and optimization procedures for the Lucent daemon's cognitive loop, task dispatch, and autonomic layers.

## When to Use

- Checking daemon health and throughput
- Tuning cycle timing or concurrency settings
- Investigating slow cycles or low task completion rates
- Reviewing memory usage growth patterns
- Monitoring multi-instance coordination
- Running routine health checks

## Architecture Quick Reference

The daemon (`daemon/daemon.py`) has three layers:

| Layer | Function | Default Cadence |
|-------|----------|-----------------|
| **Cognitive Loop** | Perceive → Reason → Decide → Act | Every 15 min (`DAEMON_INTERVAL_MINUTES`) |
| **Task Dispatch** | Claim + run sub-agent sessions (max 2/cycle) | After each cognitive cycle |
| **Autonomic** | Memory maintenance, learning extraction | Every 8 cycles (`AUTONOMIC_INTERVAL`) |

## Health Checks

### 1. Instance Health

**Check heartbeat memory**:
```
search_memories with tags=["daemon-heartbeat"]
```

The heartbeat contains:
- `instance_id`: `{hostname}-{pid}-{timestamp}` — unique per daemon process
- `cycle_count`: increments each cycle — confirms the loop is progressing
- `timestamp`: last update — stale means the daemon is stuck or dead
- `model`: which LLM model is running the cognitive loop
- `max_sessions`: concurrency limit

**Healthy**: `timestamp` within the last `DAEMON_INTERVAL_MINUTES` + a few minutes of buffer.
**Stale**: `timestamp` older than 2× the interval — daemon likely crashed or watchdog killed it.

### 2. Server Health

```bash
curl http://localhost:8766/api/health
```

If this fails, the MCP server is down — the daemon can't read or write memories.

### 3. Database Health

```bash
docker exec lucent-db pg_isready -U lucent
```

If PostgreSQL is unreachable, all memory operations will timeout (15s per request in `MemoryAPI`).

### 4. Log Health

```bash
tail -20 daemon/daemon.log
```

Log rotates at 10 MB with 5 backups. If the file is missing or empty, the daemon hasn't started. Check for `ERROR` or `WARNING` lines.

## Cycle Timing Analysis

### Understanding Cycle Duration

Each cognitive cycle consists of:
1. `_update_heartbeat()` — ~1 API call
2. `_release_stale_claims()` — 1 search + potential updates
3. `run_cognitive_cycle()` — 1 LLM session (cognitive-N) with `SESSION_TOTAL_TIMEOUT` of 720s
4. `_dispatch_pending_tasks()` — up to 2 sub-agent sessions, each with 720s timeout

**Theoretical max cycle time**: ~2160s (36 min) if all 3 sessions hit timeout.
**Typical cycle time**: 2–5 minutes for cognitive + 2–10 minutes per dispatched task.

### Monitoring Cycle Performance

1. **Parse log timestamps**: Each cycle logs start/end markers. Diff the timestamps.
   ```bash
   grep -E "cognitive cycle|dispatch" daemon/daemon.log | tail -20
   ```
2. **Check daemon-state memory**: Updated each cycle with decisions made and time taken.
3. **Count completed tasks over time**: Search `daemon-task` + `completed` memories, check `updated_at` distribution.

### Tuning Cycle Interval

| Setting | Effect | Trade-off |
|---------|--------|-----------|
| `DAEMON_INTERVAL_MINUTES=5` | More responsive | Higher API/LLM costs |
| `DAEMON_INTERVAL_MINUTES=30` | Lower cost | Slower task pickup |
| `DAEMON_INTERVAL_MINUTES=15` (default) | Balanced | Good for most workloads |

The interval is the **pause between cycles**, not cycle duration. Actual time between cycle starts = interval + cycle duration.

## Task Throughput

### Measuring Throughput

1. **Pending queue depth**: `search_memories` with tags `["daemon-task", "pending"]` — count results
2. **In-progress tasks**: `search_memories` with tags `["daemon-task", "in-progress"]` — should be ≤ `MAX_CONCURRENT_SESSIONS`
3. **Completed tasks**: `search_memories` with tags `["daemon-task", "completed"]` — growth rate = throughput
4. **Failed/released tasks**: Look for tasks that cycled between `pending` → `in-progress` → `pending` multiple times (check version count via `get_memory_versions`)

### Throughput Bottlenecks

| Bottleneck | Diagnosis | Fix |
|-----------|-----------|-----|
| All tasks pending, none dispatched | Cognitive loop not running or not finding tasks | Check heartbeat, check logs |
| Tasks dispatched but not completing | Sub-agent sessions timing out | Check LLM latency, reduce task complexity |
| Tasks completing but results invalid | `_validate_task_result()` rejecting short/failed results | Review task descriptions for clarity |
| Queue growing faster than completion | Too many tasks created per cycle | Reduce cognitive cycle ambition, increase `MAX_CONCURRENT_SESSIONS` |

### Concurrency Tuning

`MAX_CONCURRENT_SESSIONS` (default 3) controls the asyncio semaphore for parallel sessions.

- **Increase to 5**: If LLM latency is high and tasks are independent
- **Decrease to 1**: If tasks have side effects that conflict (e.g., editing same files)
- The dispatch loop processes max 2 tasks per cycle (`max_tasks=2` in `_dispatch_pending_tasks`)

## Memory Usage Patterns

### Tag Inventory

Monitor these key tags for growth:

| Tag | Expected Growth | Alert If |
|-----|----------------|----------|
| `daemon-task` | Steady (created + completed balance) | Pending count growing unbounded |
| `daemon-result` | Grows with completed tasks | Exceeds 100 without consolidation |
| `daemon-state` | 1 per cycle (updated in place) | Multiple exist (should be singleton) |
| `daemon-heartbeat` | 1 per instance | Multiple stale heartbeats (crashed instances) |
| `daemon-message` | Sporadic (inter-agent comms) | Unprocessed messages accumulating |
| `lesson` | Grows slowly via learning extraction | Duplicates (check `memory-management` skill) |

### Memory Cleanup Triggers

The autonomic layer runs every `AUTONOMIC_INTERVAL` (8) cycles and dispatches the `memory` sub-agent for maintenance. Learning extraction runs every `LEARNING_INTERVAL` (16) cycles.

If memories are growing too fast:
1. Check if `daemon-result` memories are being created but never consolidated
2. Check if old `daemon-task` + `completed` memories are accumulating — they can be archived
3. Run manual consolidation via `memory-management` skill

### Result Storage

Task results are stored in two places:
- Task memory metadata: `{"result": "..."}` truncated to `MAX_RESULT_LENGTH` (8000 chars)
- Separate `daemon-result` memory with the full result

This duplication is intentional — the metadata enables quick scanning, the separate memory enables full retrieval.

## Multi-Instance Coordination

### How Instances Coordinate

Multiple daemon instances share the same memory store. Coordination is tag-based:

1. **Task claiming**: Atomic tag swap `pending` → `in-progress`. First writer wins.
2. **Heartbeat tracking**: Each instance writes its own heartbeat. Other instances can read all heartbeats.
3. **Stale release**: Any instance can release tasks stuck `in-progress` for >30 minutes (`_release_stale_claims()`).

### Monitoring Multi-Instance

1. Search for all heartbeat memories: tags `["daemon-heartbeat"]`
2. Each should have a unique `instance_id` and recent `timestamp`
3. Count active instances: heartbeats with `timestamp` < 2× interval
4. Check for split-brain: multiple instances claiming the same task (look for rapid tag flipping in `get_memory_versions`)

### Instance Conflicts

If two instances claim the same task:
- Both update tags to `in-progress`
- Both run sub-agent sessions
- Both try to mark `completed`
- One will succeed, the other's update may clobber — this is a known limitation
- Mitigation: keep `MAX_CONCURRENT_SESSIONS` low across instances to reduce collision probability

## Operational Runbook

### Daily Check

```bash
# 1. Process alive?
ps aux | grep daemon.py

# 2. Server healthy?
curl -s http://localhost:8766/api/health | python -m json.tool

# 3. Recent log activity?
tail -5 daemon/daemon.log

# 4. Check pending queue via MCP
# search_memories(tags=["daemon-task", "pending"])

# 5. Check for stuck tasks via MCP
# search_memories(tags=["daemon-task", "in-progress"])
```

### After Configuration Changes

When changing environment variables:
1. Stop the daemon: kill the process (note the PID from heartbeat)
2. Update env vars
3. Restart and watch logs: `tail -f daemon/daemon.log`
4. Verify heartbeat updates within one interval
5. Verify a cognitive cycle completes successfully

### After Code Changes

The daemon watches source files for changes and auto-reloads (`run_forever()` watches file modification times). If auto-reload fails:
1. Check logs for reload errors
2. Manual restart may be needed for breaking changes
3. Verify the `adaptation.py` templates still render correctly if agent/skill templates changed

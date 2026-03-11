---
name: daemon-debugging-specialized
description: 'Diagnose and fix daemon cycle failures, stuck tasks, sub-agent dispatch issues, and memory corruption in the Lucent daemon architecture'
---

# Daemon Debugging

Procedures for diagnosing failures in the Lucent daemon's three-layer architecture: cognitive loop, task dispatch, and autonomic maintenance.

## When to Use

- Daemon stops producing output or log entries
- Tasks stay `in-progress` or `pending` indefinitely
- Sub-agent sessions fail or return empty results
- Cognitive cycles complete but produce no tasks
- Memory operations timeout or return unexpected results
- Watchdog kills the process (`os._exit(1)`)

## Key Files

| File | Purpose |
|------|---------|
| `daemon/daemon.py` | Main orchestrator — `LucentDaemon`, `MemoryAPI`, task dispatch |
| `daemon/cognitive.md` | PRDA protocol (Perceive → Reason → Decide → Act) |
| `daemon/adaptation.py` | Environment assessment and `AdaptationPipeline` |
| `daemon/agents/*.agent.md` | Sub-agent role definitions |
| `daemon/daemon.log` | Runtime log (rotates at 10 MB, keeps 5 backups) |

## Debugging Procedures

### 1. Daemon Not Running or Crashing

**Symptoms**: No new log entries, heartbeat memory stale, process not found.

**Steps**:
1. Check if the process is alive: `ps aux | grep daemon.py`
2. Read the tail of the log: `tail -100 daemon/daemon.log`
3. Look for the watchdog kill pattern — if the last log line is older than 900s with no activity, the watchdog thread calls `os._exit(1)`. Search for `WATCHDOG` in logs.
4. Check for Python exceptions — `_run_session_inner()` wraps sessions in `SESSION_TOTAL_TIMEOUT` (720s). A `TimeoutError` here returns `None` and logs `ERROR`.
5. Check environment variables: `DATABASE_URL`, `MODEL` (default `claude-opus-4.6`), `DAEMON_INTERVAL_MINUTES` (default 15).

**Common causes**:
- Missing `DATABASE_URL` → server can't start
- LLM API key expired → all sessions fail with auth errors
- Port conflict on 8766 → server startup fails

### 2. Stuck Tasks (in-progress forever)

**Symptoms**: Tasks tagged `daemon-task` + `in-progress` with `updated_at` older than 30 minutes.

**Steps**:
1. Search for stuck tasks via MCP: `search_memories` with tags `["daemon-task", "in-progress"]`
2. Check the `updated_at` timestamp — `_release_stale_claims()` in `daemon.py` releases tasks where `updated_at` > 30 minutes ago by swapping `in-progress` → `pending`
3. If the release mechanism isn't firing, check that `run_cognitive_cycle()` is being called (it runs `_release_stale_claims()` at the start of each cycle)
4. Manual fix: call `update_memory(memory_id, tags=[...replace "in-progress" with "pending"...])`
5. Check if the sub-agent session timed out — look for `TimeoutError` or `SESSION_TOTAL_TIMEOUT` in logs around the task's `updated_at` time

**Root causes**:
- Session hung waiting for LLM response (no watchdog kill because other sessions are active)
- `_validate_task_result()` failed but the error handler didn't release the claim
- Multiple daemon instances racing on the same task (check `instance_id` in heartbeat memories)

### 3. Task Dispatch Failures

**Symptoms**: Pending tasks exist but never get picked up. Logs show cognitive cycles completing but `_dispatch_pending_tasks()` finds nothing or fails.

**Steps**:
1. Verify pending tasks exist: `search_memories` with tags `["daemon-task", "pending"]`
2. Check task memory content — `_dispatch_pending_tasks()` reads the memory content to build the sub-agent prompt. If content is empty or malformed, it skips the task.
3. Check the `role` field in task metadata — it must match a valid agent definition in `daemon/agents/`. Valid roles: `research`, `code`, `memory`, `reflection`, `documentation`, `planning`, `assessment`.
4. Check `MAX_CONCURRENT_SESSIONS` (default 3) — if the semaphore is full, new dispatches block.
5. Look for the atomic claim pattern: the daemon updates tags from `pending` to `in-progress`. If another instance already claimed it, the tags won't match on re-read.
6. Confirm the sub-agent definition file exists: `daemon/agents/{role}.agent.md`

**The dispatch flow** (lines 926–1076 of `daemon.py`):
```
find pending tasks → get memory details → atomic claim (tag swap) → run_session() → validate result → mark completed
```

### 4. Task Validation Failures

**Symptoms**: Tasks complete but get released back to `pending` instead of `completed`.

**Steps**:
1. Check `_validate_task_result()` logic — a result is valid if:
   - Length ≥ 1000 characters, OR
   - Length 100–999 characters AND no failure indicators present
2. Failure indicators that trigger rejection:
   ```
   "couldn't find", "could not find", "unable to",
   "failed to", "i don't have", "i do not have",
   "no context", "cannot complete", "error occurred"
   ```
3. If multi-model review is enabled (`REVIEW_MODELS` env var), each review model must also approve. Check `_multi_model_review()` output in logs.
4. If `REQUIRE_APPROVAL=true`, tasks go to `needs-review` instead of `completed` — they need human approval via feedback memories.

### 5. Cognitive Cycle Produces No Work

**Symptoms**: Cycles run on schedule but `daemon-state` memory shows no new tasks created.

**Steps**:
1. Read the `daemon-state` memory — it records what the last cycle decided and why.
2. Check for `daemon-message` memories — collaborators may have sent instructions the cycle is trying to process.
3. Check for `feedback-approved` / `feedback-rejected` memories — the cognitive loop processes these FIRST (per `cognitive.md`). If many are queued, the cycle may spend its budget processing feedback instead of creating tasks.
4. Verify the cognitive prompt includes environment context — search for `environment` tagged memories. If none exist, the cycle lacks domain awareness.
5. Check if all goals are marked completed — no active goals means nothing to work toward.

### 6. Memory Operation Failures

**Symptoms**: `MemoryAPI` calls return empty results or timeout.

**Steps**:
1. Check the Lucent server is running: `curl http://localhost:8766/api/health`
2. `MemoryAPI` methods use a 15-second timeout per request. Check logs for timeout warnings.
3. Verify `DATABASE_URL` points to a running PostgreSQL instance.
4. Check PostgreSQL connection pool — high concurrency can exhaust connections.
5. Test directly: `curl http://localhost:8766/api/memories/search -H "Authorization: Bearer <key>"`

### 7. Watchdog Kills

**Symptoms**: Process exits with code 1, log shows `WATCHDOG` message or process just disappears.

**Steps**:
1. The watchdog thread in `run_forever()` checks every 60 seconds if the last log activity was > `WATCHDOG_TIMEOUT` (900s / 15 min) ago.
2. If exceeded, it calls `os._exit(1)` — no cleanup, no graceful shutdown.
3. This usually means a session is blocking the event loop. Check for:
   - LLM API hanging (network issues)
   - Database query blocking (lock contention)
   - `asyncio` deadlock (awaiting something that will never complete)
4. Look at the last few log entries before the gap to identify what was running.

## Quick Diagnostic Checklist

```
1. Is the process running?           → ps aux | grep daemon.py
2. Is the server healthy?            → curl localhost:8766/api/health
3. Is the database reachable?        → docker exec lucent-db pg_isready -U lucent
4. Are there stale tasks?            → search tags=["daemon-task","in-progress"]
5. Is the heartbeat current?         → search tags=["daemon-heartbeat"]
6. What did the last cycle decide?   → search tags=["daemon-state"]
7. Are there pending messages?       → search tags=["daemon-message"]
8. What errors are in the log?       → grep -i "error\|exception\|timeout" daemon/daemon.log | tail -20
```

## Environment Variables Reference

| Variable | Default | Impact on Debugging |
|----------|---------|-------------------|
| `DAEMON_INTERVAL_MINUTES` | 15 | Time between cycles — long intervals look like hangs |
| `SESSION_TOTAL_TIMEOUT` | 720 | Max session duration before forced timeout |
| `WATCHDOG_TIMEOUT` | 900 | Idle time before process kill |
| `MAX_CONCURRENT_SESSIONS` | 3 | Limits parallel task execution |
| `REQUIRE_APPROVAL` | false | Tasks need human review if true |
| `REVIEW_MODELS` | "" | Comma-separated models for multi-model review |
| `ALLOW_GIT_COMMIT` | false | Blocks commit if false |
| `ALLOW_GIT_PUSH` | false | Blocks push if false |

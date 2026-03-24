---
name: daemon-debugging
description: 'Diagnose and fix daemon cycle failures, stuck tasks, sub-agent dispatch issues, and memory corruption in the Lucent daemon architecture. Use when daemon cycles fail, tasks get stuck, sub-agent dispatch breaks, or memory corruption is suspected.'
---

# Daemon Debugging

Procedures for diagnosing failures in the Lucent daemon's three-layer architecture: cognitive loop, task dispatch, and autonomic maintenance.

## Disambiguation

This skill is for **diagnosing and fixing failures** — crashes, stuck tasks, dispatch errors, memory corruption. Use it when something is broken.

- For routine **health monitoring, throughput analysis, or optimization** of a working daemon → use **daemon-operations**

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
| `daemon/templates/agents/` | Jinja2 templates for generating new agent definitions |
| `daemon/daemon.log` | Runtime log (rotates at 10 MB, keeps 5 backups) |

## MCP Tools Used

| Tool | Purpose | Key Parameters |
|------|---------|---------------|
| `memory-server-search_memories` | Check heartbeat and daemon-state | `tags=["daemon-heartbeat"]`, `tags=["daemon-state"]` |
| `memory-server-search_memories` | Find pending daemon messages | `tags=["daemon-message"]` |
| `memory-server-get_memory` | Get full heartbeat/state detail | `memory_id` |

## Quick Diagnostic Checklist

```
1. Is the process running?           → ps aux | grep daemon.py
2. Is the server healthy?            → curl localhost:8766/api/health
3. Is the database reachable?        → docker exec lucent-db pg_isready -U lucent
4. Are there stale tasks?            → Activity page or GET /api/requests/queue/pending
5. Is the heartbeat current?         → memory-server-search_memories(tags=["daemon-heartbeat"])
6. What did the last cycle decide?   → memory-server-search_memories(tags=["daemon-state"])
7. Are there pending requests?       → Activity page or GET /api/requests?status=pending
8. What errors are in the log?       → grep -i "error\|exception\|timeout" daemon/daemon.log | tail -20
```

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

**Symptoms**: Tasks in `running` status for longer than 30 minutes.

**Steps**:
1. Check the Activity page for tasks stuck in `running` status
2. The daemon's `_release_stale_claims()` releases tasks where `started_at` > 30 minutes ago
3. If the release mechanism isn't firing, check that the cognitive cycle is running (heartbeat memory should be recent)
4. Manual fix: `POST /api/requests/queue/release-stale?stale_minutes=30`
   ```bash
   curl -s -X POST "http://localhost:8766/api/requests/queue/release-stale?stale_minutes=30"
   ```
5. Check if the sub-agent session timed out — look for `TimeoutError` or `SESSION_TOTAL_TIMEOUT` in logs around the task's `updated_at` time

**Root causes**:
- Session hung waiting for LLM response (no watchdog kill because other sessions are active)
- `_validate_task_result()` failed but the error handler didn't release the claim
- Multiple daemon instances racing on the same task (check `instance_id` in heartbeat memories)

### 3. Task Dispatch Failures

**Symptoms**: Pending tasks exist but never get picked up. Logs show cognitive cycles completing but `_dispatch_pending_tasks()` finds nothing or fails.

**Steps**:
1. Verify pending tasks exist: check Activity page or `GET /api/requests/queue/pending`
2. Check task description — `_dispatch_pending_tasks()` reads the task row to build the sub-agent prompt. If description is empty, it may skip.
3. Check the `agent_type` field — it must match an active agent definition. Check Agents & Skills page for available agents.
4. Check `MAX_CONCURRENT_SESSIONS` (default 3) — if the semaphore is full, new dispatches block.
5. Check that the agent definition exists and is approved (status = `active`) in the definitions table.
6. The daemon builds the prompt via `build_subagent_prompt(agent_type, description, agent_definition_id)`.

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
1. Read the `daemon-state` memory:
   ```
   memory-server-search_memories(tags=["daemon-state"], limit=1)
   ```
   It records what the last cycle decided and why.
2. Check for `daemon-message` memories — collaborators may have sent instructions the cycle is trying to process:
   ```
   memory-server-search_memories(tags=["daemon-message"])
   ```
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

## Decision: Which Phase is Failing?

- IF daemon process is not found → **restart daemon**, check startup errors
- ELIF heartbeat memory is stale (>2× interval) → **watchdog killed or hung**, check logs for WATCHDOG or TimeoutError
- ELIF heartbeat is current BUT tasks stuck in `pending` → **dispatch issue**, check agent_type validity and semaphore
- ELIF heartbeat is current BUT tasks stuck in `running` → **execution hung**, run release-stale endpoint
- ELIF cycles run but no tasks are being created → **cognitive cycle producing no work**, check daemon-state memory
- ELIF tasks complete but get released back → **validation failure**, check task description quality and failure indicators
- ELSE → **memory API issue**, check server health and PostgreSQL connectivity

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

## Example: Good Debugging Session

```
1. User reports "daemon seems stuck"
2. Run: memory-server-search_memories(tags=["daemon-heartbeat"])
   → Heartbeat timestamp is 45 minutes old (interval is 15 min → STALE)
3. Run: ps aux | grep daemon.py → process NOT found
4. Run: tail -50 daemon/daemon.log → last line: "WATCHDOG: no activity for 900s, exiting"
5. Check what was running before exit: look 15 min back in log
   → Find: "Running session for task abc123 (agent: code)"
   → Session started but never finished
6. Root cause: LLM API was timing out silently
7. Fix: Restart daemon, verify heartbeat appears within 15 min
   curl -s http://localhost:8766/api/health → confirm server healthy
   python -m daemon.daemon &
   Wait 15 min, then: memory-server-search_memories(tags=["daemon-heartbeat"])
```

## Example: Anti-Pattern (What NOT to Do)

```
❌ Bad approach:
- Check daemon logs without first verifying the server is up
- Assume the daemon is stuck without checking the heartbeat timestamp
- Delete tasks manually without understanding why they're stuck
- Restart the daemon without capturing the root cause

✅ Correct approach:
- Follow the Quick Diagnostic Checklist top-to-bottom
- Read daemon-state memory before intervening
- Release stale claims via API rather than manual DB edits
- Save findings as a memory before fixing
```

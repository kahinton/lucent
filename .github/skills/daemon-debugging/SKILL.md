---
name: daemon-debugging
description: 'Diagnose and fix daemon cycle failures, stuck tasks, and sub-agent dispatch issues specific to the Lucent daemon architecture'
---

# Daemon Debugging

Diagnose and fix daemon cognitive cycle failures, stuck tasks, and sub-agent dispatch issues in the Lucent daemon architecture.

## When to Use

- Daemon cycle is failing or producing errors
- Tasks are stuck in `pending` or `in_progress` state and not progressing
- Sub-agent dispatch is failing or timing out
- Memory API calls from the daemon are returning errors
- Cycle timing is degraded or the daemon is running slower than expected

## Key Files

- `daemon/daemon.py` — Main daemon loop implementing the perceive→reason→decide→act cycle
- `daemon/` — Supporting daemon modules (config, agents, scheduling)
- `src/lucent/tools/` — MCP tools the daemon calls for memory operations
- `docker-compose.yml` — Service definitions (postgres, lucent server)

## Debugging Process

### Step 1: Check Daemon Logs

1. Review daemon stdout/stderr for tracebacks or error messages
2. Look for repeated error patterns indicating a stuck loop
3. Check timestamps to identify when the issue started
4. Run `docker compose logs daemon` if running in Docker

### Step 2: Identify the Failing Cycle Phase

The daemon runs a **perceive → reason → decide → act** loop. Determine which phase is failing:

- **Perceive**: Failure to read tasks or memory state. Check memory API connectivity and response codes.
- **Reason**: Errors during LLM calls or context assembly. Check for token limit issues or malformed prompts.
- **Decide**: Task selection or prioritization failures. Check for empty task queues or invalid task states.
- **Act**: Sub-agent dispatch or execution failures. Check agent type validity and dispatch timeout settings.

### Step 3: Diagnose Stuck Tasks

1. Query the task store for tasks stuck in `pending` — look for missing dependencies or invalid `agent_type`
2. Check for tasks stuck in `in_progress` — the daemon or sub-agent may have crashed mid-execution
3. Look for circular dependencies preventing any task from becoming ready
4. Verify task validation logic is not rejecting all candidates

### Step 4: Diagnose Memory API Errors

1. Verify the Lucent server is running: `curl http://localhost:8000/health`
2. Check PostgreSQL connectivity: `docker compose exec postgres pg_isready`
3. Look for HTTP 4xx/5xx responses in daemon logs when calling memory tools
4. Test memory operations manually via the MCP endpoint to isolate server vs. daemon issues

### Step 5: Diagnose Sub-Agent Dispatch Failures

1. Check that the requested `agent_type` is valid and has a registered handler
2. Review dispatch timeout settings — long-running tasks may exceed the timeout
3. Look for resource exhaustion (too many concurrent sub-agents)
4. Verify sub-agent environment variables and API keys are configured

### Step 6: Diagnose Cycle Timing Issues

1. Measure time spent in each cycle phase (perceive, reason, decide, act)
2. Check for slow memory API responses adding latency
3. Look for LLM API rate limiting or high response times
4. Verify sleep/backoff intervals between cycles are appropriate

## Common Fixes

- **Stuck pending tasks**: Reset task state or fix missing dependencies
- **Memory API 500 errors**: Restart the Lucent server or check PostgreSQL disk space
- **Sub-agent timeout**: Increase dispatch timeout or break task into smaller subtasks
- **Cycle not advancing**: Check for infinite loops in decide phase when no tasks are ready
- **Connection refused**: Ensure all Docker services are running with `docker compose ps`

## Best Practices

- Always check the simplest explanations first (service down, network issue, bad config)
- Use `docker compose logs --tail=50 <service>` to get recent logs without noise
- When resetting stuck tasks, investigate root cause before clearing state
- Monitor cycle duration over time to catch gradual degradation

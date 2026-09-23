# Database Call Site Audit

Static audit of Python runtime DB calls outside `src/lucent/db`.

Pattern: `.execute()`, `.fetch()`, `.fetchrow()`, `.fetchval()`, and
`text()` call sites. Runtime proxy/wrapper methods are included in the
inventory; SQL keywords alone are intentionally omitted.

## Summary

- **52 files** contain candidate call sites.
- Highest concentrations:
  - `src/lucent/web/routes/definitions.py`: 58
  - `src/lucent/tools/requests.py`: 43
  - `src/lucent/tools/definitions.py`: 39
  - `src/lucent/tools/memories.py`: 37
  - `src/lucent/web/routes/projects.py`: 19
  - `src/lucent/web/routes/settings.py`: 18
  - `src/lucent/web/routes/requests_routes.py`: 16
  - `src/lucent/web/routes/memories.py`: 14
  - `src/lucent/api/routers/chat.py`: 14
  - `src/lucent/api/routers/schedules.py`: 12
  - `src/lucent/web/routes/sandboxes.py`: 11

## Migration Status

The runtime DB migration is complete. The remaining matching `.execute()`
sites are executor/model calls, not database calls, in
`src/lucent/api/routers/definitions.py`, `src/lucent/tools/definitions.py`,
and `src/lucent/llm/langchain_engine.py`.

## Full Inventory

```text
58 src/lucent/web/routes/definitions.py
43 src/lucent/tools/requests.py
39 src/lucent/tools/definitions.py
37 src/lucent/tools/memories.py
19 src/lucent/web/routes/projects.py
18 src/lucent/web/routes/settings.py
16 src/lucent/web/routes/requests_routes.py
14 src/lucent/web/routes/memories.py
14 src/lucent/api/routers/chat.py
12 src/lucent/api/routers/schedules.py
11 src/lucent/web/routes/sandboxes.py
9 src/lucent/web/routes/admin.py
8 src/lucent/web/routes/schedules.py
8 src/lucent/api/routers/memories.py
7 src/lucent/web/routes/groups.py
7 src/lucent/web/routes/dashboard.py
7 src/lucent/web/routes/daemon.py
6 src/lucent/web/routes/user_interactions.py
6 src/lucent/web/routes/files.py
6 src/lucent/web/routes/connections.py
6 src/lucent/tools/schedules.py
5 src/lucent/server.py
5 src/lucent/secrets/builtin.py
5 src/lucent/llm/mcp_bridge.py
5 src/lucent/llm/builtin_tools.py
4 src/lucent/web/routes/secrets.py
4 src/lucent/tools/tool_audit.py
4 src/lucent/secrets/transit.py
4 src/lucent/llm/langchain_engine.py
4 src/lucent/llm/context.py
2 src/lucent/web/routes/live.py
2 src/lucent/web/routes/chat.py
2 src/lucent/telemetry.py
2 src/lucent/storage/service.py
2 src/lucent/prompts/memory_usage.py
2 src/lucent/models/validation.py
2 src/lucent/llm/project_context.py
2 src/lucent/integrations/slack_adapter.py
2 src/lucent/api/app.py
1 src/lucent/web/routes/usage_analytics.py
1 src/lucent/web/routes/usage.py
1 src/lucent/web/routes/audit.py
1 src/lucent/web/routes/_shared.py
1 src/lucent/startup/tenant_chain.py
1 src/lucent/services/chat_intro.py
1 src/lucent/secrets/registry.py
1 src/lucent/log_context.py
1 src/lucent/llm/hooks.py
1 src/lucent/llm/attachments.py
1 src/lucent/integrations/encryption.py
1 src/lucent/api/routers/definitions.py
1 src/lucent/api/deps.py
```

## Remaining Work

The runtime DB migration is complete. The only remaining matching
`.execute()` sites are executor/model calls, not SQL calls:

- `src/lucent/api/routers/definitions.py`
- `src/lucent/tools/definitions.py`
- `src/lucent/llm/langchain_engine.py`

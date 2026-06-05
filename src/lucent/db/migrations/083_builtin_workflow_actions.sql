-- Migration 083: Built-in workflow action definitions
--
-- Migration 082 made schedules workflow-capable, but existing built-in rows
-- still had their execution instructions primarily in the legacy prompt column.
-- This migration promotes built-ins to explicit workflow actions. Server-side
-- built-ins such as Stale Task Reaper are represented as server_function
-- actions so the UI/runtime do not treat them as agent-dispatched tasks.

-- Stale Task Reaper runs inside the API process. It is not an agent task, so do
-- not advertise a non-existent "system" agent type.
UPDATE schedules
SET agent_type = 'lucent',
    prompt = '',
    trigger_type = 'schedule',
    trigger_config = jsonb_strip_nulls(jsonb_build_object(
        'schedule_type', schedule_type,
        'interval_seconds', interval_seconds,
        'timezone', timezone,
        'execution_mode', 'server_side',
        'runner', 'api_process',
        'preflight', 'stale_task_reaper_has_work'
    )),
    request_template = jsonb_build_object(
        'title_prefix', '[Server Workflow]',
        'title', title,
        'description', 'Server-side maintenance workflow. No request is created; results are recorded on schedule_runs.'
    ),
    actions = jsonb_build_array(jsonb_build_object(
        'action_type', 'server_function',
        'title', 'Release stale task claims',
        'description', 'Runs directly inside the Lucent API process. It checks for expired task claims or dead daemon owners, releases eligible claims, and records the result in schedule_runs. It does not dispatch an agent task.',
        'function', 'release_stale_tasks',
        'module', 'lucent.api.system_schedules',
        'execution_boundary', 'api_process',
        'preflight', 'stale_task_reaper_has_work',
        'sequence_order', 0
    )),
    review_instructions = 'No model review applies. Verify by checking schedule_runs.result and task claim state if this workflow reports released tasks.',
    updated_at = NOW()
WHERE title = 'Stale Task Reaper'
  AND is_system = true;

-- Other built-in schedules still create agent tasks, but their task action is
-- now explicit JSON. Keep prompt as a compatibility/source-copy field; runtime
-- prefers actions when present.
UPDATE schedules
SET trigger_type = 'schedule',
    trigger_config = jsonb_strip_nulls(jsonb_build_object(
        'schedule_type', schedule_type,
        'cron_expression', cron_expression,
        'interval_seconds', interval_seconds,
        'timezone', timezone,
        'max_runs', max_runs,
        'expires_at', expires_at
    )),
    request_template = jsonb_build_object(
        'title_prefix', '[Scheduled]',
        'title', title,
        'description', COALESCE(description, ''),
        'dependency_policy', 'strict'
    ),
    actions = jsonb_build_array(jsonb_strip_nulls(jsonb_build_object(
        'action_type', 'task',
        'title', title,
        'description', COALESCE(NULLIF(prompt, ''), description, title),
        'agent_type', agent_type,
        'model', model,
        'reasoning_effort', reasoning_effort,
        'sandbox_template_id', sandbox_template_id,
        'sandbox_config', sandbox_config,
        'priority', priority,
        'sequence_order', 0
    ))),
    review_instructions = COALESCE(NULLIF(review_instructions, ''), 'Review the generated request and recorded task outputs before approval.'),
    updated_at = NOW()
WHERE is_system = true
  AND title != 'Stale Task Reaper';

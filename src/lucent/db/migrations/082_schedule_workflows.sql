-- Migration 082: Schedule-backed workflows
--
-- Evolve the existing critical schedules table into a broader workflow model.
-- This is intentionally additive and compatibility-first: existing schedules,
-- schedule_runs, schedule IDs, and /schedules API paths remain valid.
-- New workflow fields describe trigger kind, request template, ordered actions,
-- and reviewer-facing completion checks.

-- Existing schedule_type is kept for compatibility but widened so non-time
-- workflows can be represented without pretending to be one-time schedules.
ALTER TABLE schedules DROP CONSTRAINT IF EXISTS valid_schedule_type;
ALTER TABLE schedules
    ADD CONSTRAINT valid_schedule_type CHECK (
        schedule_type IN ('once', 'interval', 'cron', 'manual', 'webhook', 'integration_event')
    );

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS trigger_type TEXT NOT NULL DEFAULT 'schedule',
    ADD COLUMN IF NOT EXISTS trigger_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS request_template JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS review_instructions TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS webhook_secret_hash TEXT,
    ADD COLUMN IF NOT EXISTS webhook_last_received_at TIMESTAMPTZ;

ALTER TABLE schedules DROP CONSTRAINT IF EXISTS valid_trigger_type;
ALTER TABLE schedules
    ADD CONSTRAINT valid_trigger_type CHECK (
        trigger_type IN ('schedule', 'manual', 'webhook', 'integration_event')
    );

ALTER TABLE schedules DROP CONSTRAINT IF EXISTS valid_workflow_actions_shape;
ALTER TABLE schedules
    ADD CONSTRAINT valid_workflow_actions_shape CHECK (jsonb_typeof(actions) = 'array');

ALTER TABLE schedules DROP CONSTRAINT IF EXISTS valid_trigger_config_shape;
ALTER TABLE schedules
    ADD CONSTRAINT valid_trigger_config_shape CHECK (jsonb_typeof(trigger_config) = 'object');

ALTER TABLE schedules DROP CONSTRAINT IF EXISTS valid_request_template_shape;
ALTER TABLE schedules
    ADD CONSTRAINT valid_request_template_shape CHECK (jsonb_typeof(request_template) = 'object');

-- Backfill trigger metadata for existing schedules. Existing rows are all
-- time-driven schedules unless a future migration or manual operator already
-- marked them otherwise.
UPDATE schedules
SET trigger_type = CASE
        WHEN schedule_type IN ('manual', 'webhook', 'integration_event') THEN schedule_type
        ELSE 'schedule'
    END
WHERE trigger_type IS NULL OR trigger_type = '';

UPDATE schedules
SET trigger_config = jsonb_strip_nulls(jsonb_build_object(
        'schedule_type', schedule_type,
        'cron_expression', cron_expression,
        'interval_seconds', interval_seconds,
        'timezone', timezone,
        'max_runs', max_runs,
        'expires_at', expires_at
    ))
WHERE trigger_config = '{}'::jsonb;

-- Convert the legacy single task prompt/template into the first workflow action
-- so existing schedules show up as action flows in the new UI while still
-- falling back to prompt/task_template at runtime if needed.
UPDATE schedules
SET actions = jsonb_build_array(jsonb_strip_nulls(jsonb_build_object(
        'action_type', 'task',
        'title', COALESCE(NULLIF(task_template->>'title', ''), title),
        'description', COALESCE(
            NULLIF(prompt, ''),
            NULLIF(task_template->>'description', ''),
            NULLIF(description, '')
        ),
        'agent_type', agent_type,
        'model', model,
        'reasoning_effort', reasoning_effort,
        'sandbox_template_id', sandbox_template_id,
        'sandbox_config', sandbox_config,
        'priority', priority,
        'sequence_order', 0
    )))
WHERE actions = '[]'::jsonb;

UPDATE schedules
SET request_template = jsonb_strip_nulls(jsonb_build_object(
        'title_prefix', CASE WHEN trigger_type = 'schedule' THEN '[Scheduled]' ELSE '[Workflow]' END,
        'title', title,
        'description', description
    ))
WHERE request_template = '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_schedules_trigger_type
    ON schedules (organization_id, trigger_type, status, enabled);

CREATE INDEX IF NOT EXISTS idx_schedules_webhook_active
    ON schedules (id, organization_id)
    WHERE trigger_type = 'webhook' AND enabled = true AND status = 'active';

COMMENT ON COLUMN schedules.trigger_type IS
    'Workflow trigger kind: schedule, manual, webhook, or integration_event.';
COMMENT ON COLUMN schedules.trigger_config IS
    'Structured trigger configuration. Time-based workflows mirror legacy schedule columns here.';
COMMENT ON COLUMN schedules.request_template IS
    'Request-level title/description/dependency template used when a workflow fires.';
COMMENT ON COLUMN schedules.actions IS
    'Ordered workflow actions. Each task action becomes a task in the request framework.';
COMMENT ON COLUMN schedules.review_instructions IS
    'Reviewer-facing checklist appended to workflow-created request descriptions.';
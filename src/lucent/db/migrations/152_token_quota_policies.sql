-- Migration 152: Token quota policies.
--
-- Background
-- ----------
-- `llm_token_usage` (108) has always been an append-only *observability*
-- ledger: it records provider-reported usage, but nothing ever read it back to
-- decide whether a request was allowed to run. Until now there was no way for
-- an administrator to cap how much of a model any person, group, or the whole
-- organization could consume.
--
-- This migration adds the configuration half of a quota system. Enforcement
-- lives in application code (`lucent.llm.token_quota`), which reads these rows
-- and aggregates the existing ledger — no new usage instrumentation is needed
-- because every recorder already stamps `organization_id`, `user_id`, `model`
-- and `created_at` onto each row.
--
-- Two tables
-- ----------
-- `token_quota_policies` is one budget definition: a set of models, a rolling
-- window, three independent limits, and a sharing mode.
--
--   * `model_ids` is the list of models.id values the policy governs. The
--     sentinel `'*'` means every model, and a policy may instead name any
--     number of specific models ("everything we run on Anthropic", "the two
--     models the support agents use"). An empty list is rejected: a policy has
--     to say what it covers. It is a `TEXT[]` rather than a join table because
--     the list is small, always read whole with the policy, and never queried
--     on its own; a child table would add a join to the hot pre-request path
--     for no benefit.
--   * Three separate limits because the three token classes are priced and
--     throttled differently by providers: `raw_input_tokens_limit` counts
--     uncached prompt tokens, `cached_input_tokens_limit` counts prompt tokens
--     served from a provider prompt cache, `output_tokens_limit` counts
--     generated tokens. Each is NULLable, and NULL means *unbounded* for that
--     dimension. All three NULL is rejected by a CHECK: a policy that limits
--     nothing is a configuration error, not a no-op.
--   * `scope_mode` is the sharing model:
--       - `global`     one shared pool for every principal the policy is
--                      assigned to (the whole org, one group, or one person),
--       - `individual` one pool *per person* in the assigned principal, so each
--                      member independently gets the full allowance.
--   * `window_seconds` is a rolling window measured back from NOW(), not a
--     calendar boundary. Rolling is what makes the check correct at any moment
--     without a scheduled reset job, and it cannot be gamed by bursting across
--     a midnight boundary.
--
-- `token_quota_assignments` is the many-to-many between a policy and the
-- principals it applies to. A policy usually has several: "these models, 5M
-- output tokens/day, shared" assigned to the org *and* "50M/day, individual"
-- assigned to the engineering group. Every applicable policy is enforced, so
-- the effective ceiling is the tightest one a principal falls under.
--
-- `principal_type` / `principal_id` are a deliberate polymorphic pair rather
-- than three nullable FKs, because the three cases have different id domains:
--
--   * `org`   — `principal_id IS NULL`; the whole organization in `organization_id`.
--   * `group` — `principal_id` is groups.id (org-scoped through the group row).
--   * `user`  — `principal_id` is users.id.
--
-- A CHECK keeps the pair consistent, and a partial unique index prevents the
-- same policy being assigned to the same principal twice.
--
-- Rollout safety
-- --------------
-- Additive only: both tables start empty, so this migration changes no existing
-- behavior. With no policies present the pre-request check does one indexed
-- lookup and returns "allowed". Enforcement is additionally gated behind the
-- `token_limits.enabled` runtime setting.

CREATE TABLE IF NOT EXISTS token_quota_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- models.id values, or the single sentinel '*' for every model. Not an FK:
    -- '*' is a legal element, a policy covering several models cannot be one
    -- FK, and a policy is allowed to outlive a transiently undiscovered model.
    model_ids TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[],
    name VARCHAR(128) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- Rolling window measured back from NOW().
    window_seconds INTEGER NOT NULL DEFAULT 86400,
    scope_mode VARCHAR(16) NOT NULL DEFAULT 'individual',
    -- NULL = unbounded in that dimension.
    raw_input_tokens_limit BIGINT,
    cached_input_tokens_limit BIGINT,
    output_tokens_limit BIGINT,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE token_quota_policies
    DROP CONSTRAINT IF EXISTS token_quota_policies_scope_mode_check;
ALTER TABLE token_quota_policies
    ADD CONSTRAINT token_quota_policies_scope_mode_check
    CHECK (scope_mode IN ('global', 'individual'));

ALTER TABLE token_quota_policies
    DROP CONSTRAINT IF EXISTS token_quota_policies_window_seconds_check;
ALTER TABLE token_quota_policies
    ADD CONSTRAINT token_quota_policies_window_seconds_check
    CHECK (window_seconds >= 60);

-- A policy must say which models it covers. An empty list would silently match
-- nothing, which reads in the UI as a limit that is not working.
ALTER TABLE token_quota_policies
    DROP CONSTRAINT IF EXISTS token_quota_policies_model_ids_present;
ALTER TABLE token_quota_policies
    ADD CONSTRAINT token_quota_policies_model_ids_present
    CHECK (cardinality(model_ids) > 0 AND array_position(model_ids, '') IS NULL);

-- Negative limits are meaningless; zero is legal and means "no usage at all"
-- (a hard stop that is genuinely useful for suspending access to a model
-- without deleting the policy or revoking model access).
ALTER TABLE token_quota_policies
    DROP CONSTRAINT IF EXISTS token_quota_policies_limits_non_negative;
ALTER TABLE token_quota_policies
    ADD CONSTRAINT token_quota_policies_limits_non_negative
    CHECK (
        (raw_input_tokens_limit IS NULL OR raw_input_tokens_limit >= 0)
        AND (cached_input_tokens_limit IS NULL OR cached_input_tokens_limit >= 0)
        AND (output_tokens_limit IS NULL OR output_tokens_limit >= 0)
    );

-- A policy that constrains nothing would silently do nothing, which reads as a
-- broken limit in the UI. Reject it at the database instead.
ALTER TABLE token_quota_policies
    DROP CONSTRAINT IF EXISTS token_quota_policies_some_limit_required;
ALTER TABLE token_quota_policies
    ADD CONSTRAINT token_quota_policies_some_limit_required
    CHECK (
        raw_input_tokens_limit IS NOT NULL
        OR cached_input_tokens_limit IS NOT NULL
        OR output_tokens_limit IS NOT NULL
    );

-- One policy name per organization. Model scope no longer belongs in the key:
-- a single policy can now span models, so "Daily allowance" over three models is
-- one row, and a second row with that name would be an accidental duplicate
-- rather than a legitimate per-model budget.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_token_quota_policies_org_name
    ON token_quota_policies (organization_id, name);

-- Resolution filters by organization and enabled state first; the model test is
-- an array membership check over the handful of rows that survive.
CREATE INDEX IF NOT EXISTS idx_token_quota_policies_org
    ON token_quota_policies (organization_id)
    WHERE is_enabled;

CREATE TABLE IF NOT EXISTS token_quota_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id UUID NOT NULL
        REFERENCES token_quota_policies(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    principal_type VARCHAR(16) NOT NULL,
    principal_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE token_quota_assignments
    DROP CONSTRAINT IF EXISTS token_quota_assignments_principal_type_check;
ALTER TABLE token_quota_assignments
    ADD CONSTRAINT token_quota_assignments_principal_type_check
    CHECK (principal_type IN ('org', 'group', 'user'));

-- The org assignment carries no id (the org is already `organization_id`); the
-- other two must name one. A half-specified principal would otherwise resolve
-- to "org" by accident and apply far more broadly than the admin intended.
ALTER TABLE token_quota_assignments
    DROP CONSTRAINT IF EXISTS token_quota_assignments_principal_pairing;
ALTER TABLE token_quota_assignments
    ADD CONSTRAINT token_quota_assignments_principal_pairing
    CHECK (
        (principal_type = 'org' AND principal_id IS NULL)
        OR (principal_type <> 'org' AND principal_id IS NOT NULL)
    );

-- One assignment per (policy, principal). COALESCE folds the NULL org id into
-- the expression so the uniqueness claim is total.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_token_quota_assignments_principal
    ON token_quota_assignments (
        policy_id, principal_type, COALESCE(principal_id, '00000000-0000-0000-0000-000000000000'::uuid)
    );

CREATE INDEX IF NOT EXISTS idx_token_quota_assignments_policy
    ON token_quota_assignments (policy_id);
CREATE INDEX IF NOT EXISTS idx_token_quota_assignments_org_principal
    ON token_quota_assignments (organization_id, principal_type, principal_id);

-- Enforcement aggregates the ledger per (model, user) inside a rolling window.
-- The existing indexes lead with organization_id + user_id, which cannot serve
-- a model-filtered scan; this one can, and it stays selective on time.
CREATE INDEX IF NOT EXISTS idx_llm_token_usage_org_model_created
    ON llm_token_usage (organization_id, model, created_at);

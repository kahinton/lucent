-- Migration 078: First-class task/request output artifacts
-- Stores user-facing deliverables produced by tasks: PRs, issues, emails,
-- documents, files, deployments, and generic links/artifacts.

CREATE TABLE IF NOT EXISTS task_outputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    request_id UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    organization_id UUID NOT NULL REFERENCES organizations(id),
    created_by UUID REFERENCES users(id),

    output_type VARCHAR(32) NOT NULL DEFAULT 'link',
    provider VARCHAR(64),
    title VARCHAR(256) NOT NULL,
    description TEXT,
    url TEXT,
    external_id TEXT,
    mime_type VARCHAR(128),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_primary BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_task_outputs_type CHECK (
        output_type IN (
            'link', 'github_issue', 'github_pr', 'email', 'document',
            'file', 'memory', 'deployment', 'artifact', 'other'
        )
    ),
    CONSTRAINT ck_task_outputs_title_nonempty CHECK (length(trim(title)) > 0),
    CONSTRAINT ck_task_outputs_url_or_external CHECK (
        url IS NOT NULL OR external_id IS NOT NULL OR output_type = 'other'
    )
);

CREATE INDEX IF NOT EXISTS idx_task_outputs_task_created
    ON task_outputs(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_outputs_request_created
    ON task_outputs(request_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_outputs_org_type_created
    ON task_outputs(organization_id, output_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_outputs_primary_request
    ON task_outputs(request_id)
    WHERE is_primary IS TRUE;

COMMENT ON TABLE task_outputs IS
  'User-facing deliverables produced by tracked tasks: GitHub PRs/issues, sent emails, documents, files, deployments, and generic artifacts.';
COMMENT ON COLUMN task_outputs.output_type IS
  'Display/output classification: link, github_issue, github_pr, email, document, file, memory, deployment, artifact, other.';
COMMENT ON COLUMN task_outputs.provider IS
  'Optional integration/provider name such as github, gmail, google_docs, slack, notion, filesystem.';
COMMENT ON COLUMN task_outputs.external_id IS
  'Provider-native identifier such as PR number, message id, document id, deployment id, or file path.';

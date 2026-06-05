-- Migration 080: Definition proposal evidence
--
-- Lets autonomous learning propose agent/skill/hook/MCP changes with explicit
-- reviewer-facing rationale. Evidence is operational metadata, not memory.

ALTER TABLE agent_definitions
    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
    ADD COLUMN IF NOT EXISTS proposal_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE skill_definitions
    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
    ADD COLUMN IF NOT EXISTS proposal_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE hook_definitions
    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
    ADD COLUMN IF NOT EXISTS proposal_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS proposal_reason TEXT,
    ADD COLUMN IF NOT EXISTS proposal_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN agent_definitions.proposal_reason IS
  'Human-readable reason this definition was proposed, especially for audit-driven learning changes.';
COMMENT ON COLUMN agent_definitions.proposal_evidence IS
  'Structured evidence behind a proposed definition change; operational metadata, not memory content.';
COMMENT ON COLUMN skill_definitions.proposal_reason IS
  'Human-readable reason this skill was proposed, especially for audit-driven learning changes.';
COMMENT ON COLUMN skill_definitions.proposal_evidence IS
  'Structured evidence behind a proposed skill change; operational metadata, not memory content.';
COMMENT ON COLUMN hook_definitions.proposal_reason IS
  'Human-readable reason this hook was proposed, especially for audit-driven learning changes.';
COMMENT ON COLUMN hook_definitions.proposal_evidence IS
  'Structured evidence behind a proposed hook change; operational metadata, not memory content.';
COMMENT ON COLUMN mcp_server_configs.proposal_reason IS
  'Human-readable reason this MCP server was proposed.';
COMMENT ON COLUMN mcp_server_configs.proposal_evidence IS
  'Structured evidence behind a proposed MCP server change; operational metadata, not memory content.';

-- Migration 158: Widen managed-tool runtime kinds.
--
-- Until now `managed_tool_definitions.runtime_type` admitted exactly one
-- value, `python`: Lucent-authored source code executed inside a sandbox
-- container. That is the most powerful kind and the most expensive one, and it
-- is the only reason a deployment without sandboxes could offer no tools of its
-- own at all.
--
-- Every other agent platform ships a spread of declarations that differ in
-- *who executes* and *how much trust they require*:
--
--   * OpenAI / Anthropic / Gemini all accept a declarative JSON-Schema function
--     whose body is a plain HTTPS call (the "actions"/"action group" shape);
--     nothing has to be compiled or sandboxed.
--   * The same vendors generate whole tool catalogs from an OpenAPI document.
--   * Copilot, Claude Code, and Gemini ship *provider-native* tools (web
--     search, fetch, code execution) that the platform owns and the model can
--     never redefine.
--
-- Lucent's managed tools already carry the right security envelope — owner,
-- capability grant, per-user consent, input/output schema, credential
-- references, network policy, resource limits, run ledger. Only the *executor*
-- was single-valued. Widening the enum lets the same envelope describe a
-- declarative HTTP call, an OpenAPI operation, or a Lucent-native handler,
-- without inventing a second parallel permission model.
--
-- Values:
--   python  - sandboxed Python entrypoint (existing behaviour, unchanged)
--   http    - declarative request template; executed by the server, no code
--   openapi - one operation derived from an OpenAPI document
--   builtin - Lucent-native handler from a fixed, reviewed catalog
--
-- `runtime_config` carries the per-kind configuration (url/method/headers for
-- `http`, spec reference plus operation for `openapi`, handler name plus
-- options for `builtin`). No new columns: the contract that the model sees
-- (`input_schema`/`output_schema`) and the policy that guards execution
-- (`auth_policy`, `network_policy`, `env_vars`, `resource_limits`) are already
-- kind-agnostic.
--
-- `source_code` stays NOT NULL with an empty default because `python` needs it
-- and the column predates the other kinds; the repository validates that a
-- non-`python` tool leaves it empty rather than silently ignoring dead code.

ALTER TABLE managed_tool_definitions
    DROP CONSTRAINT IF EXISTS managed_tool_definitions_runtime_type_check;

ALTER TABLE managed_tool_definitions
    ADD CONSTRAINT managed_tool_definitions_runtime_type_check
    CHECK (runtime_type IN ('python', 'http', 'openapi', 'builtin'));

COMMENT ON COLUMN managed_tool_definitions.runtime_type IS
    'Executor kind: python (sandboxed source), http (declarative request), '
    'openapi (spec-derived operation), builtin (Lucent-native handler).';

COMMENT ON COLUMN managed_tool_definitions.runtime_config IS
    'Per-kind executor configuration. python: image/limits overrides. '
    'http: url/method/query/body/headers/response_path. openapi: spec/spec_url '
    'plus operation_id or path+method. builtin: handler plus options.';

-- Existing rows are unaffected: every row today is `python`.

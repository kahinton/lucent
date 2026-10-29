-- Migration 159: Per-user credential slots on MCP server definitions.
--
-- A remote MCP server that Lucent ships as a **built-in** connector (GitHub,
-- Sentry, Linear — a hosted instance we know the URL of) has to work for every
-- user without an administrator minting one definition per person. The endpoint
-- is shared; the *credential* is not. Each person supplies their own token, and
-- the shared definition resolves it for whoever is calling.
--
-- Two things were missing for that:
--
-- 1. **A declared credential slot.** `headers` already supports `secret://`
--    references, and those already resolve per calling user through the
--    clearance-driven secret store. What a shared definition could not express was
--    *which* references it needs, so the UI had nowhere to render "add your token"
--    and the runtime had no way to report "this needs setup" instead of failing
--    with a bare `KeyError` mid-turn.
--
-- 2. **A built-in flag.** `scope = 'built-in'` exists and already carries the right
--    properties for this: built-ins are consent-exempt, readable org-wide, and
--    protected from daemon mutation by ``BuiltInProtectionError``. Reusing it keeps
--    shipped connectors on the same ownership path as shipped skills and tools
--    rather than inventing a parallel notion of "shipped".
--
-- `credential_slots` is a JSON array, one object per required credential:
--
--   [{"name": "GITHUB_TOKEN",
--     "header": "Authorization",
--     "format": "Bearer {value}",
--     "label": "GitHub personal access token",
--     "help": "Create one at github.com/settings/tokens with repo scope.",
--     "required": true,
--     "secret_key": "github.mcp.token"}]
--
-- The runtime never reads `credential_slots` for authorization — it reads the
-- resolved row. The column exists so the *UI* can render a form and so a
-- composition step can tell "not configured yet" apart from "configured but
-- broken", which is the difference between an actionable message and a 500.
--
-- Nothing here changes an existing row: every current definition gets an empty
-- array, which means "no declared slots" and therefore no behaviour change.

ALTER TABLE mcp_server_configs
    ADD COLUMN IF NOT EXISTS credential_slots JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN mcp_server_configs.credential_slots IS
    'Credentials this definition needs, one object per slot '
    '({name, header, format, label, help, required, secret_key}). A secret:// '
    'reference in headers is resolved per calling user; this lists which ones so '
    'the UI can prompt and the runtime can report "needs setup".';

-- A built-in connector may be granted without an admin authoring it, but it still
-- has to be approvable: `scope = 'built-in'` rows are created already-active by
-- the sync, exactly like built-in skills and managed tools.

-- Reuse the existing org-shared index for built-in connectors, which are
-- org-shared with a NULL owner.
CREATE INDEX IF NOT EXISTS idx_mcp_cfg_builtin
    ON mcp_server_configs(organization_id, name)
    WHERE scope = 'built-in';

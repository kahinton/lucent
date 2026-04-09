# Security Audit Synthesis Report

**Date:** 2026-03-27
**Auditors:** Claude Opus 4.6 (Task 1), GPT-5.3 Codex (Task 2), Gemini 3.1 Pro (Task 3)
**Scope:** Full Lucent codebase — auth, RBAC, middleware, secrets, Docker, sandbox, dependencies

---

## Unified Findings Table

| ID | Severity | Title | Source(s) | File | Status |
|----|----------|-------|-----------|------|--------|
| C1 | Critical | Exposed GitHub Token in .env File | Gemini | .env | **Mitigated** — .env in .gitignore; token is local dev artifact |
| C2 | Critical | Docker Socket Mounted in Application Container | Gemini | docker-compose.yml | **Mitigated** — docker-compose.prod.yml uses docker-socket-proxy |
| C3 | Critical | Hardcoded Default Credentials Across Services | Gemini, GPT (related) | docker-compose.yml | **Mitigated** — prod compose uses `${VAR:?error}` requiring explicit values |
| H1 | High | OpenBao Dev Mode in Container | Gemini | docker-compose.yml | **Mitigated** — prod compose uses `server -config=` with file storage backend |
| H2 | High | Interactive TTY in Production-Ready Container | Gemini | docker-compose.yml | **Mitigated** — stdin_open/tty already disabled in dev compose |
| H3 | High | OpenBao Container Network Exposure | Gemini | docker-compose.yml | **Mitigated** — prod compose uses internal network, no exposed ports |
| H4 | High | Weak Base Images Without Security Scanning | Gemini | Dockerfile.dev | **Mitigated** — production Dockerfile pins SHA256 digests with multi-stage build |
| M1 | Medium | Rate Limit Bypass via Rotating Auth Headers | GPT | src/lucent/api/app.py | **Fixed** — rate key uses `f"api:{key_prefix}:{client_ip}"` |
| M2 | Medium | Temp Password Cookie Missing Secure Flag | GPT | src/lucent/web/routes/admin.py | **Fixed** — uses `SECURE_COOKIES` global (default `true`) |
| M3 | Medium | Daemon API Key Uses Broad Read/Write Scopes | GPT | daemon/daemon.py | **Open** — extracted to `DAEMON_KEY_SCOPES` constant with TODO |
| M4 | Medium | Known CVEs in Dependency Graph | GPT | pyproject.toml | **Open** — needs pip-audit + dependency updates |
| M5 | Medium | Auth System Findings (4 issues) | Claude | src/lucent/auth.py | **Unactionable** — Claude report truncated; needs re-review |
| L1 | Low | CORS Policy Broader Than Necessary | GPT | src/lucent/api/app.py | **Open** — allow_methods/headers could be restricted |
| L2 | Low | Login Limiter Uses Direct Socket IP | GPT | src/lucent/web/routes/auth.py | **Open** — should use shared get_client_ip() helper |
| L3 | Low | Internal Error Details Propagated to Clients | GPT | src/lucent/sandbox/mcp_bridge.py | **Open** — raw exceptions in JSON-RPC error responses |

---

## Cross-Model Analysis

### Agreement Across Models
- **Gemini and GPT** both flagged credential management (C3/M3) — Gemini at the Docker level, GPT at the API key scope level
- **All three models** confirmed auth foundations are sound (bcrypt, timing-safe comparisons, parameterized SQL)

### Model Strengths
- **Gemini 3.1 Pro** excelled at infrastructure/Docker security — found all 3 critical and 4 high findings related to container configuration
- **GPT-5.3 Codex** excelled at application-level security — found rate limit bypass, cookie issues, scope problems, and dependency CVEs
- **Claude Opus 4.6** provided the deepest auth analysis but report was truncated

### Confidence Assessment
- Critical/High findings (C1-C3, H1-H4): **High confidence** — infrastructure issues are objective and clearly present in dev compose
- Medium findings (M1-M4): **High confidence** — GPT provided structured JSON with file/line references
- Low findings (L1-L3): **Medium confidence** — minor hardening opportunities

---

## Remediation Evidence

### Critical Findings — All Mitigated

**C1 (Exposed Token):** `.gitignore` line 14 contains `.env`. Token is a local development artifact, not committed to version control.

**C2 (Docker Socket):** `docker-compose.prod.yml` lines 73-104 deploy `tecnativa/docker-socket-proxy` with restrictive ACLs (AUTH=0, SECRETS=0, EXEC=0, etc.). The lucent service connects via `DOCKER_HOST: tcp://docker-socket-proxy:2375` instead of direct socket mount.

**C3 (Hardcoded Credentials):** `docker-compose.prod.yml` uses `${VAR:?error}` syntax throughout:
- Line 30: `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}`
- Line 119: `DATABASE_URL` with required password
- Line 127: `LUCENT_SECRET_KEY: ${LUCENT_SECRET_KEY:?LUCENT_SECRET_KEY is required}`
- Line 130: `VAULT_TOKEN: ${VAULT_TOKEN:?VAULT_TOKEN is required}`

The dev compose file has a prominent 12-line header: "LOCAL DEVELOPMENT ONLY — NOT FOR PRODUCTION USE".

### High Findings — All Mitigated

**H1 (OpenBao Dev Mode):** `docker-compose.prod.yml` line 53 uses `server -config=/openbao/config/config.hcl`. Production config at `docker/openbao-prod-config.hcl` configures file storage backend and proper listener settings.

**H2 (Interactive TTY):** Dev compose lines 124-125 explicitly document that stdin_open/tty are disabled by default.

**H3 (OpenBao Network Exposure):** Prod compose uses internal Docker network (`networks: internal`) with no port mappings for OpenBao.

**H4 (Weak Base Images):** Production `Dockerfile` (not `Dockerfile.dev`) lines 4 and 23 pin `python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4`. Uses multi-stage build with non-root user.

### Medium Findings — Partially Addressed

**M1 (Rate Limit Bypass):** Already fixed in `app.py` line 359: `rate_key = f"api:{key_prefix}:{client_ip}"` — rotating bogus tokens still hit the same IP bucket.

**M2 (Cookie Secure Flag):** Already fixed in `admin.py` lines 197/256: `secure=SECURE_COOKIES` references global setting (default `true` in `auth_providers.py` line 38).

**M3 (Daemon Key Scopes):** Extracted to `DAEMON_KEY_SCOPES` constant in `daemon/daemon.py` with TODO for future narrowing. Requires MCP tool-level scope enforcement to safely restrict.

---

## Remaining Issues for Future Work

### Medium Priority

1. **M3 — Daemon Key Scopes**: `DAEMON_KEY_SCOPES = ["read", "write"]` grants full API access. Narrowing requires implementing scope checks in MCP tool handlers. Track as future security hardening.

2. **M4 — Dependency CVEs**: `pip-audit` reported 5 vulnerabilities in 4 packages (requests, pyasn1, and tooling packages). Run `pip-audit --fix` and update constraints.

3. **M5 — Claude Auth Findings**: Claude Opus report was truncated at "4 findings includi…". Re-run a focused auth audit to capture the complete findings.

### Low Priority

4. **L1 — CORS Configuration**: Restrict `allow_methods` and `allow_headers` from `["*"]` to only what the frontend requires.

5. **L2 — Login Rate Limiter IP**: Switch `request.client.host` to shared `get_client_ip()` helper for proxy-aware IP extraction.

6. **L3 — Error Information Disclosure**: Sanitize exception messages in `mcp_bridge.py` JSON-RPC error responses to prevent leaking internal details.

# Session Management Architecture — Technical Mapping

**Scope:** Web UI session authentication (login → token → per-request validation → logout).
**Status:** Pre-change investigation for the multi-session-login request. Read-only; no code modified.
**Search path:** `/app/src` (host working tree mounted into daemon-1). Investigation date: 2026-09-08.
**Live DB checks:** via read-only `db_query` tool (read-only SQL session enforced five ways). No writes anywhere.

---

## 1. Token issuance at login

| Step | Location | What happens |
|---|---|---|
| Endpoint (GET) | `lucent/web/routes/auth.py:138-155` (`login_page`) | Renders `login.html`; first-run → `/setup`; if a *valid* session cookie is already present → redirect `/` (`auth.py:146-153`) |
| Endpoint (POST) | `lucent/web/routes/auth.py:159-229` (`login_submit`) | CSRF check → per-IP rate limit (`auth.py:169`) → `provider.authenticate(credentials)` (`auth.py:184`) → `create_session(pool, user["id"])` (`auth.py:208`) → set cookie → 303 → `/` |
| Credential check | `lucent/auth_providers.py:195-249` (`BasicAuthProvider.authenticate`) | bcrypt verify + `organization_allows_access`; `update_last_login` |
| Token minting | `lucent/auth_providers.py:312-314` (`generate_session_token`) | `secrets.token_urlsafe(48)` — cryptographically random, no JWT/state; **all session state is DB-side** |
| Hashing | `lucent/auth_providers.py:317-319` (`hash_session_token`) | SHA-256 hex of raw token (deliberate: sessions are ephemeral, comment at :318) |
| Persistence | `lucent/auth_providers.py:322-344` (`create_session`) | `UPDATE users SET session_token = $1, session_expires_at = $2 WHERE id = $3` (SQL at :336-340). **No sessions table. No INSERT.** |

The raw token exists only in the login response `Set-Cookie` header; the DB stores only the SHA-256
hash. **This UPDATE is the single point where one login destroys the previous session** — the
`WHERE id = $3` matches the user row and overwrites the only `session_token` slot.

Cookie details: `set_cookie(SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_HOURS*3600, **get_cookie_params())`
at `auth.py:219-225`; name `lucent_session`, TTL default 24 h (env `LUCENT_SESSION_TTL_HOURS`,
`auth_providers.py:30`); cookie params `auth_providers.py:115-131` (HttpOnly/SameSite/Secure).
Fresh CSRF cookie minted per response (`auth.py:226-227`).

## 2. Where single-active-session behavior is enforced

**Mechanism: a single `session_token` column on the `users` row — candidate (a) of the three
hypothesized designs.** There is no sessions table and no in-memory store.

### Schema (confirmed in migration and live DB)
- Migration: `lucent/db/migrations/011_add_password_auth.sql:15-25` —
  `ALTER TABLE users ADD COLUMN IF NOT EXISTS session_token TEXT` (:15) +
  `session_expires_at TIMESTAMPTZ` (:16) + partial index `idx_users_session_token` (:19-20).
- Live DB (read-only query, 2026-09-08): `users` has exactly `session_token text` and
  `session_expires_at timestamp with time zone`. No `sessions` table exists (all
  `%session%`/`%token%` tables are LLM-conversation tables: `llm_sessions`,
  `llm_session_events`, `llm_session_requests`, `llm_token_usage`).

### Enforcement points (file:line)
1. **Mint overwrites:** `create_session` — `lucent/auth_providers.py:322-344`; the destructive
   UPDATE is `:336-340` (`SET session_token=$1 ... WHERE id=$3`). Second login sets a new
   SHA-256 hash, so the first device's cookie no longer matches any row.
2. **Validation is equality-to-the-canonical-hash:** `validate_session` —
   `lucent/auth_providers.py:347-380`; SQL `WHERE session_token = $1 AND session_expires_at > NOW()
   AND is_active = true` at `:362-372` (the `WHERE session_token = $1` is `:367`). After the
   overwrite, session A's hash matches nothing → `fetchrow` → `None` → 303 to `/login`
   (`_shared.py:296-307`). No token-list comparison — one column, one hash.
3. **Logout is user-scoped, not token-scoped:** `destroy_session` —
   `lucent/auth_providers.py:400-412` (`UPDATE users SET session_token = NULL ... WHERE id = $1`,
   SQL `:407-410`); called from `POST /logout` at `lucent/web/routes/auth.py:231-248` — it nulls
   the sole slot for the whole user, so under multi-session it would kill *every* device.
4. **Session-bound features that read the same slot** (secondary couplings, not enforcement):
   - Impersonation start regenerates the session in place:
     `lucent/web/routes/admin.py:314-338` (`create_session` at :316, cookie reset :332-338) —
     session-fixation defense; rebinds `lucent_impersonate` cookie via
     `sign_value(f"{user_id}:{session_hash}")` :340.
   - Impersonation validity is *bound to the current session hash*:
     `lucent/web/routes/_shared.py:336-338` compares the cookie's embedded
     `hash_session_token(session_token)` against the live session.
   - Password changes rotate the slot: `destroy_session` + `create_session` at
     `lucent/web/routes/settings.py:303-305` and `:393-395`; admin password reset nukes the
     session (`auth_providers.py:485-486`).

Every authenticated surface funnels through the two validators above: web dependency
`get_user_context` (`_shared.py:277-378`, validates via `validate_session` :298), API middleware
badge check (`lucent/api/app.py:408-419`), and SSE endpoints
(`lucent/api/routers/chat.py:23,156`). MCP transport uses a separate `mcp_session_manager`
(`lucent/server.py:427,449`; `lucent/api/app.py:46-51`) that does not touch `users.session_token`
— MCP/API-key auth is unaffected by any of this.

### Why each alternative mechanism is ruled out
- **One-row-per-user sessions table:** no such table exists (live DB enumeration above).
- **In-memory store keyed by user_id:** `create_session`/`validate_session` are pure SQL against
  `users`; no module-level session dict exists in `auth_providers.py`.

## 3. Client-side token storage and login-screen assumptions

**Token storage = HttpOnly cookie `lucent_session`.** No client script touches the session token:
- App templates/statics contain no `localStorage`/`sessionStorage` token code — the only
  `localStorage` use is sidebar collapse state (`base.html:42,604`); the htmx vendor bundle's
  `localStorage` use is its internal history cache. `sessionStorage` is unused.
- CSRF uses the double-submit pattern (`lucent_csrf` cookie + hidden form field,
  `auth_providers.py:35-37`; checked at `POST /login` via `_check_csrf`).
- Live SSE (`/ui/events`, `base.html:708-712`) rides browser cookie auth automatically — no
  token hand-off in JS, so an SSE stream's auth lifetime == cookie validity.

**Login-screen single-session assumptions:**
1. **Copy:** _Removed 2026-09-08 — login no longer shows a session warning._ Historically
   `_render_login_page` injected a `session_warning` context variable (`auth.py:54`), rendered as
   a banner in `login.html` (`{% if session_warning %}`); the mechanism existed only on the login
   screen and was fully removed once it served no purpose.
2. **No token-swapping logic in the browser:** the form is a plain `POST /login`
   (`login.html:119-120`); the server's 303 + `Set-Cookie` does the swap. No JS redirect/cache
   handling tied to sessions.
3. **"Already logged in" redirect** (`auth.py:146-153`): with multiple sessions this just means
   "this browser has a valid cookie" — harmless, semantics unchanged.
4. **Adjacent copy that will need review** (multi-session semantics change):
   - `settings/danger.html:10` — "Invalidates every active session for your account on all
     devices" (currently true *by construction*; stays true if "log out everywhere" is kept).
   - `settings/account.html:79` — "Changing your password will sign you out of other sessions"
     (currently true by construction — one slot).
   - `POST /logout` (`auth.py:231-248`) currently ends **all** sessions for the user via
     `destroy_session(user_id)` — under multi-session this must become per-device.

## 4. Change-surface summary for the multi-session fix

Minimal correct shape (for the implementing task — not part of this investigation):
- New `login_sessions` table (one row per session: `token_hash`, `user_id`, `created_at`,
  `expires_at`, optional `last_seen_at`/UA) replacing the `users.session_token` column;
  `create_session` → INSERT, `validate_session` → SELECT by token_hash (+ expiry + is_active),
  `destroy_session` → DELETE by token_hash (per-device), plus a user-wide delete for
  "log out everywhere" / password-reset.
- Coupled code to re-point: the 5 `create_session` call sites (`auth.py:208`, `auth.py:351`
  (setup), `admin.py:316`, `settings.py:305`, `settings.py:394`), impersonation session-hash
  binding (`_shared.py:336-338`, `admin.py:340`), admin password reset
  (`auth_providers.py:485-486`), password-change copy (`settings/account.html:79`,
  `danger.html:10`), and the login warning string (`auth.py:54` → drop/reword).
- Remember-me/TTL: none exists beyond `SESSION_TTL_HOURS` (`auth_providers.py:30`) applied at
  mint time (`auth_providers.py:334`); expiry is checked in `validate_session` SQL
  (`auth_providers.py:368`). No entanglement with single-session enforcement — a sessions-table
  design preserves this unchanged.
- Schema change ⇒ **DB backup first** per standing data-safety policy; backup location must be
  recorded in the implementing task's output.
- SSE regression gate: session A's `validate_session` hit must not depend on any per-user slot a
  session-B login could touch; with a sessions table this is satisfied structurally.

---

## 5. Implementation addendum — multi-session login (2026-09-08)

The fix above was implemented in this working tree (same request cycle). Supersedes the
single-session behavior described in sections 1–4, which are retained as the historical mapping.

### Schema
- **New:** `lucent/db/migrations/110_multi_session_login.sql` — creates `user_sessions`
  (`token_hash TEXT PRIMARY KEY`, `user_id UUID NOT NULL REFERENCES users ON DELETE CASCADE`,
  `created_at`, `expires_at NOT NULL`, `last_seen_at`), indexes on `user_id` and `expires_at`,
  carries existing `users.session_token` rows forward (no one is logged out by the migration),
  then drops `users.session_token` / `users.session_expires_at`. Applied by the server's own
  startup migration runner — no manual DDL.
- Pre-migration DB backup per standing policy:
  **`pre_multi_session-20260908-154238.dump`** (349,191,974 bytes) in `/backups`
  (host `./backups`), verified via size match with the known-good snapshot family.

### Behavior changes
- `create_session` (`auth_providers.py`) → INSERT into `user_sessions`. N concurrent sessions
  per user; a new login never touches other sessions.
- `validate_session` → JOIN `user_sessions`/`users` by token hash + expiry + `u.is_active`,
  then `organization_allows_access`; updates `last_seen_at` on each successful validation.
- `destroy_session` (new signature: `(pool, token) -> bool`) → DELETE by token hash = per-device
  logout. `POST /logout` (`auth.py`) uses it directly; other devices stay signed in.
- **New** `destroy_all_user_sessions(pool, user_id, *, except_token=None)` → user-wide delete,
  used by "sign out all sessions" (`/settings/sessions/revoke-all`), admin password reset, and
  password changes (which keep the acting device via `except_token`).
- **New** `rotate_session(pool, token) -> str | None` → in-place token rotation for the same
  device (session-fixation defense + password changes).
- Impersonation start (`admin.py`) rotates the current device's session in place instead of
  minting a second session; `lucent_impersonate` binding semantics unchanged (session-hash
  comparison in `_shared.py` works against the same rotated token).
- Self-service password change (`settings.py`) keeps the current device signed in (rotated
  token, falls back to `/login` if the cookie is absent/invalid) and revokes all other devices.
  Forced password change: same pattern. Success copy updated accordingly.
- Login screen: session warning removed entirely 2026-09-08 (nothing warns or re-assures at
  login; multi-session behavior is simply silent). No other client-side token logic exists to
  change (HttpOnly cookie, double-submit CSRF — see section 3).
- `settings/account.html` ("Changing your password will sign you out of other sessions") and
  `settings/danger.html` ("Invalidates every active session ... on all devices") remain accurate.

### SSE / API regression surface
Validation now keys on the `user_sessions` row only; no per-user slot exists for a second login
to disturb. Session A's SSE streams (chat, hook chips, context-usage polling) validate against
their own token hash and are structurally unaffected by a login on session B. API-key auth and
the MCP `mcp_session_manager` remain untouched.
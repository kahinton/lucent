# Access Control Architecture

> **Status:** Authoritative reference. This document defines how authorization
> ("who may see, use, modify, and grant a thing") works across every Lucent
> surface — REST API, web UI, MCP tools, and the daemon. Treat it as the design
> contract for any new resource type or access-bearing feature.
>
> **Companion docs:** [security-model.md](security-model.md) (authentication,
> identity, org isolation, secrets), [api-reference.md](api-reference.md).

---

## 1. Principles

1. **One plane, one predicate.** There is exactly one place that encodes the rule
   "can principal P access resource R": `AccessControlService` in
   [../src/lucent/access_control.py](../src/lucent/access_control.py). Every
   surface and every repository delegates to it. No layer hand-writes ownership
   SQL.
2. **Fail closed.** Unknown resource type, missing role, NULL identity, or a row
   that does not resolve → **deny**. Never error-leak, never default-allow.
3. **Identity comes from the authenticated principal, never the request body.**
   The actor is whoever the auth token says they are. Resource IDs in the body
   are untrusted input.
4. **Org isolation is non-negotiable.** Every access predicate is scoped
   `AND organization_id = $org`. A cross-org ID resolves to *deny*, not to a
   leak or a 500.
5. **Owners, actors, and grantors are distinct.** See §3. We always record *who
   was given access* separately from *who had the authority to give it*.
6. **Mismatches are surfaced, attributed, and rectifiable — never silent.** See
   §5.
7. **Autonomous agents cannot self-escalate.** Scoped API-key contexts (daemon
   sub-agents) may propose, but may not approve, grant, or hot-patch active
   definitions.

---

## 2. The Access Model

Access is governed by **two complementary planes**:

- **The grant plane** (`resource_access_grants`) — an explicit many-to-many map
  of *who may use a resource*. This is the source of truth for **read/use
  visibility**.
- **The ownership column** (`owner_user_id`) — records *who manages a resource*
  (may edit it and administer its grants). This drives **write/manage**
  authority, not visibility.

> **Default-deny.** A newly created instance resource is visible only to its
> owner (and org admins/owners) until an explicit grant is added. Sharing is
> always an affirmative act, never a side effect of a NULL column.

### 2.1 The grant table

Every shareable resource type uses **one** uniform table,
`resource_access_grants`:

| Column | Meaning |
|---|---|
| `organization_id` | Owning org. Every grant is org-scoped; cross-org rows never match. |
| `resource_type` | Canonical token (`agent`, `skill`, `mcp_server`, `hook`, `managed_tool`, `sandbox_template`, `workflow`, `model`, `secret`). |
| `resource_id` | **TEXT** — covers both UUID-keyed resources and string-keyed models (the global model catalog uses provider-slug ids). |
| `principal_type` | `user`, `group`, or `org`. |
| `principal_id` | The user id, group id, or — for `org` grants — the organization id. |
| `granted_by`, `granted_at` | Grantor attribution. |

`UNIQUE (resource_type, resource_id, principal_type, principal_id)` makes grants
idempotent. An **`org` grant is the "everyone in the org" special case** — it is
just another principal in the same mechanism, not a separate flag.

### 2.2 The ownership column

`owner_user_id` (and the legacy `owner_group_id`, now NULL-on-migrate) identifies
the **manager** of an instance resource. The owner can always see and modify the
resource and administer its grants, even with no self-grant. `scope = 'built-in'`
still marks platform-shipped rows that are globally visible and immutable to
non-owners.

| State | `scope` | Visible to | Manageable by |
|---|---|---|---|
| **Built-in** | `built-in` | Everyone in the org | admins/owners only |
| **Owned (default)** | `instance` | Owner + admins/owners | Owner + admins/owners |
| **Shared to users/groups** | `instance` | Owner + admins + each granted user/group | Owner + admins/owners |
| **Shared to org** | `instance` | Everyone in the org (via an `org` grant) | Owner + admins/owners |

### 2.3 The canonical resolution order

`can_access` resolves in this order, short-circuiting on the first match:

```
built-in?  → allow
owner_user_id == principal?  → allow
a matching grant exists?  → allow
   (org grant for the principal's org, OR a user grant for the principal,
    OR a group grant for one of the principal's groups; all org-scoped)
principal role ∈ {admin, owner}?  → allow
otherwise  → deny
```

`can_modify` is independent of grants — **grants confer use, never management**:

```
role ∈ {admin, owner}?  → allow (any resource in the org)
owner_user_id == principal?  → allow
otherwise  → deny
```

> A user granted access to a resource may **use** it but cannot edit it or
> re-share it. Only the owner and org admins/owners manage grants. This
> separation of *use* from *management* is intentional and must be preserved.

### 2.4 The single predicate

The clause above is produced by **one** function,
`build_access_clause(...)` in
[../src/lucent/access_control.py](../src/lucent/access_control.py). Both
`AccessControlService` and `DefinitionRepository` build their `WHERE` fragments
from it. It emits an `EXISTS` sub-query against `resource_access_grants`
(org-scoped, matching `org`/`user`/`group` principals) plus the owner and
built-in short-circuits. **Do not hand-write the owner/grant predicate anywhere
else.** A guard test (`tests/test_access_clause_single_source.py`) fails the
build if that pattern appears outside `access_control.py`.

---

## 3. Actors, Owners, and Grantors

A grant attaches a **capability** to an **actor**. Three roles exist for every
grant; we record all three.

| Role | Definition | Stored as |
|---|---|---|
| **Owner** | Controls the capability and decides who else may use it. | `owner_user_id` on the resource, plus its `resource_access_grants` rows. |
| **Actor** | The entity that has been *given* use of the capability (e.g. an agent that holds a skill). | The `agent_id` side of a junction row. |
| **Grantor** | The principal who performed the grant, and under what authority. | `granted_by`, `granted_at`, `grant_reason`, `grant_override` on the junction row. |

### 3.1 Grant junction tables

`agent_skills`, `agent_mcp_servers`, `agent_hooks`, `agent_managed_tools` each
carry grantor metadata:

| Column | Meaning |
|---|---|
| `granted_by` | User who performed the grant. NULL = legacy/system (pre-grant-metadata). |
| `granted_at` | When the grant happened. |
| `grant_reason` | Optional human/agent-supplied justification. |
| `grant_override` | `true` when the grant was a deliberate scope-mismatch override (§5). |

### 3.2 Authorization rule for every grant

A grant of capability `C` to actor `A` by grantor `G` is permitted **only when
all** hold:

1. `can_modify(G, A)` — the grantor may modify the actor (you cannot wire
   capabilities into an agent you do not control).
2. `can_access(G, C)` — the grantor can see/use the capability (you cannot grant
   a thing you cannot see; prevents confused-deputy leakage).
3. `check_grant_compatibility(A, C)` is `OK`, **or** the grantor explicitly
   passes `override=True` with a reason (§5).
4. The grantor is an **unscoped human** context (not a scoped API key).

All four checks live in `AccessControlService.authorize_grant(...)`. Every
surface (REST, web, MCP, daemon composition) calls it. Repository `grant_*`
functions are **plumbing only** — they persist rows and emit audit events; they
do not decide policy.

---

## 4. Enforcement Topology

Three surfaces, **one** decision point.

```
REST router ─┐
Web route   ─┼─→ AccessControlService ─→ build_access_clause ─→ DB
MCP tool    ─┤        (can_access / can_modify /
Daemon      ─┘         authorize_grant / check_grant_compatibility)
```

| Surface | Read gate | Write gate | Grant gate |
|---|---|---|---|
| REST API | `can_access` / repo list filtered by predicate | `can_modify` | `authorize_grant` |
| Web UI | same | `can_modify` | `authorize_grant` |
| MCP tools | same | `_can_modify_definition` → `can_modify` | `authorize_grant` (+ unscoped-human check) |

**Role-only checks (`role in {admin, owner}`) are NOT a substitute for a
resource-level check.** `_require_admin` is reserved for genuinely org-global
actions (e.g. approving a proposal, toggling org-wide model availability), and
must be documented as such at each call site.

### 4.0 Managing resource access grants

Editing the `resource_access_grants` plane (the per-user/group/org "who may use
this" rows) is exposed identically on all three surfaces, each gated by
`can_modify` (managing owner or org admin/owner only) and **blocked for scoped
API-key contexts** (daemon sub-agents cannot self-escalate):

| Surface | Entry points |
|---|---|
| Web UI | `POST /access/{resource_type}/{resource_id}/grant\|revoke` ([access_ui.py](../src/lucent/web/routes/access_ui.py)) + the `_access_panel.html` partial on every resource detail page |
| REST API | `GET/POST /api/access-grants/{resource_type}/{resource_id}` and `.../grant`, `.../revoke` ([resource_access.py](../src/lucent/api/routers/resource_access.py)) |
| MCP tools | `list_resource_access`, `grant_resource_access`, `revoke_resource_access` ([definitions.py](../src/lucent/tools/definitions.py)) |

All three call `AccessControlService.grant_access` / `revoke_access` /
`list_access_grants`, validate the principal belongs to the caller's org
(`principal_exists_in_org`), and confer **use, not management**. Note this axis
is distinct from the capability-junction grant gate (`authorize_grant`, §3),
which wires a capability *into an actor* (e.g. a skill into an agent).


### 4.1 Anti-spoofing & TOCTOU

- The principal's `user_id`, `role`, and `organization_id` come from the
  authenticated token only. Tests in `tests/test_acl_enforcement.py`
  (`TestAntiSpoofing`) enforce this and must keep passing.
- Accessibility checks and the mutating write should share a transaction where
  feasible, to avoid time-of-check/time-of-use gaps.

### 4.2 Model selection enforcement (use-time)

Listing models already respects grants (`ModelRepository.list_models` filters by
the requester). But validating a model name (`validate_model` in
`model_registry.py`) is intentionally user-agnostic — it only checks existence
and tool support. To stop a user from *selecting* a model they cannot use, every
selection site additionally calls
[`enforce_model_access`](../src/lucent/access_control.py) **after**
`validate_model`, keyed on the **requesting** user's grants:

```python
error = await enforce_model_access(
    pool, user_id=..., role=..., org_id=..., model_id=model
)
# REST/web → HTTP 403; MCP → {"error": ...}
```

`enforce_model_access` returns `None` (allow) when the model id is empty, when
the model is **absent from the org's catalog** (deferred to `validate_model`, so
registry/DB desync degrades open rather than spuriously blocking), or when
`can_access_model` passes. Otherwise it returns a denial message. The daemon and
org admins/owners satisfy the `admin/owner` branch of `build_access_clause`, so
autonomous task model selection keeps broad access.

Wired selection sites: REST request task create / model-update / edit, schedule
create / update / workflow create, chat `chat_stream` and `chat_stream_v2`; MCP
`create_task` and `create_schedule`; web schedule edit and task edit. Internal
system paths (workflow-execution override clearing, session-experience
summarization) are deliberately exempt — they are not user selections.

### 4.3 Default-model fallback (avoiding self-inflicted 403s)

Enforcement is strict for **explicit** picks, but the *default* a user is handed
must always be one they can actually use — otherwise a member who lacks the
configured default would 403 on every chat message despite having other
accessible models. Two helpers keep defaults in reach:

- [`AccessControlService.accessible_model_ids`](../src/lucent/access_control.py)
  returns the enabled model ids the requester may use (same grant clause as
  `can_access_model`; admin/owner get all enabled via the role branch).
- [`get_default_model_id_for_user`](../src/lucent/model_registry.py) resolves the
  configured/preferred default when accessible, else the user's best accessible
  model by category, else `None` (the user has zero accessible models).

Chat applies this in `_resolve_chat_model_for_user`: an explicit `body.model` is
returned as-is and stays strictly enforced (a forbidden explicit pick still
403s), but when no model is chosen it resolves an accessible default — honoring a
stored session default when still accessible. If the user has **no** accessible
models, chat returns a single clear 403 ("No chat models are available to you…")
instead of a confusing per-message failure. The `/chat/models` picker is likewise
grant-filtered with a user-appropriate `default`. The daemon is unaffected: it
selects models as the owner and never calls this path.

---

## 5. Scope Compatibility (Mismatch Model)

### 5.1 Visibility breadth ordering

```
personal (1)  <  group (2)  <  org-wide (3)  <  built-in / global (4)
```

`resource_breadth(resource)` maps a resource's **widest grant** to one of these
ranks: an `org` grant (or `built-in`) is widest, then group, then a single user,
then owner-only.

### 5.2 The rule

For a grant of capability `C` to actor `A`:

| Condition | Verdict |
|---|---|
| `breadth(C) >= breadth(A)` | **OK** — everyone who can use `A` can also see `C`. |
| `breadth(C) < breadth(A)` | **MISMATCH** — `A` is broadly usable but transitively depends on a narrowly-owned `C`. Allowed only with an audited override. |
| `A` and `C` are owned by different, non-containing groups | **CROSS_GROUP** — treated as a mismatch requiring override. |

The dangerous case is the personal-skill-on-an-org-wide-agent: the agent looks
shareable, but only one person can see/maintain the capability it depends on.

### 5.3 Detection, display, rectification

- `check_grant_compatibility(A, C)` returns a structured verdict
  `{status, reason, suggested_fixes[]}` used identically by every surface.
- At grant time, a non-OK verdict is **blocked** unless `override=True` + reason
  is supplied; the override is recorded (`grant_override`, `grant_reason`) and
  audited.
- Standing mismatches are queryable (e.g. `GET /agents/{id}/access-warnings`) so
  the UI can render an "Access warnings" panel and the daemon can flag them.
- Four rectification actions are always offered: **promote** the capability to
  the actor's breadth, **narrow** the actor to the capability's breadth,
  **revoke** the grant, or **acknowledge/override** with a reason.

---

## 6. Resource Classification

Every table is exactly one of:

- **Access-controlled** — owned via `owner_user_id` and shared via
  `resource_access_grants`; gated by `AccessControlService`. *Agents, skills, MCP
  servers, hooks, managed tools, sandbox templates, workflows (schedules),
  models, secrets* (secrets have no `built-in` concept, so no `scope`).
- **Runtime artifact** — not a shareable capability; filtered by
  creator/org/group **visibility** (the same predicate, read-only), but does not
  participate in the grant model. *Requests, tasks, reviews, user interactions,
  LLM sessions.*
- **Exempt** — org-global config with no per-resource ownership, gated by role
  only, documented as such. *Runtime settings.*

The canonical registry lives in [../src/lucent/access_control.py](../src/lucent/access_control.py)
(`RESOURCE_TABLE_MAP` / `_TABLES_WITH_SCOPE` / `CANONICAL_RESOURCE_TYPE`). A guard
test fails the build if a table grows an `owner_user_id` column without being
registered.

---

## 7. Rules for Adding a New Access-Bearing Resource

When you introduce a new resource that users can own or share:

1. **Schema:** add `scope VARCHAR(16) NOT NULL DEFAULT 'instance'` (omit if the
   type has no built-in concept, e.g. secrets) and
   `owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL`. Sharing rows
   live in the shared `resource_access_grants` table — **do not** add per-table
   `owner_group_id` or bespoke share columns.
2. **Backfill:** `owner_user_id = created_by` for existing instance rows. If any
   rows were previously org- or group-visible, emit matching `org`/`group`
   grants into `resource_access_grants` so the migration is visibility-preserving
   (the default is otherwise deny).
3. **Register:** add the table to `RESOURCE_TABLE_MAP` / `_TABLES_WITH_SCOPE` and
   add its canonical token to `CANONICAL_RESOURCE_TYPE`.
4. **Gate:** route every read through the predicate and every write through
   `can_modify`. Never add inline ACL SQL.
5. **Grants:** expose the resource on the generic access surface
   (`access_ui.py` → `/access/{resource_type}/{resource_id}/grant|revoke`) and
   render `_access_panel.html` on its detail page. Grant mutations go through
   `AccessControlService.grant_access` / `revoke_access`.
6. **Tests:** extend `tests/test_acl_enforcement.py` and
   `tests/test_resource_ownership.py` to cover the new type (default-deny / user
   grant / group grant / org grant / cross-org / built-in).

---

## 8. Invariants (must always hold)

- `can_access` and `list_accessible` never disagree for the same principal +
  resource (property-tested).
- A new instance resource is visible only to its owner + admins until an
  explicit grant is added (default-deny).
- Grants confer **use only**; management stays with the owner and org
  admins/owners.
- Every grant is org-scoped; a cross-org `resource_id` never resolves.
- A grant always records a grantor (or an explicit legacy-NULL).
- A scope mismatch is never persisted without `grant_override = true` and a
  reason.
- Built-ins are immutable to non-owners and to the daemon.
- Every grant / revoke / override emits an attributable audit event.
- No ownership predicate exists outside `access_control.py`.

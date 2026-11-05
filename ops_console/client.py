"""HTTP client for the Lucent hyperadmin API.

The console deliberately owns no database access and no Lucent imports. Every
read and write goes through the server's public API, which means:

* the API is exercised the way the eventual hosted signup site will exercise it
  (this console is the test bed for that site);
* nothing here can drift from the server's authorization rules, because the
  server enforces them;
* a bug in the console cannot corrupt the database directly.

Error handling contract: every method raises :class:`LucentAPIError` with the
server's ``detail`` string when the API refuses. Callers show that message to the
operator verbatim — it is written for a human (e.g. "This is the organization's
only owner…").
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

#: Bound each request. Organization listing can be slow on a big instance, and
#: provisioning seeds every built-in definition, so this is generous but finite.
DEFAULT_TIMEOUT = 60.0

#: Retries for idempotent reads. Provisioning and other writes are never
#: retried: a retried create would either 409 or duplicate a tenant.
READ_ATTEMPTS = 2


class LucentAPIError(Exception):
    """A non-2xx response from the Lucent API."""

    def __init__(self, status_code: int, detail: str, *, path: str = ""):
        self.status_code = status_code
        self.detail = detail
        self.path = path
        super().__init__(f"{status_code} {path}: {detail}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


class LucentClient:
    """Thin async wrapper over the organization-facing Lucent API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout
        self._transport = transport

    # -- plumbing ---------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=self._timeout,
            transport=self._transport,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> Any:
        attempts = READ_ATTEMPTS if retry else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                async with self._client() as client:
                    response = await client.request(
                        method, path, json=json, params=params
                    )
            except (httpx.RequestError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise LucentAPIError(
                    0,
                    f"Cannot reach Lucent at {self.base_url}: {exc}",
                    path=path,
                ) from exc

            if response.is_success:
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text

            raise LucentAPIError(
                response.status_code,
                _extract_detail(response),
                path=path,
            )

        raise LucentAPIError(0, str(last_error), path=path)

    # -- instance ---------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Unauthenticated reachability probe.

        Routed through :meth:`_request` so an unreachable server surfaces as the
        same :class:`LucentAPIError` shape as every other call — the console's
        status page depends on that uniformity.
        """
        payload = await self._request("GET", "/api/health", retry=True)
        return payload if isinstance(payload, dict) else {"status": "unknown"}

    async def me(self) -> dict[str, Any]:
        """The operator identity behind the API key."""
        return await self._request("GET", "/api/users/me", retry=True)

    async def verify_key(self) -> bool:
        """Cheap proof that the key is valid (also fails for a suspended org)."""
        payload = await self._request("GET", "/api/auth/verify", retry=True)
        return bool(payload and payload.get("valid"))

    # -- organizations ----------------------------------------------------

    async def list_organizations(
        self,
        *,
        offset: int = 0,
        limit: int = 25,
        status: str | None = None,
        search: str | None = None,
        include_stats: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "offset": offset,
            "limit": limit,
            "include_stats": str(include_stats).lower(),
        }
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        return await self._request(
            "GET", "/api/organizations", params=params, retry=True
        )

    async def get_organization(
        self, organization_id: str, *, include_stats: bool = True
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/organizations/{organization_id}",
            params={"include_stats": str(include_stats).lower()},
            retry=True,
        )

    async def provision_organization(
        self,
        *,
        name: str,
        admin_email: str | None,
        admin_display_name: str | None = None,
        admin_password: str | None = None,
        admin_role: str = "owner",
        api_key_name: str | None = "Default API key",
        model_id: str | None = None,
        providers: list[dict[str, str]] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a tenant, its first admin, its API key, providers, and settings.

        Everything after the name is optional; a single call can leave the tenant
        fully configured so its owner never has to finish the setup by hand.
        """
        body: dict[str, Any] = {"name": name, "api_key_name": api_key_name}
        if model_id:
            body["model_id"] = model_id
        if providers:
            body["providers"] = providers
        if settings:
            body["settings"] = settings
        if admin_email or admin_display_name or admin_password:
            first: dict[str, Any] = {"role": admin_role}
            if admin_email:
                first["email"] = admin_email
            if admin_display_name:
                first["display_name"] = admin_display_name
            if admin_password:
                first["password"] = admin_password
            body["first_admin"] = first
        return await self._request("POST", "/api/organizations/provision", json=body)

    async def create_organization(self, name: str) -> dict[str, Any]:
        """Create an empty organization shell (no members)."""
        return await self._request(
            "POST", "/api/organizations", json={"name": name}
        )

    async def rename_organization(
        self, organization_id: str, name: str
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/api/organizations/{organization_id}", json={"name": name}
        )

    async def suspend_organization(self, organization_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/organizations/{organization_id}/suspend"
        )

    async def resume_organization(self, organization_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/organizations/{organization_id}/resume"
        )

    async def delete_organization(
        self, organization_id: str, confirm_name: str
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/api/organizations/{organization_id}",
            params={"confirm_name": confirm_name},
        )

    # -- members ----------------------------------------------------------

    async def list_users(
        self, organization_id: str, *, include_daemon: bool = False
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/organizations/{organization_id}/users",
            params={"include_daemon": str(include_daemon).lower()},
            retry=True,
        )
        return payload or []

    async def create_user(
        self,
        organization_id: str,
        *,
        email: str | None,
        display_name: str | None = None,
        role: str = "admin",
        password: str | None = None,
        mint_api_key: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"role": role}
        if email:
            body["email"] = email
        if display_name:
            body["display_name"] = display_name
        if password:
            body["password"] = password
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/users",
            json=body,
            params={"mint_api_key": str(mint_api_key).lower()},
        )

    async def set_user_role(
        self, organization_id: str, user_id: str, role: str
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/api/organizations/{organization_id}/users/{user_id}/role",
            json={"role": role},
        )

    async def reset_user_password(
        self, organization_id: str, user_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/users/{user_id}/reset-password",
        )

    async def mint_api_key(
        self, organization_id: str, user_id: str, name: str = "Default API key"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/users/{user_id}/api-keys",
            json={"name": name},
        )

    async def remove_user(self, organization_id: str, user_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/api/organizations/{organization_id}/users/{user_id}"
        )

    # -- settings ---------------------------------------------------------

    async def get_settings(self, organization_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/organizations/{organization_id}/settings", retry=True
        )

    async def set_setting(
        self, organization_id: str, key: str, value: Any
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/api/organizations/{organization_id}/settings/{key}",
            json={"value": value, "reset": False},
        )

    async def reset_setting(
        self, organization_id: str, key: str
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/api/organizations/{organization_id}/settings/{key}",
            json={"reset": True},
        )

    # -- setting locks ----------------------------------------------------
    #
    # A lock is a write barrier, not a value: the effective value stays in the
    # tenant's own settings row (or its env/default fallback) and locking only
    # removes the tenant's ability to change or reset it. So "pin this tenant to a
    # value" is two calls — set, then lock — and the console keeps them separate
    # for the same reason the API does.
    #
    # The path is ``setting-locks`` rather than ``settings/<key>/lock`` because the
    # server declares ``PUT /settings/{setting_key:path}``, whose greedy path
    # parameter would swallow a trailing segment.

    async def list_setting_locks(self, organization_id: str) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/api/organizations/{organization_id}/setting-locks",
            retry=True,
        )

    async def lock_setting(
        self, organization_id: str, key: str, reason: str = ""
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/api/organizations/{organization_id}/setting-locks/{key}",
            json={"reason": reason},
        )

    async def unlock_setting(
        self, organization_id: str, key: str
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/api/organizations/{organization_id}/setting-locks/{key}",
        )

    async def apply_lock_set(
        self,
        organization_id: str,
        locks: list[dict[str, Any]],
        *,
        release_unlisted: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Reconcile a tenant's whole lock set (the tier-application call)."""
        return await self._request(
            "PUT",
            f"/api/organizations/{organization_id}/setting-locks",
            json={
                "locks": locks,
                "release_unlisted": release_unlisted,
                "dry_run": dry_run,
            },
        )

    async def release_lock_set(
        self, organization_id: str, keys: list[str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/setting-locks/release",
            json={"keys": keys},
        )

    async def monitor_setting_locks(self, *, key: str | None = None) -> dict[str, Any]:
        params = {"key": key} if key else None
        return await self._request(
            "GET",
            "/api/organizations/instance/setting-locks",
            params=params,
            retry=True,
        )

    # -- audit ------------------------------------------------------------

    async def instance_audit(
        self,
        *,
        organization_id: str | None = None,
        action: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if organization_id:
            params["organization_id"] = organization_id
        if action:
            params["action"] = action
        return await self._request(
            "GET", "/api/organizations/instance/audit", params=params, retry=True
        )

    async def organization_audit(
        self,
        organization_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/organizations/{organization_id}/audit",
            params={"offset": offset, "limit": limit},
            retry=True,
        )

    async def available_models(self) -> list[dict[str, Any]]:
        """Models the console can offer when enabling a tenant's first model.

        Uses the admin models catalog, which a hyperadmin can read. A failure
        here must not block provisioning, so callers treat it as optional.
        """
        payload = await self._request(
            "GET", "/api/admin/models", params={"limit": 200}, retry=True
        )
        if isinstance(payload, dict):
            return payload.get("items") or payload.get("models") or []
        return payload or []

    async def instance_runtime(self) -> dict[str, Any]:
        """The instance's LLM engine and the providers that engine can run."""
        return await self._request(
            "GET", "/api/organizations/instance/runtime", retry=True
        ) or {"configured_engine": "", "providers": [], "provider_names": []}

    # -- providers, credentials, models (per tenant) -----------------------

    async def available_providers(self) -> list[dict[str, Any]]:
        """Providers that can be given a credential, with the engines using them."""
        payload = await self._request(
            "GET", "/api/organizations/instance/providers", retry=True
        )
        return (payload or {}).get("items") or []

    async def engine_catalog(self) -> list[dict[str, Any]]:
        """Engines, their allowed providers, and their config fields."""
        return await self._request(
            "GET", "/api/organizations/instance/engines", retry=True
        ) or []

    async def settings_schema(self) -> dict[str, Any]:
        """Keys, types, defaults, and bounds for every editable setting."""
        return await self._request(
            "GET", "/api/organizations/instance/settings-schema", retry=True
        ) or {"sections": [], "count": 0}

    async def list_credentials(self, organization_id: str) -> list[dict[str, Any]]:
        """Which provider credentials a tenant has (values never returned)."""
        payload = await self._request(
            "GET",
            f"/api/organizations/{organization_id}/credentials",
            retry=True,
        )
        return (payload or {}).get("items") or []

    async def set_credential(
        self, organization_id: str, provider: str, value: str
    ) -> dict[str, Any]:
        """Store a provider API key for a tenant.

        This is the credential the engines actually read, so it is what makes a
        provider usable by that tenant.
        """
        return await self._request(
            "PUT",
            f"/api/organizations/{organization_id}/credentials/{provider}",
            json={"value": value},
        )

    async def clear_credential(
        self, organization_id: str, provider: str
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/api/organizations/{organization_id}/credentials/{provider}",
        )

    async def discover_models(self, organization_id: str) -> dict[str, Any]:
        """Register the models the tenant's configured providers expose."""
        return await self._request(
            "POST", f"/api/organizations/{organization_id}/providers/discover"
        )

    async def list_backends(self, organization_id: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/api/organizations/{organization_id}/backends",
            retry=True,
        )
        return (payload or {}).get("items") or []

    async def create_backend(
        self,
        organization_id: str,
        *,
        name: str,
        engine: str,
        provider: str,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        is_enabled: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/backends",
            json={
                "name": name,
                "engine": engine,
                "provider": provider,
                "config": config or {},
                "secrets": secrets or {},
                "is_enabled": is_enabled,
            },
        )

    async def delete_backend(
        self, organization_id: str, backend_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/api/organizations/{organization_id}/backends/{backend_id}"
        )

    async def list_org_models(self, organization_id: str) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", f"/api/organizations/{organization_id}/models", retry=True
        )
        return (payload or {}).get("items") or []

    async def create_model(
        self,
        organization_id: str,
        *,
        model_id: str,
        provider: str,
        name: str = "",
        engine: str | None = None,
        category: str = "general",
        model_class: str = "language",
        context_window: int = 0,
        is_enabled: bool = True,
        grant_to_org: bool = True,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/models",
            json={
                "model_id": model_id,
                "provider": provider,
                "name": name or model_id,
                "engine": engine or None,
                "category": category,
                "model_class": model_class,
                "context_window": context_window,
                "is_enabled": is_enabled,
                "grant_to_org": grant_to_org,
            },
        )

    async def toggle_org_model(
        self, organization_id: str, model_id: str, *, enabled: bool
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/api/organizations/{organization_id}/models/{model_id}",
            json={"enabled": enabled},
        )

    async def grant_org_model(
        self, organization_id: str, model_id: str
    ) -> dict[str, Any]:
        """Grant a model to a tenant, adopting it if it is instance-shared."""
        return await self._request(
            "POST",
            f"/api/organizations/{organization_id}/models/{model_id}/grant",
        )


def _extract_detail(response: httpx.Response) -> str:
    """Pull a human-readable message out of an error response.

    FastAPI returns ``{"detail": "..."}`` for HTTPException and a list of
    validation objects for request-model failures. Both are rendered as
    something an operator can act on.
    """
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:500] if text else response.reason_phrase

    detail = payload.get("detail") if isinstance(payload, dict) else payload
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                location = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                message = item.get("msg") or "invalid"
                parts.append(f"{location}: {message}" if location else message)
            else:
                parts.append(str(item))
        return "; ".join(parts) or "Request validation failed"
    if isinstance(detail, dict):
        # 409 conflict shape: {"field": ..., "message": ...} naming the input
        # that collided. ``message`` is the part an operator can act on.
        if detail.get("message"):
            return str(detail["message"])
        return str(detail)

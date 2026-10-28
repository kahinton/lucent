"""Provisioning a tenant onto a shared LLM gateway (LiteLLM-style).

The deployment shape this proves: an instance operator runs one OpenAI-compatible
gateway in front of whichever vendors it has keys for, and every tenant talks to
that gateway with **its own virtual key**. Tier/plan assignment stays in the
operator's other systems; Lucent only has to be able to point a tenant at a
gateway and hold that tenant's credential.

Two provider choices are involved and they are easy to confuse:

* ``provider="custom"`` — the OpenAI-compatible *gateway* route. It has no vendor
  credential of its own; the base URL and key live on the engine backend, per
  organization. This is the correct choice, and discovery fans out over it.
* ``provider="litellm"`` — the ``langchain-litellm`` integration. It is a valid
  catalog entry but the wrong tool here: it needs an extra package, it is not
  covered by custom-endpoint discovery, and it also advertises a workspace-level
  provider credential, which would give an operator two places to put one key.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.api.deps import CurrentUser, get_current_user
from lucent.db import OrganizationRepository, UserRepository
from lucent.db.backends import BackendRepository
from lucent.db.models import ModelRepository
from lucent.secrets import SecretRegistry
from lucent.secrets.builtin import BuiltinSecretProvider
from lucent.services import org_providers, org_provisioning

GATEWAY_KEY = "sk-tenant-virtual-key-123"


@pytest.fixture(autouse=True)
def _secret_provider(db_pool):
    """Backend secrets need a registered secret provider to be writable.

    Production registers ``builtin`` at startup; tests must do it explicitly,
    otherwise ``create_backend(secrets=...)`` raises KeyError and every
    per-tenant-key assertion would fail for the wrong reason.
    """
    SecretRegistry.register("builtin", BuiltinSecretProvider(db_pool))
    yield


class _GatewayHandler(BaseHTTPRequestHandler):
    """Serve ``GET /v1/models`` and record the bearer token received."""

    models: list[dict] = [
        {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
        {"id": "claude-sonnet-4", "object": "model", "owned_by": "anthropic"},
    ]
    seen_auth: list[str] = []

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        if not self.path.rstrip("/").endswith("/v1/models"):
            self.send_response(404)
            self.end_headers()
            return
        type(self).seen_auth.append(self.headers.get("Authorization", ""))
        body = json.dumps({"object": "list", "data": type(self).models}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def gateway():
    """A live HTTP server standing in for the operator's LiteLLM gateway."""
    _GatewayHandler.seen_auth = []
    server = HTTPServer(("127.0.0.1", 0), _GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest_asyncio.fixture
async def gw_prefix(db_pool, delete_test_organizations):
    prefix = f"gw_{str(uuid4())[:8]}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await delete_test_organizations(conn, [f"{prefix}%"])


@pytest_asyncio.fixture
async def gw_org(db_pool, gw_prefix):
    return await OrganizationRepository(db_pool).create(name=f"{gw_prefix}org")


# ---------------------------------------------------------------------------
# Backend creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_backend_stores_the_tenant_key_encrypted_not_returned(
    db_pool, gw_org, gateway
):
    """The gateway key must be per-organization and never echoed back."""
    created = await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="shared-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
        secrets={"api_key": GATEWAY_KEY},
    )

    assert created["secrets_configured"] == {"api_key": True}
    # The response must never contain the secret value or its storage key.
    assert GATEWAY_KEY not in json.dumps(created)

    backend = await BackendRepository(db_pool).get(created["id"], org_id=str(gw_org["id"]))
    assert backend is not None
    assert backend["provider"] == "custom"
    assert str(backend["organization_id"]) == str(gw_org["id"])
    assert backend["config"]["base_url"] == gateway
    # secret_refs holds a key NAME, never a value.
    assert backend["secret_refs"]["api_key"].startswith("engine.langchain.")


@pytest.mark.asyncio
async def test_backends_are_org_scoped_so_two_tenants_hold_separate_keys(
    db_pool, gw_prefix, gateway
):
    """Two tenants on the same gateway must not share one virtual key."""
    org_a = await OrganizationRepository(db_pool).create(name=f"{gw_prefix}a")
    org_b = await OrganizationRepository(db_pool).create(name=f"{gw_prefix}b")

    for org, key in ((org_a, "sk-tenant-a"), (org_b, "sk-tenant-b")):
        await org_providers.create_backend(
            db_pool,
            org["id"],
            name="shared-gateway",
            engine="langchain",
            provider="custom",
            config={"base_url": gateway},
            secrets={"api_key": key},
        )

    # The same backend name is legal in both orgs: uniqueness is per-org.
    a_backends = await org_providers.list_backends(db_pool, org_a["id"])
    b_backends = await org_providers.list_backends(db_pool, org_b["id"])
    assert len(a_backends) == 1
    assert len(b_backends) == 1
    assert a_backends[0]["id"] != b_backends[0]["id"]


@pytest.mark.asyncio
async def test_backend_rejects_an_unknown_secret_field(db_pool, gw_org, gateway):
    """Only the engine's declared secret fields are accepted; others are typos."""
    with pytest.raises(org_providers.ProviderConfigError, match="Not secret field"):
        await org_providers.create_backend(
            db_pool,
            gw_org["id"],
            name="gw",
            engine="langchain",
            provider="custom",
            config={"base_url": gateway},
            secrets={"gateway_token": "x"},
        )


@pytest.mark.asyncio
async def test_duplicate_backend_name_in_one_org_is_rejected(db_pool, gw_org, gateway):
    kwargs = dict(
        name="gw",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
    )
    await org_providers.create_backend(db_pool, gw_org["id"], **kwargs)
    with pytest.raises(org_providers.ProviderConfigError, match="already exists"):
        await org_providers.create_backend(db_pool, gw_org["id"], **kwargs)


# ---------------------------------------------------------------------------
# Discovery through the gateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_registers_what_the_gateway_serves_and_sends_the_tenant_key(
    db_pool, gw_org, gateway
):
    """This is the step that turns a gateway into selectable models.

    It also proves the *tenant's* key is what reaches the gateway, which is the
    whole point of provisioning tokens per organization.
    """
    _GatewayHandler.seen_auth = []
    await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="shared-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
        secrets={"api_key": GATEWAY_KEY},
    )

    result = await org_providers.discover_models(
        db_pool, gw_org["id"], providers=["custom"]
    )
    assert result.get("upserted_count", 0) >= 2

    # The gateway saw this organization's virtual key, not a global one.
    assert any(GATEWAY_KEY in auth for auth in _GatewayHandler.seen_auth)

    models = await ModelRepository(db_pool).list_models(
        org_id=str(gw_org["id"]), limit=50
    )
    ids = {m["id"] for m in models["items"]}
    assert "gpt-4o-mini" in ids
    assert "claude-sonnet-4" in ids


@pytest.mark.asyncio
async def test_discovered_models_are_bound_to_the_gateway_backend(
    db_pool, gw_org, gateway
):
    """A discovered model must know which endpoint serves it.

    Without the binding, resolution falls back to "any backend for this
    provider", which stops being correct the moment an instance has two.
    """
    backend = await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="shared-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
        secrets={"api_key": GATEWAY_KEY},
    )
    await org_providers.discover_models(db_pool, gw_org["id"], providers=["custom"])

    models = await ModelRepository(db_pool).list_models(
        org_id=str(gw_org["id"]), limit=50
    )
    bound = [m for m in models["items"] if m.get("backend_id")]
    assert bound, "discovered gateway models should carry a backend_id"
    assert all(str(m["backend_id"]) == str(backend["id"]) for m in bound)


# ---------------------------------------------------------------------------
# Model definition + binding through the operator API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_model_binds_to_a_gateway_backend(db_pool, gw_org, gateway):
    backend = await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="shared-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
        secrets={"api_key": GATEWAY_KEY},
    )
    owner = await UserRepository(db_pool).create(
        external_id=f"gwowner-{uuid4().hex[:8]}",
        provider="local",
        organization_id=gw_org["id"],
        email=f"owner-{uuid4().hex[:8]}@test.com",
        display_name="Owner",
        role="owner",
    )

    created = await org_providers.create_model(
        db_pool,
        gw_org["id"],
        model_id=f"gpt-4o-mini-{uuid4().hex[:6]}",
        provider="custom",
        name="GPT-4o mini",
        engine="langchain",
        backend_id=backend["id"],
        granted_by=owner["id"],
    )
    row = await ModelRepository(db_pool).get_model(created["id"])
    assert str(row["backend_id"]) == str(backend["id"])


@pytest.mark.asyncio
async def test_create_model_rejects_a_backend_from_another_org(
    db_pool, gw_prefix, gateway
):
    """A tenant must never be pointed at another tenant's endpoint."""
    org_a = await OrganizationRepository(db_pool).create(name=f"{gw_prefix}a")
    org_b = await OrganizationRepository(db_pool).create(name=f"{gw_prefix}b")
    backend_b = await org_providers.create_backend(
        db_pool,
        org_b["id"],
        name="b-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
    )

    with pytest.raises(org_providers.ModelConfigError, match="not found for this organization"):
        await org_providers.create_model(
            db_pool,
            org_a["id"],
            model_id=f"cross-{uuid4().hex[:6]}",
            provider="custom",
            name="Cross",
            engine="langchain",
            backend_id=backend_b["id"],
        )


@pytest.mark.asyncio
async def test_create_model_rejects_a_backend_speaking_another_provider(
    db_pool, gw_org, gateway
):
    backend = await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="gw",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
    )
    with pytest.raises(org_providers.ModelConfigError, match="speaks provider"):
        await org_providers.create_model(
            db_pool,
            gw_org["id"],
            model_id=f"mismatch-{uuid4().hex[:6]}",
            provider="openai",
            name="Mismatch",
            engine="langchain",
            backend_id=backend["id"],
        )


# ---------------------------------------------------------------------------
# Full provisioning: gateway + key + model in one call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_with_a_gateway_backend_end_to_end(
    db_pool, gw_prefix, gateway
):
    """The call the signup/tier system makes for a new tenant.

    Asserts the ordering guarantees that make it work: the backend exists before
    discovery runs, discovery registers the gateway's catalogue, and the named
    model is bound to that backend and becomes the tenant's default.
    """
    _GatewayHandler.seen_auth = []
    result = await org_provisioning.provision_organization(
        db_pool,
        name=f"{gw_prefix}tenant",
        admin_email=f"{gw_prefix}owner@test.com",
        api_key_name=None,
        backends=[
            {
                "name": "shared-gateway",
                "engine": "langchain",
                "provider": "custom",
                "config": {"base_url": gateway},
                "secrets": {"api_key": GATEWAY_KEY},
            }
        ],
        model_id="gpt-4o-mini",
        model_provider="custom",
        model_backend="shared-gateway",
    )

    org_id = result.organization["id"]
    assert result.backends_created == ["shared-gateway"]
    assert result.warnings == []
    assert result.model_id == "gpt-4o-mini"

    # Discovery ran because the gateway backend counts as a credential source.
    assert result.models_discovered >= 1
    assert any(GATEWAY_KEY in auth for auth in _GatewayHandler.seen_auth)

    # The model is bound to this tenant's gateway backend.
    backend = next(
        b for b in await BackendRepository(db_pool).list(org_id=str(org_id))
        if b["name"] == "shared-gateway"
    )
    model = await ModelRepository(db_pool).get_model("gpt-4o-mini")
    assert model is not None
    assert str(model["backend_id"]) == str(backend["id"])
    assert str(model["organization_id"]) == str(org_id)

    # And it is the tenant's default, so the first chat does not fall back to an
    # instance model this tenant has no route for.
    assert result.settings_applied.get("models.default_model") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_provision_survives_an_unreachable_gateway(db_pool, gw_prefix):
    """A bad gateway must not leave a headless or half-broken tenant behind.

    The org, admin, and backend are all real; only discovery fails, and that
    failure is reported rather than fatal.
    """
    result = await org_provisioning.provision_organization(
        db_pool,
        name=f"{gw_prefix}offline",
        admin_email=f"{gw_prefix}offline@test.com",
        api_key_name=None,
        backends=[
            {
                "name": "dead-gateway",
                "engine": "langchain",
                "provider": "custom",
                "config": {"base_url": "http://127.0.0.1:1"},
                "secrets": {"api_key": "sk-dead"},
            }
        ],
    )

    assert result.backends_created == ["dead-gateway"]
    assert result.user["id"]
    # Discovery failed, and the warning names it instead of raising.
    assert any("discovery" in w.lower() for w in result.warnings), result.warnings


@pytest.mark.asyncio
async def test_provision_without_a_backend_still_works(db_pool, gw_prefix):
    """The pre-existing shape must be untouched: backends remain optional."""
    result = await org_provisioning.provision_organization(
        db_pool,
        name=f"{gw_prefix}plain",
        admin_email=f"{gw_prefix}plain@test.com",
        api_key_name=None,
    )
    assert result.backends_created == []
    assert result.user["id"]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def operator_client(db_pool, gw_prefix):
    op_org = await OrganizationRepository(db_pool).create(name=f"{gw_prefix}oporg")
    operator = await UserRepository(db_pool).create(
        external_id=f"{gw_prefix}operator",
        provider="local",
        organization_id=op_org["id"],
        email=f"{gw_prefix}operator@test.com",
        display_name="Operator",
        role="hyperadmin",
    )
    app = create_app()
    fake = CurrentUser(
        id=operator["id"],
        organization_id=op_org["id"],
        role="hyperadmin",
        email=operator.get("email"),
        display_name=operator.get("display_name"),
        auth_method="api_key",
        api_key_scopes=["read", "write"],
    )

    async def override():
        return fake

    app.dependency_overrides[get_current_user] = override
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_provision_endpoint_accepts_a_gateway_backend(
    operator_client, gw_prefix, gateway
):
    """The HTTP contract the outside signup/tier system drives."""
    resp = await operator_client.post(
        "/api/organizations/provision",
        json={
            "name": f"{gw_prefix}http",
            "first_admin": {"email": f"{gw_prefix}http@test.com"},
            "api_key_name": None,
            "backends": [
                {
                    "name": "shared-gateway",
                    "engine": "langchain",
                    "provider": "custom",
                    "config": {"base_url": gateway},
                    "secrets": {"api_key": GATEWAY_KEY},
                }
            ],
            "model_id": "gpt-4o-mini",
            "model_provider": "custom",
            "model_backend": "shared-gateway",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["backends_created"] == ["shared-gateway"]
    assert body["model_id"] == "gpt-4o-mini"
    # The gateway key must not leak in the response.
    assert GATEWAY_KEY not in resp.text


@pytest.mark.asyncio
async def test_provision_rejects_a_malformed_backend_without_orphaning_the_org(
    operator_client, gw_prefix
):
    """A backend with a secret field the engine does not declare is a warning.

    The tenant is still created: rolling the org back on a bad optional backend
    would be worse than a tenant with fewer capabilities and a clear warning.
    """
    resp = await operator_client.post(
        "/api/organizations/provision",
        json={
            "name": f"{gw_prefix}badbackend",
            "first_admin": {"email": f"{gw_prefix}badbackend@test.com"},
            "api_key_name": None,
            "backends": [
                {
                    "name": "bad",
                    "engine": "langchain",
                    "provider": "custom",
                    "config": {"base_url": "https://x"},
                    "secrets": {"gateway_token": "nope"},
                }
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["backends_created"] == []
    assert any("bad" in w for w in body["warnings"])


# ---------------------------------------------------------------------------
# Id collisions with the shared catalogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_model_id_colliding_with_a_shared_row_stays_bound(
    db_pool, gw_org, gateway
):
    """A gateway model name can collide with an unrelated shared catalogue row.

    ``models.id`` is a global primary key, so both may be called
    ``claude-sonnet-4``. Cloning the shared row would keep the model usable but
    silently drop the backend binding, routing the tenant's traffic wherever the
    shared backend points. The tenant must instead get its own suffixed row bound
    to its own gateway — the ``<model>@<org-prefix>`` convention the grant path
    already uses.
    """
    colliding_id = f"claude-sonnet-4-{uuid4().hex[:6]}"
    # A shared row is instance-wide state; creating one needs system scope.
    from lucent.db.pool import clear_tenant_scope, set_tenant_scope

    set_tenant_scope(role="system")
    try:
        await ModelRepository(db_pool).create_model(
            colliding_id, "anthropic", "Shared Claude", org_id=None
        )
    finally:
        clear_tenant_scope()
    backend = await org_providers.create_backend(
        db_pool,
        gw_org["id"],
        name="shared-gateway",
        engine="langchain",
        provider="custom",
        config={"base_url": gateway},
        secrets={"api_key": GATEWAY_KEY},
    )
    owner = await UserRepository(db_pool).create(
        external_id=f"gwowner-{uuid4().hex[:8]}",
        provider="local",
        organization_id=gw_org["id"],
        email=f"owner-{uuid4().hex[:8]}@test.com",
        display_name="Owner",
        role="owner",
    )

    created = await org_providers.create_model(
        db_pool,
        gw_org["id"],
        model_id=colliding_id,
        provider="custom",
        name="Claude via gateway",
        engine="langchain",
        backend_id=backend["id"],
        granted_by=owner["id"],
        scope_id_on_collision=True,
    )

    assert created["id"] != colliding_id, "the tenant must get its own row"
    assert created["id"].startswith(f"{colliding_id}@")
    assert str(created["backend_id"]) == str(backend["id"])

    # The shared row is untouched and still unbound.
    shared = await ModelRepository(db_pool).get_model(colliding_id)
    assert shared["organization_id"] is None
    assert shared["backend_id"] is None


@pytest.mark.asyncio
async def test_collision_without_the_flag_is_an_error_not_a_silent_clone(
    db_pool, gw_org
):
    """The default stays strict so no other caller changes behaviour."""
    taken = f"taken-{uuid4().hex[:6]}"
    from lucent.db.pool import clear_tenant_scope, set_tenant_scope

    set_tenant_scope(role="system")
    try:
        await ModelRepository(db_pool).create_model(
            taken, "openai", "Taken", org_id=None
        )
    finally:
        clear_tenant_scope()
    with pytest.raises(org_providers.ModelConfigError, match="already exists"):
        await org_providers.create_model(
            db_pool, gw_org["id"], model_id=taken, provider="custom", name="X"
        )

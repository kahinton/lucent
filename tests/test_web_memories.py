"""Integration tests for memory web routes in web/routes.py.

Tests the HTML-serving memory endpoints:
- GET  /memories                              (list/search memories)
- GET  /memories/new                          (new memory form)
- POST /memories/new                          (create memory)
- GET  /memories/{memory_id}                  (view memory detail)
- GET  /memories/{memory_id}/edit             (edit form)
- POST /memories/{memory_id}/edit             (submit edit)
- POST /memories/{memory_id}/share            (toggle sharing)
- POST /memories/{memory_id}/delete           (delete memory)
- POST /memories/{memory_id}/restore/{version} (restore version)

Uses real DB sessions + CSRF tokens through the full ASGI stack.
"""

from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import ASGITransport

from lucent.api.app import create_app
from lucent.auth_providers import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    set_user_password,
)
from lucent.db import MemoryRepository, OrganizationRepository, UserRepository

TEST_PASSWORD = "TestPass1"


# ============================================================================
# Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def web_prefix(db_pool):
    """Unique prefix and cleanup for web memory tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_webmem_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM memory_audit_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM memory_access_log WHERE memory_id IN "
            "(SELECT id FROM memories WHERE username LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM memories WHERE username LIKE $1", f"{prefix}%")
        await conn.execute(
            "DELETE FROM api_keys WHERE user_id IN "
            "(SELECT id FROM users WHERE external_id LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute("DELETE FROM users WHERE external_id LIKE $1", f"{prefix}%")
        await conn.execute("DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%")


@pytest_asyncio.fixture
async def web_user(db_pool, web_prefix):
    """Create user + org with a password set for web memory tests."""
    org_repo = OrganizationRepository(db_pool)
    org = await org_repo.create(name=f"{web_prefix}org")
    user_repo = UserRepository(db_pool)
    user = await user_repo.create(
        external_id=f"{web_prefix}user",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}user@test.com",
        display_name=f"{web_prefix}User",
    )
    await set_user_password(db_pool, user["id"], TEST_PASSWORD)
    token = await create_session(db_pool, user["id"])
    return user, org, token


@pytest_asyncio.fixture
async def client(db_pool, web_user):
    """httpx client with session + CSRF cookies pre-set."""
    _user, _org, session_token = web_user
    csrf_token = "test-csrf-token-mem123"

    app = create_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={
            SESSION_COOKIE_NAME: session_token,
            CSRF_COOKIE_NAME: csrf_token,
        },
    ) as c:
        c._csrf_token = csrf_token  # type: ignore[attr-defined]
        yield c


@pytest_asyncio.fixture
async def web_memory(db_pool, web_user, web_prefix):
    """Create a memory owned by the test user."""
    user, org, _token = web_user
    repo = MemoryRepository(db_pool)
    memory = await repo.create(
        username=f"{web_prefix}User",
        type="experience",
        content="Test memory content for web tests",
        tags=["test", "web"],
        importance=5,
        user_id=user["id"],
        organization_id=org["id"],
    )
    return memory


@pytest_asyncio.fixture
async def other_user_memory(db_pool, web_user, web_prefix):
    """Create a second user in the same org with a memory owned by them."""
    _user, org, _token = web_user
    user_repo = UserRepository(db_pool)
    other_user = await user_repo.create(
        external_id=f"{web_prefix}other",
        provider="basic",
        organization_id=org["id"],
        email=f"{web_prefix}other@test.com",
        display_name=f"{web_prefix}Other",
    )
    repo = MemoryRepository(db_pool)
    memory = await repo.create(
        username=f"{web_prefix}Other",
        type="experience",
        content="Other user memory content",
        tags=["test", "other"],
        importance=5,
        user_id=other_user["id"],
        organization_id=org["id"],
        shared=True,
    )
    return memory


def _csrf_data(client: httpx.AsyncClient, extra: dict | None = None) -> dict:
    """Build form data dict with CSRF token included."""
    data = {CSRF_FIELD_NAME: client._csrf_token}  # type: ignore[attr-defined]
    if extra:
        data.update(extra)
    return data


# ============================================================================
# GET /memories — list / search
# ============================================================================


class TestMemoriesList:
    async def test_list_returns_html(self, client, web_memory):
        resp = await client.get("/memories")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_list_contains_memories_text(self, client, web_memory):
        resp = await client.get("/memories")
        assert "memor" in resp.text.lower()

    async def test_list_with_query_param(self, client, web_memory):
        resp = await client.get("/memories", params={"q": "Test memory"})
        assert resp.status_code == 200

    async def test_list_with_type_filter(self, client, web_memory):
        resp = await client.get("/memories", params={"type": "experience"})
        assert resp.status_code == 200

    async def test_list_with_tag_filter(self, client, web_memory):
        resp = await client.get("/memories", params={"tag": "test"})
        assert resp.status_code == 200

    async def test_list_pagination(self, client, web_memory):
        resp = await client.get("/memories", params={"page": "2"})
        assert resp.status_code == 200

    async def test_list_unauthenticated_redirects(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/memories", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /memories/new — new memory form
# ============================================================================


class TestMemoryNewForm:
    async def test_new_form_returns_html(self, client):
        resp = await client.get("/memories/new")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_new_form_unauthenticated_redirects(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/memories/new", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# POST /memories/new — create memory
# ============================================================================


class TestMemoryNewSubmit:
    async def test_create_redirects_to_detail(self, client):
        resp = await client.post(
            "/memories/new",
            data=_csrf_data(
                client,
                {
                    "type": "experience",
                    "content": "Created via test",
                    "tags": "test,create",
                    "importance": "5",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/memories/" in resp.headers["location"]

    async def test_create_individual_returns_400(self, client):
        resp = await client.post(
            "/memories/new",
            data=_csrf_data(
                client,
                {
                    "type": "individual",
                    "content": "Should not work",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_create_missing_csrf_fails(self, client):
        resp = await client.post(
            "/memories/new",
            data={
                "type": "experience",
                "content": "No CSRF",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_create_unauthenticated_redirects(self, db_pool):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        csrf = "test-csrf-unauth"
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={CSRF_COOKIE_NAME: csrf},
        ) as c:
            resp = await c.post(
                "/memories/new",
                data={CSRF_FIELD_NAME: csrf, "type": "experience", "content": "x"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /memories/{id} — memory detail
# ============================================================================


class TestMemoryDetail:
    async def test_detail_returns_html(self, client, web_memory):
        resp = await client.get(f"/memories/{web_memory['id']}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_detail_contains_content(self, client, web_memory):
        resp = await client.get(f"/memories/{web_memory['id']}")
        assert "Test memory content" in resp.text

    async def test_detail_not_found(self, client):
        resp = await client.get(f"/memories/{uuid4()}")
        assert resp.status_code == 404

    async def test_detail_unauthenticated_redirects(self, db_pool, web_memory):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(f"/memories/{web_memory['id']}", follow_redirects=False)
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# GET /memories/{id}/edit — edit form
# ============================================================================


class TestMemoryEditForm:
    async def test_edit_form_returns_html(self, client, web_memory):
        resp = await client.get(f"/memories/{web_memory['id']}/edit")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_edit_form_not_found(self, client):
        resp = await client.get(f"/memories/{uuid4()}/edit")
        assert resp.status_code == 404

    async def test_edit_form_other_user_returns_403(self, client, other_user_memory):
        resp = await client.get(f"/memories/{other_user_memory['id']}/edit")
        assert resp.status_code == 403

    async def test_edit_form_unauthenticated_redirects(self, db_pool, web_memory):
        app = create_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(
                f"/memories/{web_memory['id']}/edit",
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "/login" in resp.headers.get("location", "")


# ============================================================================
# POST /memories/{id}/edit — submit edit
# ============================================================================


class TestMemoryEditSubmit:
    async def test_edit_redirects_to_detail(self, client, web_memory):
        resp = await client.post(
            f"/memories/{web_memory['id']}/edit",
            data=_csrf_data(
                client,
                {
                    "content": "Updated content",
                    "tags": "updated",
                    "importance": "7",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/memories/{web_memory['id']}" in resp.headers["location"]

    async def test_edit_persists_changes(self, client, web_memory, db_pool, web_user):
        user, org, _token = web_user
        await client.post(
            f"/memories/{web_memory['id']}/edit",
            data=_csrf_data(
                client,
                {
                    "content": "Persisted update",
                    "tags": "persisted",
                    "importance": "8",
                },
            ),
        )
        repo = MemoryRepository(db_pool)
        updated = await repo.get_accessible(
            web_memory["id"],
            user["id"],
            org["id"],
        )
        assert updated["content"] == "Persisted update"

    async def test_edit_not_found(self, client):
        resp = await client.post(
            f"/memories/{uuid4()}/edit",
            data=_csrf_data(
                client,
                {
                    "content": "x",
                    "tags": "",
                    "importance": "5",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_edit_other_user_returns_403(self, client, other_user_memory):
        resp = await client.post(
            f"/memories/{other_user_memory['id']}/edit",
            data=_csrf_data(
                client,
                {
                    "content": "Hacked",
                    "tags": "",
                    "importance": "5",
                },
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_edit_missing_csrf_fails(self, client, web_memory):
        resp = await client.post(
            f"/memories/{web_memory['id']}/edit",
            data={
                "content": "No CSRF",
                "tags": "",
                "importance": "5",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /memories/{id}/share — toggle sharing
# ============================================================================


class TestMemoryShare:
    async def test_share_returns_404_in_non_team_mode(self, client, web_memory):
        """Default test env is not team mode, so share returns 404."""
        resp = await client.post(
            f"/memories/{web_memory['id']}/share",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ============================================================================
# POST /memories/{id}/delete — delete memory
# ============================================================================


class TestMemoryDelete:
    async def test_delete_redirects_to_list(self, client, web_memory):
        resp = await client.post(
            f"/memories/{web_memory['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/memories"

    async def test_delete_removes_memory(self, client, web_memory, db_pool, web_user):
        user, org, _token = web_user
        await client.post(
            f"/memories/{web_memory['id']}/delete",
            data=_csrf_data(client),
        )
        repo = MemoryRepository(db_pool)
        result = await repo.get_accessible(web_memory["id"], user["id"], org["id"])
        assert result is None

    async def test_delete_not_found(self, client):
        resp = await client.post(
            f"/memories/{uuid4()}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_delete_other_user_returns_403(self, client, other_user_memory):
        resp = await client.post(
            f"/memories/{other_user_memory['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_delete_individual_type_returns_400(
        self,
        client,
        db_pool,
        web_user,
        web_prefix,
    ):
        """Individual memories cannot be deleted via web."""
        user, org, _token = web_user
        repo = MemoryRepository(db_pool)
        individual = await repo.create(
            username=f"{web_prefix}User",
            type="individual",
            content="Individual memory",
            tags=["individual"],
            importance=5,
            user_id=user["id"],
            organization_id=org["id"],
        )
        resp = await client.post(
            f"/memories/{individual['id']}/delete",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_delete_missing_csrf_fails(self, client, web_memory):
        resp = await client.post(
            f"/memories/{web_memory['id']}/delete",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 403


# ============================================================================
# POST /memories/{id}/restore/{version} — restore version
# ============================================================================


class TestMemoryRestore:
    async def test_restore_redirects_to_detail(
        self,
        client,
        db_pool,
        web_user,
        web_prefix,
    ):
        """Create a memory, edit it (creating version 2), then restore to version 1."""
        user, org, _token = web_user
        repo = MemoryRepository(db_pool)
        from lucent.db.audit import AuditRepository

        audit_repo = AuditRepository(db_pool)

        # Create memory
        memory = await repo.create(
            username=f"{web_prefix}User",
            type="experience",
            content="Original content",
            tags=["restore-test"],
            importance=5,
            user_id=user["id"],
            organization_id=org["id"],
        )

        # Log creation with snapshot (version 1)
        await audit_repo.log(
            memory_id=memory["id"],
            action_type="create",
            user_id=user["id"],
            organization_id=org["id"],
            new_values={"content": "Original content"},
            version=memory["version"],
            snapshot={
                "content": "Original content",
                "tags": ["restore-test"],
                "importance": 5,
                "metadata": None,
                "related_memory_ids": [],
                "shared": False,
            },
        )

        # Edit memory (creates version 2)
        updated = await repo.update(
            memory_id=memory["id"],
            content="Edited content",
            tags=["restore-test", "edited"],
            importance=7,
        )

        await audit_repo.log(
            memory_id=memory["id"],
            action_type="update",
            user_id=user["id"],
            organization_id=org["id"],
            changed_fields=["content", "tags", "importance"],
            old_values={"content": "Original content"},
            new_values={"content": "Edited content"},
            version=updated["version"],
            snapshot={
                "content": "Edited content",
                "tags": ["restore-test", "edited"],
                "importance": 7,
                "metadata": None,
                "related_memory_ids": [],
                "shared": False,
            },
        )

        # Restore to version 1
        resp = await client.post(
            f"/memories/{memory['id']}/restore/1",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/memories/{memory['id']}" in resp.headers["location"]

        # Verify content was restored
        restored = await repo.get_accessible(memory["id"], user["id"], org["id"])
        assert restored["content"] == "Original content"

    async def test_restore_not_found_memory(self, client):
        resp = await client.post(
            f"/memories/{uuid4()}/restore/1",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_restore_missing_csrf_fails(self, client, web_memory):
        resp = await client.post(
            f"/memories/{web_memory['id']}/restore/1",
            data={},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_restore_other_user_returns_403(self, client, other_user_memory):
        resp = await client.post(
            f"/memories/{other_user_memory['id']}/restore/1",
            data=_csrf_data(client),
            follow_redirects=False,
        )
        assert resp.status_code == 403

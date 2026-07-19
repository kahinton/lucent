import pytest

from lucent.daemon_identity import ensure_daemon_service_user, resolve_daemon_service_user


class _Connection:
    def __init__(self, *, existing=None, organization=None):
        self.existing = existing
        self.organization = organization
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "SELECT id, organization_id FROM users" in query:
            return self.existing
        if "SELECT id FROM organizations" in query:
            return self.organization
        if "INSERT INTO users" in query:
            return {"id": "daemon-user", "organization_id": args[1]}
        raise AssertionError(query)


@pytest.mark.asyncio
async def test_ensure_daemon_service_user_uses_org_scoped_daemon_role():
    conn = _Connection()

    user = await ensure_daemon_service_user(conn, "org-1")

    assert user == {"id": "daemon-user", "organization_id": "org-1"}
    insert_query, insert_args = conn.calls[-1]
    assert "'Lucent Daemon', 'daemon'" in insert_query
    assert insert_args == ("daemon-service:org-1", "org-1")


@pytest.mark.asyncio
async def test_resolve_daemon_service_user_never_falls_back_to_system_org():
    conn = _Connection(organization={"id": "real-org"})

    user = await resolve_daemon_service_user(conn)

    assert user["organization_id"] == "real-org"
    select_query, select_args = conn.calls[0]
    assert "name <> $1" in select_query
    assert select_args == ("__lucent_system__",)

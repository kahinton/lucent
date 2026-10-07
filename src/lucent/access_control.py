"""Application-facing facade for the access-control repository."""

from __future__ import annotations

from lucent.db.access_control import AccessControlRepository, normalize_resource_type

RESOURCE_TABLE_MAP = normalize_resource_type.__globals__["RESOURCE_TABLE_MAP"]

__all__ = ["AccessControlRepository", "AccessControlService", "normalize_resource_type"]


class AccessControlService:
    """Backward-compatible wrapper around the database repository."""

    def __init__(self, pool):
        self._repository = AccessControlRepository(pool)

    @classmethod
    def invalidate_user_groups(cls, user_id: str) -> None:
        AccessControlRepository.invalidate_user_groups(user_id)
        # The authorized-pool principal cache shares the same TTL contract;
        # lazy import (db.pool is imported by everything downstream of this).
        from lucent.db.pool import invalidate_user_groups as invalidate_authorized_pool

        invalidate_authorized_pool(user_id)

    async def _get_user_role(self, user_id: str, org_id: str) -> str | None:
        return await self._repository._get_user_role(user_id, org_id)

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        return await self._repository.get_user_group_ids(user_id)

    async def can_access(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        return await self._repository.can_access(user_id, resource_type, resource_id, org_id)

    async def can_modify(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        return await self._repository.can_modify(user_id, resource_type, resource_id, org_id)

    async def list_accessible(self, user_id: str, resource_type: str, org_id: str) -> list[str]:
        return await self._repository.list_accessible(user_id, resource_type, org_id)

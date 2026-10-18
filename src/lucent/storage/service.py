"""Ownership-aware orchestration for user file content and metadata."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from pathlib import PurePath, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from asyncpg import Pool

from lucent.db.files import UserFileRepository
from lucent.db.llm_sessions import LLMSessionRepository
from lucent.db.requests import RequestRepository
from lucent.settings import (
    file_soft_delete_retention_days,
    max_bytes_per_file,
    max_bytes_per_org,
    max_bytes_per_user,
)
from lucent.storage.providers import FileStorageRegistry, build_file_storage_registry

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

logger = logging.getLogger(__name__)


def _safe_filename(filename: str) -> str:
    name = PurePath(str(filename or "")).name.strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("A valid filename is required")
    return name[:512]


class UserFileService:
    def __init__(self, pool: Pool, registry: FileStorageRegistry | None = None):
        self.pool = pool
        self.repository = UserFileRepository(pool)
        self.registry = registry or build_file_storage_registry()
        # Retained for callers that read it; the effective per-file cap is
        # resolved per organization at write time from runtime settings.
        self.max_file_bytes = max_bytes_per_file()

    async def create(
        self,
        *,
        org_id: str,
        user_id: str,
        created_by: str,
        filename: str,
        content: bytes,
        display_name: str | None = None,
        mime_type: str | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_name: str | None = None,
        is_primary: bool = False,
    ) -> dict[str, Any]:
        org_id = str(org_id)
        user_id = str(user_id)
        per_file_limit = max_bytes_per_file(organization_id=org_id)
        if len(content) > per_file_limit:
            raise ValueError(
                f"File exceeds the per-file limit of {per_file_limit} bytes"
            )
        safe_name = _safe_filename(filename)
        resolved_type = (
            mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        )

        if task_id:
            task = await RequestRepository(self.pool).get_task(task_id, org_id)
            if not task or str(task["requesting_user_id"] or "") != str(user_id):
                raise ValueError("Task not found")
            request_id = str(task["request_id"])
        elif request_id:
            owns_request = await RequestRepository(self.pool).owns_request(
                request_id,
                org_id=org_id,
                user_id=user_id,
            )
            if not owns_request:
                raise ValueError("Request not found")

        await self._validate_session_owner(session_id, org_id, user_id)

        file_id = str(uuid4())
        storage_key = f"{org_id}/{user_id}/{file_id}"
        self._validate_storage_key_owner(storage_key, org_id, user_id)
        # New writes go to the active provider; the row records which provider
        # actually stored the bytes so reads resolve it later regardless of the
        # active setting at read time.
        provider = (
            self.registry.active() if provider_name is None else self.registry.get(provider_name)
        )
        limits = self._quota_limits(org_id)
        # Reserve quota *before* touching storage: an over-quota upload is
        # rejected here, without transmitting or storing its payload. The
        # reservation is released on any failure below and the final insert
        # measures again under the lock.
        reservation_id = await self.repository.reserve(
            org_id=org_id,
            user_id=user_id,
            new_bytes=len(content),
            **limits,
        )
        try:
            await provider.put(storage_key, content)
            item = await self.repository.create(
                org_id=org_id,
                user_id=user_id,
                created_by=created_by,
                request_id=request_id,
                task_id=task_id,
                session_id=session_id,
                turn_id=turn_id,
                message_id=message_id,
                provider=provider.name,
                storage_key=storage_key,
                filename=safe_name,
                display_name=(display_name or safe_name)[:256],
                mime_type=resolved_type,
                size_bytes=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
                change_summary="Created file",
                metadata=metadata,
                reservation_id=reservation_id,
                **limits,
            )
        except Exception:
            await self._cleanup_failed_write(
                provider=provider,
                storage_key=storage_key,
                reservation_id=reservation_id,
                org_id=org_id,
                user_id=user_id,
            )
            raise

        if task_id:
            output = await RequestRepository(self.pool).create_task_output(
                task_id=task_id,
                org_id=org_id,
                created_by=created_by,
                output={
                    "output_type": "file",
                    "provider": provider.name,
                    "title": item["display_name"],
                    "url": item["url"],
                    "external_id": str(item["id"]),
                    "mime_type": item["mime_type"],
                    "metadata": {"user_file_id": str(item["id"])},
                    "is_primary": is_primary,
                },
            )
            item["task_output_id"] = str(output["id"])
        return item

    async def update(
        self,
        *,
        file_id: str,
        org_id: str,
        user_id: str,
        edited_by: str,
        content: bytes,
        change_summary: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        org_id = str(org_id)
        user_id = str(user_id)
        per_file_limit = max_bytes_per_file(organization_id=org_id)
        if len(content) > per_file_limit:
            raise ValueError(f"File exceeds the per-file limit of {per_file_limit} bytes")
        current = await self._resolve_owned_file(file_id, org_id, user_id)
        if not current:
            raise ValueError("File not found")
        await self._validate_session_owner(session_id, org_id, user_id)
        request_id = await self._validate_work_owner(
            request_id=request_id,
            task_id=task_id,
            org_id=org_id,
            user_id=user_id,
        )
        resolved_file_id = str(current["id"])
        storage_key = f"{org_id}/{user_id}/{resolved_file_id}/revisions/{uuid4()}"
        self._validate_storage_key_owner(storage_key, org_id, user_id)
        # Revisions are new writes: they go to the *active* provider, and the
        # revision row records it. Following the row's original provider here
        # would keep appending revisions of a legacy local file to a container
        # filesystem after the deployment switched to Azure.
        provider = self.registry.active()
        limits = self._quota_limits(org_id)
        # Reserve before the provider write so an over-quota revision is
        # rejected without touching storage.
        reservation_id = await self.repository.reserve(
            org_id=org_id,
            user_id=user_id,
            new_bytes=len(content),
            **limits,
        )
        try:
            await provider.put(storage_key, content)
            revision = await self.repository.append_revision(
                file_id=resolved_file_id,
                org_id=org_id,
                user_id=user_id,
                edited_by=edited_by,
                provider=provider.name,
                storage_key=storage_key,
                size_bytes=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
                change_summary=(change_summary or "Updated file").strip()[:500],
                session_id=session_id,
                turn_id=turn_id,
                message_id=message_id,
                request_id=request_id,
                task_id=task_id,
                reservation_id=reservation_id,
                **limits,
            )
        except Exception:
            await self._cleanup_failed_write(
                provider=provider,
                storage_key=storage_key,
                reservation_id=reservation_id,
                org_id=org_id,
                user_id=user_id,
            )
            raise
        if not revision:
            await self._cleanup_failed_write(
                provider=provider,
                storage_key=storage_key,
                reservation_id=reservation_id,
                org_id=org_id,
                user_id=user_id,
            )
            raise ValueError("File not found")
        item = await self.repository.get_owned(resolved_file_id, org_id, user_id)
        if not item:
            raise ValueError("File not found")
        item["revision"] = revision
        return item

    async def _validate_session_owner(
        self, session_id: str | None, org_id: str, user_id: str
    ) -> None:
        if not session_id:
            return
        owns_session = await LLMSessionRepository(self.pool).user_owns_session(
            session_id=session_id,
            org_id=org_id,
            user_id=user_id,
        )
        if not owns_session:
            raise ValueError("Chat session not found")

    async def _validate_work_owner(
        self,
        *,
        request_id: str | None,
        task_id: str | None,
        org_id: str,
        user_id: str,
    ) -> str | None:
        if task_id:
            task = await RequestRepository(self.pool).get_task(task_id, org_id)
            if not task or str(task["requesting_user_id"] or "") != str(user_id):
                raise ValueError("Task not found")
            return str(task["request_id"])
        if request_id:
            owns_request = await RequestRepository(self.pool).owns_request(
                request_id,
                org_id=org_id,
                user_id=user_id,
            )
            if not owns_request:
                raise ValueError("Request not found")
        return request_id

    @staticmethod
    def _validate_storage_key_owner(storage_key: str, org_id: str, user_id: str) -> None:
        """Fail closed when a provider key is not scoped to the row's owner.

        Keys are written as ``<organization_id>/<user_id>/...``. Ownership is
        resolved from the row, so a row whose key names another tenant (legacy
        data, a future caller supplying keys) must not be allowed to reach a
        provider: the key must agree with the owner the query already proved.
        """
        parts = PurePosixPath(str(storage_key or "")).parts
        if len(parts) < 2 or parts[0] != str(org_id) or parts[1] != str(user_id):
            raise ValueError("Storage key does not belong to this file owner")

    @staticmethod
    def _quota_limits(org_id: str) -> dict[str, int]:
        return {
            "max_bytes_per_user": max_bytes_per_user(organization_id=org_id),
            "max_bytes_per_org": max_bytes_per_org(organization_id=org_id),
            "soft_delete_retention_days": file_soft_delete_retention_days(
                organization_id=org_id
            ),
        }

    async def _cleanup_failed_write(
        self,
        *,
        provider: Any,
        storage_key: str,
        reservation_id: str | None,
        org_id: str,
        user_id: str,
    ) -> None:
        """Release a failed write's reservation and remove any stored bytes.

        Both steps are best-effort and individually guarded: the exception that
        brought us here is the one the caller must see, not a secondary failure
        from cleanup.
        """
        if reservation_id is not None:
            try:
                await self.repository.release_reservation(
                    reservation_id, org_id=org_id, user_id=user_id
                )
            except Exception:
                logger.exception(
                    "Failed to release file quota reservation %s", reservation_id
                )
        try:
            await provider.delete(storage_key)
        except Exception:
            logger.exception(
                "Failed to remove orphaned file storage object %s", storage_key
            )

    async def _resolve_owned_file(
        self,
        file_reference: str,
        org_id: str,
        user_id: str,
        *,
        include_storage_key: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve an owned file ID or an exact filename without guessing."""
        try:
            file_id = str(UUID(file_reference))
        except (TypeError, ValueError):
            file_id = None
        if file_id:
            item = await self.repository.get_owned(
                file_id, org_id, user_id, include_storage_key=include_storage_key
            )
            if item:
                return item

        try:
            filename = _safe_filename(file_reference)
        except ValueError:
            return None
        matches = await self.repository.find_owned_by_filename(
            filename, org_id, user_id, include_storage_key=include_storage_key
        )
        if len(matches) > 1:
            raise ValueError(
                f"Multiple owned files are named {filename!r}. "
                "Call list_user_files and use the returned file ID."
            )
        return matches[0] if matches else None

    async def get_owned(
        self, file_reference: str, org_id: str, user_id: str
    ) -> dict[str, Any] | None:
        return await self._resolve_owned_file(file_reference, org_id, user_id)

    async def read_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> tuple[dict[str, Any], bytes] | None:
        item = await self._resolve_owned_file(
            file_id, org_id, user_id, include_storage_key=True
        )
        if not item:
            return None
        storage_key = item.pop("storage_key", "")
        self._validate_storage_key_owner(storage_key, org_id, user_id)
        try:
            content = await self.registry.get(item["provider"]).get(storage_key)
        except FileNotFoundError:
            return None
        return item, content

    async def read_revision_owned(
        self,
        file_id: str,
        revision_number: int,
        org_id: str,
        user_id: str,
    ) -> tuple[dict[str, Any], bytes] | None:
        item = await self._resolve_owned_file(file_id, org_id, user_id)
        if not item:
            return None
        revision = await self.repository.get_revision_owned(
            str(item["id"]),
            revision_number,
            org_id,
            user_id,
            include_storage_key=True,
        )
        if not revision:
            return None
        storage_key = revision.pop("storage_key", "")
        self._validate_storage_key_owner(storage_key, org_id, user_id)
        try:
            content = await self.registry.get(revision["provider"]).get(storage_key)
        except FileNotFoundError:
            return None
        return revision, content

    async def delete_owned(self, file_id: str, org_id: str, user_id: str) -> bool:
        item = await self._resolve_owned_file(file_id, org_id, user_id)
        if not item:
            return False
        resolved_file_id = str(item["id"])
        storage_keys = await self.repository.list_storage_keys_owned(
            resolved_file_id, org_id, user_id
        )
        # Fail closed before mutating anything: every key must be scoped to
        # this row's owner, and every provider must resolve. A row whose
        # provider is no longer configured therefore stays live and retryable;
        # soft-deleting first would strand blobs behind a 404 forever.
        for _provider_name, storage_key in storage_keys:
            self._validate_storage_key_owner(storage_key, org_id, user_id)
        targets = [
            (self.registry.get(provider_name), storage_key)
            for provider_name, storage_key in storage_keys
        ]
        # Provider deletes happen before the row mutation. They are idempotent,
        # so a transient failure part-way leaves the row live and a retry
        # finishes the remaining deletes rather than losing track of them.
        for provider, storage_key in targets:
            await provider.delete(storage_key)
        deleted = await self.repository.soft_delete(resolved_file_id, org_id, user_id)
        return deleted is not None

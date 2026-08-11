"""Ownership-aware orchestration for user file content and metadata."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from pathlib import PurePath
from typing import Any
from uuid import uuid4

from asyncpg import Pool

from lucent.db.files import UserFileRepository
from lucent.storage.providers import FileStorageRegistry

DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024


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
        self.registry = registry or FileStorageRegistry()
        self.max_file_bytes = int(
            os.environ.get("LUCENT_MAX_USER_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES))
        )

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
        provider_name: str = "local",
        is_primary: bool = False,
    ) -> dict[str, Any]:
        if len(content) > self.max_file_bytes:
            raise ValueError(f"File exceeds the {self.max_file_bytes}-byte limit")
        safe_name = _safe_filename(filename)
        resolved_type = (
            mime_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        )

        if task_id:
            async with self.pool.acquire() as conn:
                task = await conn.fetchrow(
                    """SELECT t.id, t.request_id, r.created_by
                       FROM tasks t JOIN requests r ON r.id = t.request_id
                       WHERE t.id = $1::uuid AND t.organization_id = $2::uuid""",
                    task_id,
                    org_id,
                )
            if not task or str(task["created_by"] or "") != str(user_id):
                raise ValueError("Task not found")
            request_id = str(task["request_id"])
        elif request_id:
            async with self.pool.acquire() as conn:
                owns_request = await conn.fetchval(
                    """SELECT EXISTS(
                           SELECT 1 FROM requests
                           WHERE id = $1::uuid AND organization_id = $2::uuid
                             AND created_by = $3::uuid
                       )""",
                    request_id,
                    org_id,
                    user_id,
                )
            if not owns_request:
                raise ValueError("Request not found")

        await self._validate_session_owner(session_id, org_id, user_id)

        file_id = str(uuid4())
        storage_key = f"{org_id}/{user_id}/{file_id}"
        provider = self.registry.get(provider_name)
        await provider.put(storage_key, content)
        try:
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
            )
        except Exception:
            await provider.delete(storage_key)
            raise

        if task_id:
            from lucent.db.requests import RequestRepository

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
        if len(content) > self.max_file_bytes:
            raise ValueError(f"File exceeds the {self.max_file_bytes}-byte limit")
        current = await self.repository.get_owned(file_id, org_id, user_id)
        if not current:
            raise ValueError("File not found")
        await self._validate_session_owner(session_id, org_id, user_id)
        request_id = await self._validate_work_owner(
            request_id=request_id,
            task_id=task_id,
            org_id=org_id,
            user_id=user_id,
        )
        storage_key = f"{org_id}/{user_id}/{file_id}/revisions/{uuid4()}"
        provider = self.registry.get(current["provider"])
        await provider.put(storage_key, content)
        try:
            revision = await self.repository.append_revision(
                file_id=file_id,
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
            )
        except Exception:
            await provider.delete(storage_key)
            raise
        if not revision:
            await provider.delete(storage_key)
            raise ValueError("File not found")
        item = await self.repository.get_owned(file_id, org_id, user_id)
        if not item:
            raise ValueError("File not found")
        item["revision"] = revision
        return item

    async def _validate_session_owner(
        self, session_id: str | None, org_id: str, user_id: str
    ) -> None:
        if not session_id:
            return
        async with self.pool.acquire() as conn:
            owns_session = await conn.fetchval(
                """SELECT EXISTS(
                       SELECT 1 FROM llm_sessions
                       WHERE id = $1::uuid AND organization_id = $2::uuid
                         AND user_id = $3::uuid
                   )""",
                session_id,
                org_id,
                user_id,
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
        async with self.pool.acquire() as conn:
            if task_id:
                task = await conn.fetchrow(
                    """SELECT t.request_id
                       FROM tasks t JOIN requests r ON r.id = t.request_id
                       WHERE t.id = $1::uuid AND t.organization_id = $2::uuid
                         AND r.created_by = $3::uuid""",
                    task_id,
                    org_id,
                    user_id,
                )
                if not task:
                    raise ValueError("Task not found")
                return str(task["request_id"])
            if request_id:
                owns_request = await conn.fetchval(
                    """SELECT EXISTS(
                           SELECT 1 FROM requests
                           WHERE id = $1::uuid AND organization_id = $2::uuid
                             AND created_by = $3::uuid
                       )""",
                    request_id,
                    org_id,
                    user_id,
                )
                if not owns_request:
                    raise ValueError("Request not found")
        return request_id

    async def get_owned(self, file_id: str, org_id: str, user_id: str) -> dict[str, Any] | None:
        return await self.repository.get_owned(file_id, org_id, user_id)

    async def read_owned(
        self, file_id: str, org_id: str, user_id: str
    ) -> tuple[dict[str, Any], bytes] | None:
        item = await self.get_owned(file_id, org_id, user_id)
        if not item:
            return None
        try:
            content = await self.registry.get(item["provider"]).get(item["storage_key"])
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
        revision = await self.repository.get_revision_owned(
            file_id, revision_number, org_id, user_id
        )
        if not revision:
            return None
        try:
            content = await self.registry.get(revision["provider"]).get(
                revision["storage_key"]
            )
        except FileNotFoundError:
            return None
        return revision, content

    async def delete_owned(self, file_id: str, org_id: str, user_id: str) -> bool:
        storage_keys = await self.repository.list_storage_keys_owned(file_id, org_id, user_id)
        item = await self.repository.soft_delete(file_id, org_id, user_id)
        if not item:
            return False
        for provider_name, storage_key in storage_keys:
            await self.registry.get(provider_name).delete(storage_key)
        return True

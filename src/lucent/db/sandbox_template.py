"""Database repository for sandbox templates (reusable environment definitions)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


class SandboxTemplateRepository:
    """CRUD for sandbox environment templates."""

    _json_fields = {"setup_commands", "env_vars", "allowed_hosts"}

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    def _parse_row(cls, row: asyncpg.Record) -> dict:
        """Convert a DB row to a dict, deserializing JSONB string fields."""
        d = dict(row)
        for field in cls._json_fields:
            val = d.get(field)
            if isinstance(val, str):
                d[field] = json.loads(val)
        return d

    async def create(
        self,
        *,
        name: str,
        organization_id: str,
        description: str = "",
        image: str = "python:3.12-slim",
        repo_url: str | None = None,
        branch: str | None = None,
        setup_commands: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
        working_dir: str = "/workspace",
        memory_limit: str = "2g",
        cpu_limit: float = 2.0,
        disk_limit: str = "10g",
        network_mode: str = "none",
        allowed_hosts: list[str] | None = None,
        timeout_seconds: int = 1800,
        created_by: str | None = None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO sandbox_templates
                   (name, organization_id, description, image, repo_url, branch,
                    setup_commands, env_vars, working_dir, memory_limit, cpu_limit,
                    disk_limit, network_mode, allowed_hosts, timeout_seconds, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9,
                           $10, $11, $12, $13, $14::jsonb, $15, $16)
                   RETURNING *""",
                name,
                UUID(organization_id),
                description,
                image,
                repo_url,
                branch,
                json.dumps(setup_commands or []),
                json.dumps(env_vars or {}),
                working_dir,
                memory_limit,
                cpu_limit,
                disk_limit,
                network_mode,
                json.dumps(allowed_hosts or []),
                timeout_seconds,
                UUID(created_by) if created_by else None,
            )
            return self._parse_row(row)

    async def get(self, template_id: str, organization_id: str | None = None) -> dict | None:
        async with self.pool.acquire() as conn:
            if organization_id:
                row = await conn.fetchrow(
                    "SELECT * FROM sandbox_templates WHERE id = $1 AND organization_id = $2",
                    UUID(template_id),
                    UUID(organization_id),
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM sandbox_templates WHERE id = $1",
                    UUID(template_id),
                )
            return self._parse_row(row) if row else None

    async def get_by_name(self, name: str, organization_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sandbox_templates WHERE name = $1 AND organization_id = $2",
                name,
                UUID(organization_id),
            )
            return self._parse_row(row) if row else None

    async def list_all(self, organization_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM sandbox_templates
                   WHERE organization_id = $1
                   ORDER BY name""",
                UUID(organization_id),
            )
            return [self._parse_row(r) for r in rows]

    async def update(self, template_id: str, organization_id: str, **kwargs) -> dict | None:
        """Update template fields. Only non-None kwargs are applied."""
        sets = ["updated_at = NOW()"]
        params: list[Any] = [UUID(template_id), UUID(organization_id)]
        idx = 3

        field_map = {
            "name": "name",
            "description": "description",
            "image": "image",
            "repo_url": "repo_url",
            "branch": "branch",
            "working_dir": "working_dir",
            "memory_limit": "memory_limit",
            "cpu_limit": "cpu_limit",
            "disk_limit": "disk_limit",
            "network_mode": "network_mode",
            "timeout_seconds": "timeout_seconds",
        }
        json_fields = {"setup_commands", "env_vars", "allowed_hosts"}

        for key, col in field_map.items():
            if key in kwargs and kwargs[key] is not None:
                sets.append(f"{col} = ${idx}")
                params.append(kwargs[key])
                idx += 1

        for key in json_fields:
            if key in kwargs and kwargs[key] is not None:
                sets.append(f"{key} = ${idx}::jsonb")
                params.append(json.dumps(kwargs[key]))
                idx += 1

        if len(sets) == 1:  # Only updated_at
            return await self.get(template_id, organization_id)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE sandbox_templates SET {", ".join(sets)}
                    WHERE id = $1 AND organization_id = $2
                    RETURNING *""",
                *params,
            )
            return self._parse_row(row) if row else None

    async def delete(self, template_id: str, organization_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM sandbox_templates WHERE id = $1 AND organization_id = $2",
                UUID(template_id),
                UUID(organization_id),
            )
            return result == "DELETE 1"

    def to_sandbox_config(self, template: dict) -> dict:
        """Convert a template record into a sandbox_config dict for daemon dispatch."""
        return {
            "image": template["image"],
            "repo_url": template.get("repo_url"),
            "branch": template.get("branch"),
            "setup_commands": template.get("setup_commands") or [],
            "env_vars": template.get("env_vars") or {},
            "working_dir": template.get("working_dir", "/workspace"),
            "memory_limit": template.get("memory_limit", "2g"),
            "cpu_limit": float(template.get("cpu_limit", 2.0)),
            "disk_limit": template.get("disk_limit", "10g"),
            "network_mode": template.get("network_mode", "none"),
            "allowed_hosts": template.get("allowed_hosts") or [],
            "timeout_seconds": template.get("timeout_seconds", 1800),
        }

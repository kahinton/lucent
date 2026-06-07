"""Database repository for sandbox templates (reusable environment definitions)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg

from lucent.access_control import build_access_clause


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
        owner_user_id: str | None = None,
        owner_group_id: str | None = None,
        scope: str = "instance",
        status: str = "approved",
        proposed_by: str | None = None,
        proposal_reason: str | None = None,
    ) -> dict:
        # Default owner to creator when no explicit ownership is provided.
        # Built-in templates are owned by the system and don't require a user owner.
        if (
            owner_user_id is None
            and owner_group_id is None
            and created_by
            and scope != "built-in"
        ):
            owner_user_id = created_by
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO sandbox_templates
                   (name, organization_id, description, image, repo_url, branch,
                    setup_commands, env_vars, working_dir, memory_limit, cpu_limit,
                    disk_limit, network_mode, allowed_hosts, timeout_seconds, created_by,
                    owner_user_id, owner_group_id, scope, status,
                    proposed_by, proposal_reason)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9,
                           $10, $11, $12, $13, $14::jsonb, $15, $16, $17, $18,
                           $19, $20, $21, $22)
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
                UUID(owner_user_id) if owner_user_id else None,
                UUID(owner_group_id) if owner_group_id else None,
                scope,
                status,
                UUID(proposed_by) if proposed_by else None,
                proposal_reason,
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

    async def get_accessible(
        self,
        template_id: str,
        organization_id: str,
        user_id: str,
        user_role: str | None = None,
    ) -> dict | None:
        """Get template only if user can access it."""
        role = user_role or "member"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM sandbox_templates
                WHERE id = $1
                  AND organization_id = $2
                  AND """ + build_access_clause(resource_type="sandbox_template", uid_param=3, role_param=4) + """
                """,
                UUID(template_id),
                UUID(organization_id),
                UUID(user_id),
                role,
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

    async def list_all(self, organization_id: str, limit: int = 25, offset: int = 0) -> dict:
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM sandbox_templates WHERE organization_id = $1",
                UUID(organization_id),
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """SELECT * FROM sandbox_templates
                   WHERE organization_id = $1
                   ORDER BY name LIMIT $2 OFFSET $3""",
                UUID(organization_id),
                limit,
                offset,
            )
            return {
                "items": [self._parse_row(r) for r in rows],
                "total_count": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(rows) < total_count,
            }

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
        # Ownership fields allow None values (to clear ownership)
        ownership_fields = {"owner_user_id", "owner_group_id"}

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

        for key in ownership_fields:
            if key in kwargs:
                sets.append(f"{key} = ${idx}")
                val = kwargs[key]
                params.append(UUID(val) if val else None)
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

    async def list_accessible_by(
        self,
        user_id: str,
        organization_id: str,
        limit: int = 100,
        offset: int = 0,
        user_role: str | None = None,
    ) -> dict:
        """List sandbox templates accessible to a user: owned, group-owned, or built-in."""
        role = user_role or "member"
        base = """
            FROM sandbox_templates
            WHERE organization_id = $1
            AND """ + build_access_clause(resource_type="sandbox_template", uid_param=2, role_param=3) + "\n"
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total {base}",
                UUID(organization_id), UUID(user_id), role,
            )
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                f"SELECT * {base} ORDER BY name LIMIT $4 OFFSET $5",
                UUID(organization_id), UUID(user_id), role, limit, offset,
            )
        return {
            "items": [self._parse_row(r) for r in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def list_dispatchable(self, organization_id: str) -> list[dict]:
        """Return all approved templates in the org — those a planner is
        allowed to reference when creating a task. Excludes proposed and
        rejected templates."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM sandbox_templates
                   WHERE organization_id = $1
                     AND status = 'approved'
                   ORDER BY scope DESC, name""",
                UUID(organization_id),
            )
        return [self._parse_row(r) for r in rows]

    async def list_proposed(self, organization_id: str) -> list[dict]:
        """Return templates awaiting human approval."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM sandbox_templates
                   WHERE organization_id = $1
                     AND status = 'proposed'
                   ORDER BY created_at""",
                UUID(organization_id),
            )
        return [self._parse_row(r) for r in rows]

    async def set_status(
        self,
        template_id: str,
        organization_id: str,
        status: str,
        reviewed_by: str | None = None,
    ) -> dict | None:
        if status not in {"approved", "proposed", "rejected"}:
            raise ValueError(f"Invalid status: {status}")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE sandbox_templates
                   SET status = $3,
                       reviewed_by = $4,
                       reviewed_at = NOW(),
                       updated_at = NOW()
                   WHERE id = $1 AND organization_id = $2
                   RETURNING *""",
                UUID(template_id),
                UUID(organization_id),
                status,
                UUID(reviewed_by) if reviewed_by else None,
            )
            return self._parse_row(row) if row else None

    async def mark_used(self, template_id: str) -> None:
        """Record that a template was just dispatched. Best-effort."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sandbox_templates SET last_used_at = NOW() WHERE id = $1",
                    UUID(template_id),
                )
        except Exception:
            pass

    async def sync_built_in_templates(
        self, organization_id: str, templates_dir: str
    ) -> int:
        """Load built-in sandbox templates from a directory of YAML files
        and upsert them into the org as ``scope='built-in', status='approved'``.

        Returns the number of templates synced (created or updated).
        """
        from pathlib import Path

        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - PyYAML is a project dep
            return 0

        path = Path(templates_dir)
        if not path.is_dir():
            return 0

        synced = 0
        for tpl_file in sorted(path.glob("*.yaml")):
            try:
                spec = yaml.safe_load(tpl_file.read_text()) or {}
            except yaml.YAMLError:
                continue
            name = spec.get("name")
            if not name:
                continue

            existing = await self.get_by_name(name, organization_id)
            payload = dict(
                description=spec.get("description", ""),
                image=spec.get("image", "python:3.12-slim"),
                repo_url=spec.get("repo_url"),
                branch=spec.get("branch"),
                setup_commands=spec.get("setup_commands") or [],
                env_vars=spec.get("env_vars") or {},
                working_dir=spec.get("working_dir", "/workspace"),
                memory_limit=spec.get("memory_limit", "2g"),
                cpu_limit=float(spec.get("cpu_limit", 2.0)),
                disk_limit=spec.get("disk_limit", "10g"),
                network_mode=spec.get("network_mode", "none"),
                allowed_hosts=spec.get("allowed_hosts") or [],
                timeout_seconds=int(spec.get("timeout_seconds", 1800)),
            )

            if existing:
                # Refresh fields and ensure built-in/approved status.
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE sandbox_templates
                           SET description = $3, image = $4, repo_url = $5, branch = $6,
                               setup_commands = $7::jsonb, env_vars = $8::jsonb,
                               working_dir = $9, memory_limit = $10, cpu_limit = $11,
                               disk_limit = $12, network_mode = $13,
                               allowed_hosts = $14::jsonb, timeout_seconds = $15,
                               scope = 'built-in', status = 'approved',
                               owner_user_id = NULL, owner_group_id = NULL,
                               updated_at = NOW()
                           WHERE id = $1 AND organization_id = $2""",
                        UUID(existing["id"]) if isinstance(existing["id"], str)
                        else existing["id"],
                        UUID(organization_id),
                        payload["description"],
                        payload["image"],
                        payload["repo_url"],
                        payload["branch"],
                        json.dumps(payload["setup_commands"]),
                        json.dumps(payload["env_vars"]),
                        payload["working_dir"],
                        payload["memory_limit"],
                        payload["cpu_limit"],
                        payload["disk_limit"],
                        payload["network_mode"],
                        json.dumps(payload["allowed_hosts"]),
                        payload["timeout_seconds"],
                    )
            else:
                await self.create(
                    name=name,
                    organization_id=organization_id,
                    scope="built-in",
                    status="approved",
                    **payload,
                )
            synced += 1
        return synced

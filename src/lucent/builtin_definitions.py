"""Built-in definition syncing.

Seeds the database with the preconfigured skills, agents, hooks, and sandbox
templates that ship in ``.github/``. This is the single source of truth for that
logic so it can run both at server startup and immediately when a new
organization is created (e.g. first-run user registration), rather than relying
on an arbitrarily-selected organization existing when the server boots.
"""

from __future__ import annotations

from pathlib import Path

from lucent.logging import get_logger

logger = get_logger("builtin_definitions")

# Internal organization used to store system-managed secrets. It must never be
# treated as a user-facing org for built-in definitions.
SYSTEM_ORG_NAME = "__lucent_system__"


def _first_existing_dir(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _repo_github_dir() -> Path:
    """Resolve the repository ``.github`` directory for source checkouts."""
    return Path(__file__).resolve().parents[2] / ".github"


async def sync_built_in_definitions_for_org(pool, org_id: str) -> dict[str, int]:
    """Sync all built-in definitions into a single organization.

    Idempotent: each underlying sync upserts and prunes only built-in rows, so
    repeated calls are safe. Returns a summary of how many of each kind synced.
    """
    from lucent.db.definitions import DefinitionRepository
    from lucent.db.sandbox_template import SandboxTemplateRepository

    repo = DefinitionRepository(pool)
    summary = {"skills": 0, "agents": 0, "hooks": 0, "sandbox_templates": 0}

    github = _repo_github_dir()

    skills_dir = _first_existing_dir(
        [Path("/app/.github/skills"), github / "skills"]
    )
    if skills_dir:
        summary["skills"] = await repo.sync_built_in_skills(org_id, str(skills_dir))

    agents_dir = _first_existing_dir(
        [Path("/app/.github/agents/definitions"), github / "agents" / "definitions"]
    )
    if agents_dir:
        summary["agents"] = await repo.sync_built_in_agents(org_id, str(agents_dir))

    summary["hooks"] = await repo.sync_built_in_hooks(org_id)

    templates_dir = _first_existing_dir(
        [Path("/app/.github/sandbox-templates"), github / "sandbox-templates"]
    )
    if templates_dir:
        tpl_repo = SandboxTemplateRepository(pool)
        summary["sandbox_templates"] = await tpl_repo.sync_built_in_templates(
            org_id, str(templates_dir)
        )

    if any(summary.values()):
        logger.info(
            "Synced built-in definitions for org %s: %s",
            org_id,
            ", ".join(f"{k}={v}" for k, v in summary.items() if v),
        )
    return summary


async def sync_built_in_definitions_for_all_real_orgs(pool) -> None:
    """Sync built-ins into every real (user-facing) organization.

    A "real" org is any organization that has at least one user and is not the
    internal system-secret org. On a clean database this selects nothing — the
    first user's registration seeds their org directly — so built-ins always
    land in the org the user actually uses, not an arbitrary first row.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT o.id
            FROM organizations o
            JOIN users u ON u.organization_id = o.id
            WHERE o.name <> $1
            """,
            SYSTEM_ORG_NAME,
        )
    for row in rows:
        try:
            await sync_built_in_definitions_for_org(pool, str(row["id"]))
        except Exception:
            logger.warning(
                "Failed to sync built-in definitions for org %s",
                row["id"],
                exc_info=True,
            )

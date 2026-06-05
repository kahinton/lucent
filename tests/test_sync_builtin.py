"""Tests for sync_built_in_agents skill_names parsing and junction table sync.

Covers:
- Parsing skill_names from YAML frontmatter in AGENT.md files
- Creating agent_skills junction records from declared skill_names
- Removing stale skill grants when frontmatter changes
- Skipping skill sync for user-created (non-built-in) agents
"""

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from lucent.db import UserRepository
from lucent.db.definitions import DefinitionRepository


@pytest_asyncio.fixture
async def sync_prefix(db_pool):
    """Unique prefix and cleanup for sync tests."""
    test_id = str(uuid4())[:8]
    prefix = f"test_sync_{test_id}_"
    yield prefix
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_skills WHERE agent_id IN "
            "(SELECT id FROM agent_definitions WHERE name LIKE $1)",
            f"{prefix}%",
        )
        await conn.execute(
            "DELETE FROM agent_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM skill_definitions WHERE name LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM users WHERE email LIKE $1", f"{prefix}%"
        )
        await conn.execute(
            "DELETE FROM organizations WHERE name LIKE $1", f"{prefix}%"
        )


@pytest_asyncio.fixture
async def sync_org(db_pool, sync_prefix):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO organizations (name) VALUES ($1) RETURNING id",
            f"{sync_prefix}org",
        )
    return str(row["id"])


@pytest_asyncio.fixture
async def def_repo(db_pool):
    return DefinitionRepository(db_pool)


def _write_agent(agents_dir: Path, name: str, description: str, skill_names: list[str] | None = None) -> Path:
    """Write an AGENT.md file in the expected directory structure."""
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", f"description: '{description}'"]
    if skill_names:
        lines.append("skill_names:")
        for sn in skill_names:
            lines.append(f"  - {sn}")
    lines.extend(["---", "", f"# {name}", "", "Agent content here."])
    agent_file = agent_dir / "AGENT.md"
    agent_file.write_text("\n".join(lines))
    return agent_file


def _write_skill(skills_dir: Path, name: str, description: str = "test skill") -> Path:
    """Write a SKILL.md file in the expected directory structure."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\nname: {name}\ndescription: '{description}'\n---\n\n# {name}\n\nSkill content."
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content)
    return skill_file


class TestSyncSkillNames:
    """Test that sync_built_in_agents parses skill_names and syncs junction table."""

    @pytest.mark.asyncio
    async def test_agent_gets_skills_from_frontmatter(self, db_pool, def_repo, sync_org, sync_prefix):
        """Skills declared in AGENT.md frontmatter are granted to the agent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            skills_dir = base / "skills"
            agents_dir = base / "agents"

            # Create skills first
            skill_a = f"{sync_prefix}skill-a"
            skill_b = f"{sync_prefix}skill-b"
            _write_skill(skills_dir, skill_a)
            _write_skill(skills_dir, skill_b)
            await def_repo.sync_built_in_skills(sync_org, str(skills_dir))

            # Create agent referencing those skills
            agent_name = f"{sync_prefix}agent"
            _write_agent(agents_dir, agent_name, "test agent", [skill_a, skill_b])
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

            # Verify: agent should have both skills granted
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM agent_definitions WHERE name = $1 AND organization_id = $2",
                    agent_name, sync_org,
                )
                assert row is not None
                agent_id = str(row["id"])

                skills = await conn.fetch(
                    """
                    SELECT s.name FROM skill_definitions s
                    JOIN agent_skills ags ON s.id = ags.skill_id
                    WHERE ags.agent_id = $1
                    ORDER BY s.name
                    """,
                    agent_id,
                )
            skill_names_result = [r["name"] for r in skills]
            assert skill_a in skill_names_result
            assert skill_b in skill_names_result

    @pytest.mark.asyncio
    async def test_stale_skills_removed_on_resync(self, db_pool, def_repo, sync_org, sync_prefix):
        """When skill_names changes, stale grants are removed on next sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            skills_dir = base / "skills"
            agents_dir = base / "agents"

            skill_a = f"{sync_prefix}skill-a"
            skill_b = f"{sync_prefix}skill-b"
            _write_skill(skills_dir, skill_a)
            _write_skill(skills_dir, skill_b)
            await def_repo.sync_built_in_skills(sync_org, str(skills_dir))

            agent_name = f"{sync_prefix}agent"
            # First sync: both skills
            _write_agent(agents_dir, agent_name, "test agent", [skill_a, skill_b])
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

            # Second sync: only skill_a (skill_b removed from frontmatter)
            _write_agent(agents_dir, agent_name, "test agent", [skill_a])
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM agent_definitions WHERE name = $1 AND organization_id = $2",
                    agent_name, sync_org,
                )
                agent_id = str(row["id"])
                skills = await conn.fetch(
                    """
                    SELECT s.name FROM skill_definitions s
                    JOIN agent_skills ags ON s.id = ags.skill_id
                    WHERE ags.agent_id = $1
                    """,
                    agent_id,
                )
            skill_names_result = [r["name"] for r in skills]
            assert skill_a in skill_names_result
            assert skill_b not in skill_names_result

    @pytest.mark.asyncio
    async def test_agent_without_skill_names_gets_none(self, db_pool, def_repo, sync_org, sync_prefix):
        """Agent with no skill_names in frontmatter has no grants."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agent_name = f"{sync_prefix}plain"
            _write_agent(agents_dir, agent_name, "no skills")
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM agent_definitions WHERE name = $1 AND organization_id = $2",
                    agent_name, sync_org,
                )
                agent_id = str(row["id"])
                skills = await conn.fetch(
                    "SELECT * FROM agent_skills WHERE agent_id = $1", agent_id
                )
            assert len(skills) == 0

    @pytest.mark.asyncio
    async def test_nonexistent_skill_name_ignored(self, db_pool, def_repo, sync_org, sync_prefix):
        """Skill names that don't exist in the DB are silently skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir) / "agents"
            agent_name = f"{sync_prefix}ghost"
            _write_agent(agents_dir, agent_name, "refs ghost skill", [f"{sync_prefix}nonexistent"])
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id FROM agent_definitions WHERE name = $1 AND organization_id = $2",
                    agent_name, sync_org,
                )
                agent_id = str(row["id"])
                skills = await conn.fetch(
                    "SELECT * FROM agent_skills WHERE agent_id = $1", agent_id
                )
            assert len(skills) == 0

    @pytest.mark.asyncio
    async def test_user_created_agent_not_overwritten(self, db_pool, def_repo, sync_org, sync_prefix):
        """User-created agents (non built-in scope) are not touched by sync."""
        agent_name = f"{sync_prefix}user-agent"

        # Create a user-scoped agent directly in DB (needs owner for check constraint)
        user_repo = UserRepository(db_pool)
        user = await user_repo.create(
            external_id=f"{sync_prefix}user",
            provider="local",
            organization_id=sync_org,
            email=f"{sync_prefix}user@test.com",
            display_name="Sync Test",
            role="admin",
        )
        user_id = str(user["id"])
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_definitions
                    (name, description, content, status, scope, organization_id,
                     created_by, owner_user_id)
                VALUES ($1, 'user agent', '# User agent', 'active', 'instance',
                        $2, $3, $3)
                """,
                agent_name, sync_org, user_id,
            )

        # Try to sync a built-in agent with the same name + skill_names
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            agents_dir = Path(tmpdir) / "agents"
            skill_name = f"{sync_prefix}skill-x"
            _write_skill(skills_dir, skill_name)
            await def_repo.sync_built_in_skills(sync_org, str(skills_dir))

            _write_agent(agents_dir, agent_name, "built-in version", [skill_name])
            await def_repo.sync_built_in_agents(sync_org, str(agents_dir))

        # The user agent should still be 'instance' scope, no skill grants
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT scope, description FROM agent_definitions WHERE name = $1 AND organization_id = $2",
                agent_name, sync_org,
            )
            assert row["scope"] == "instance"
            assert row["description"] == "user agent"

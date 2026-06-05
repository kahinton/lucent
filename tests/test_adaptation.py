"""Tests for the Lucent adaptation pipeline.

Validates:
- Assessment output parsing (<assessment_result> JSON extraction)
- AssessmentResult data model construction
- Agent definition generation via definitions API
- Skill definition generation via definitions API
- Graceful handling of missing/malformed assessment output
- End-to-end pipeline execution (without memory API)
"""

from __future__ import annotations

import json
import logging
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import httpx

from daemon.adaptation import (
    DOMAIN_ARCHETYPES,
    AdaptationPipeline,
    AgentRecommendation,
    AssessmentResult,
    DomainSignalParser,
    SkillRecommendation,
    _build_agent_context,
    _build_skill_context,
    _select_agent_template,
    _select_skill_template,
    parse_assessment_output,
    run_adaptation,
    validate_agent,
    validate_skill,
)

# ============================================================================
# Fixtures
# ============================================================================

SAMPLE_ASSESSMENT_JSON = {
    "domain": {
        "primary": "software",
        "secondary": ["devops"],
        "description": "A Python MCP memory server for LLMs",
    },
    "tech_stack": {
        "languages": ["python"],
        "frameworks": ["fastapi"],
        "infrastructure": ["docker"],
        "databases": ["postgresql"],
        "tools": ["git", "ruff", "pytest"],
    },
    "collaborators": [{"name": "Kyle", "role": "Lead Developer", "preferences": "Concise, direct"}],
    "existing_agents": ["code", "testing"],
    "existing_skills": ["memory-init", "memory-search"],
    "recommended_agents": [
        {
            "name": "security",
            "purpose": "Identify security vulnerabilities and recommend fixes",
            "domain_template": "software",
            "specialization": {"language": "python", "framework": "fastapi"},
        },
        {
            "name": "deployment",
            "purpose": "Manage Docker deployments and CI/CD",
            "domain_template": "software",
            "specialization": {"language": "python"},
        },
    ],
    "recommended_skills": [
        {
            "name": "code-review",
            "purpose": "Review code changes for correctness and style",
            "domain_template": "software",
        },
        {
            "name": "dev-workflow",
            "purpose": "Standard development workflow for this project",
            "domain_template": "software",
        },
    ],
    "guardrails": [
        "Never expose database credentials",
        "Always run ruff before committing",
    ],
    "mcp_servers": {
        "connected": ["memory-server"],
        "recommended": [],
    },
}


def _wrap_assessment(data: dict) -> str:
    """Wrap assessment JSON in the expected XML tags with surrounding text."""
    return textwrap.dedent(f"""\
        I've completed the environment assessment. Here are my findings:

        The workspace is a Python project using FastAPI...

        <assessment_result>
        {json.dumps(data, indent=2)}
        </assessment_result>
    """)


@pytest.fixture(autouse=True)
def _redirect_generated_definition_dirs(tmp_path, monkeypatch):
    """Avoid writing generated adaptation artifacts into the real repo."""
    agents_dir = tmp_path / "agents"
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr("daemon.adaptation.AGENTS_DIR", agents_dir)
    monkeypatch.setattr("daemon.adaptation.SKILLS_DIR", skills_dir)
    return {"agents_dir": agents_dir, "skills_dir": skills_dir}


@pytest.fixture
def sample_raw_output() -> str:
    return _wrap_assessment(SAMPLE_ASSESSMENT_JSON)


@pytest.fixture
def sample_assessment() -> AssessmentResult:
    return AssessmentResult.from_json(SAMPLE_ASSESSMENT_JSON)


# ============================================================================
# parse_assessment_output
# ============================================================================


class TestParseAssessmentOutput:
    """Tests for extracting JSON from <assessment_result> tags."""

    def test_parses_valid_output(self, sample_raw_output: str):
        result = parse_assessment_output(sample_raw_output)
        assert result is not None
        assert result.domain_primary == "software"
        assert result.domain_secondary == ["devops"]
        assert len(result.recommended_agents) == 2
        assert len(result.recommended_skills) == 2

    def test_returns_none_for_no_tags(self):
        result = parse_assessment_output("No assessment tags here")
        assert result is None

    def test_returns_none_for_empty_string(self):
        result = parse_assessment_output("")
        assert result is None

    def test_returns_none_for_malformed_json(self):
        raw = "<assessment_result>\n{not valid json}\n</assessment_result>"
        result = parse_assessment_output(raw)
        assert result is None

    def test_returns_none_for_empty_tags(self):
        raw = "<assessment_result>\n\n</assessment_result>"
        result = parse_assessment_output(raw)
        assert result is None

    def test_handles_extra_whitespace_in_tags(self):
        raw = "<assessment_result>  \n  {}  \n  </assessment_result>"
        result = parse_assessment_output(raw)
        assert result is not None
        assert result.domain_primary == "software"  # default

    def test_parses_minimal_json(self):
        raw = _wrap_assessment({})
        result = parse_assessment_output(raw)
        assert result is not None
        assert result.domain_primary == "software"  # default
        assert result.recommended_agents == []
        assert result.recommended_skills == []

    def test_ignores_text_outside_tags(self):
        raw = (
            "Some preamble text\n"
            '<assessment_result>{"domain": {"primary": "legal"}}</assessment_result>\n'
            "Some epilogue text"
        )
        result = parse_assessment_output(raw)
        assert result is not None
        assert result.domain_primary == "legal"


# ============================================================================
# AssessmentResult.from_json
# ============================================================================


class TestAssessmentResultFromJson:
    """Tests for the data model construction."""

    def test_full_data(self):
        result = AssessmentResult.from_json(SAMPLE_ASSESSMENT_JSON)
        assert result.domain_primary == "software"
        assert result.domain_description == "A Python MCP memory server for LLMs"
        assert result.tech_stack["languages"] == ["python"]
        assert len(result.collaborators) == 1
        assert result.collaborators[0]["name"] == "Kyle"
        assert result.existing_agents == ["code", "testing"]
        assert result.existing_skills == ["memory-init", "memory-search"]
        assert len(result.recommended_agents) == 2
        assert result.recommended_agents[0].name == "security"
        assert result.recommended_agents[0].domain_template == "software"
        assert result.recommended_agents[0].specialization["language"] == "python"
        assert len(result.recommended_skills) == 2
        assert result.recommended_skills[0].name == "code-review"
        assert result.guardrails == [
            "Never expose database credentials",
            "Always run ruff before committing",
        ]

    def test_defaults_for_empty_data(self):
        result = AssessmentResult.from_json({})
        assert result.domain_primary == "software"
        assert result.domain_secondary == []
        assert result.domain_description == ""
        assert result.tech_stack == {}
        assert result.collaborators == []
        assert result.existing_agents == []
        assert result.recommended_agents == []
        assert result.recommended_skills == []
        assert result.guardrails == []

    def test_agent_recommendation_defaults(self):
        data = {"recommended_agents": [{"name": "test-agent"}]}
        result = AssessmentResult.from_json(data)
        agent = result.recommended_agents[0]
        assert agent.name == "test-agent"
        assert agent.purpose == ""
        assert agent.domain_template == "general"
        assert agent.specialization == {}

    def test_skill_recommendation_defaults(self):
        data = {"recommended_skills": [{"name": "test-skill"}]}
        result = AssessmentResult.from_json(data)
        skill = result.recommended_skills[0]
        assert skill.name == "test-skill"
        assert skill.purpose == ""
        assert skill.domain_template == "general"


# ============================================================================
# Template Selection
# ============================================================================


class TestTemplateSelection:
    """Tests for template file selection logic."""

    def test_software_agent_template(self):
        rec = AgentRecommendation(
            name="code-review", purpose="Review code", domain_template="software"
        )
        assert _select_agent_template(rec) == "agents/software_agent.md.j2"

    def test_support_agent_template(self):
        rec = AgentRecommendation(name="triage", purpose="Triage issues", domain_template="support")
        assert _select_agent_template(rec) == "agents/support_agent.md.j2"

    def test_research_agent_template(self):
        rec = AgentRecommendation(
            name="literature", purpose="Literature review", domain_template="research"
        )
        assert _select_agent_template(rec) == "agents/research_agent.md.j2"

    def test_general_agent_template(self):
        rec = AgentRecommendation(
            name="generic", purpose="General tasks", domain_template="general"
        )
        assert _select_agent_template(rec) == "agents/base_agent.md.j2"

    def test_unknown_domain_falls_back_to_base(self):
        rec = AgentRecommendation(name="custom", purpose="Custom", domain_template="unknown-domain")
        assert _select_agent_template(rec) == "agents/base_agent.md.j2"

    def test_software_code_review_skill_template(self):
        rec = SkillRecommendation(
            name="code-review", purpose="Review code", domain_template="software"
        )
        assert _select_skill_template(rec) == "skills/software_code_review.md.j2"

    def test_software_dev_workflow_skill_template(self):
        rec = SkillRecommendation(
            name="dev-workflow", purpose="Dev workflow", domain_template="software"
        )
        assert _select_skill_template(rec) == "skills/software_dev_workflow.md.j2"

    def test_general_skill_template(self):
        rec = SkillRecommendation(
            name="unknown-skill", purpose="Something", domain_template="general"
        )
        assert _select_skill_template(rec) == "skills/base_skill.md.j2"

    def test_unknown_skill_falls_back_to_base(self):
        rec = SkillRecommendation(name="weird", purpose="Weird", domain_template="unknown")
        assert _select_skill_template(rec) == "skills/base_skill.md.j2"

    def test_legal_agent_template(self):
        rec = AgentRecommendation(
            name="legal-research",
            purpose="Legal research",
            domain_template="legal",
        )
        assert _select_agent_template(rec) == "agents/legal_agent.md.j2"

    def test_legal_case_analysis_skill_template(self):
        rec = SkillRecommendation(
            name="case-analysis",
            purpose="Case analysis",
            domain_template="legal",
        )
        assert _select_skill_template(rec) == "skills/legal_case_analysis.md.j2"

    def test_legal_compliance_skill_template(self):
        rec = SkillRecommendation(
            name="compliance-review", purpose="Compliance review", domain_template="legal"
        )
        assert _select_skill_template(rec) == "skills/legal_compliance.md.j2"


# ============================================================================
# Context Building
# ============================================================================


class TestContextBuilding:
    """Tests for template context construction."""

    def test_agent_context_with_specialization(self, sample_assessment: AssessmentResult):
        rec = AgentRecommendation(
            name="code-review",
            purpose="Review code",
            domain_template="software",
            specialization={"language": "python", "framework": "fastapi"},
        )
        ctx = _build_agent_context(rec, sample_assessment)
        assert ctx["agent_name"] == "Code Review"
        assert ctx["language"] == "python"
        assert ctx["framework"] == "fastapi"
        assert ctx["linter_config"] == "ruff (pyproject.toml)"
        assert ctx["primary_tag"] == "code-review"

    def test_agent_context_falls_back_to_tech_stack(self, sample_assessment: AssessmentResult):
        rec = AgentRecommendation(
            name="security",
            purpose="Security scanning",
            domain_template="software",
        )
        ctx = _build_agent_context(rec, sample_assessment)
        assert ctx["language"] == "python"  # from tech_stack
        assert ctx["framework"] == "fastapi"  # from tech_stack

    def test_agent_context_with_no_tech_stack(self):
        assessment = AssessmentResult()
        rec = AgentRecommendation(name="generic", purpose="Do things", domain_template="general")
        ctx = _build_agent_context(rec, assessment)
        assert ctx["language"] == ""
        assert ctx["framework"] == ""
        assert ctx["linter_config"] == "project linter"

    def test_agent_context_typescript_linter(self):
        assessment = AssessmentResult(tech_stack={"languages": ["typescript"]})
        rec = AgentRecommendation(
            name="code-review",
            purpose="Review",
            domain_template="software",
            specialization={"language": "typescript"},
        )
        ctx = _build_agent_context(rec, assessment)
        assert ctx["linter_config"] == "eslint/biome"

    def test_agent_context_uses_guardrails(self, sample_assessment: AssessmentResult):
        rec = AgentRecommendation(name="test", purpose="Test", domain_template="software")
        ctx = _build_agent_context(rec, sample_assessment)
        assert "Never expose database credentials" in ctx["guardrails"]

    def test_agent_context_default_guardrails(self):
        assessment = AssessmentResult()
        rec = AgentRecommendation(name="test", purpose="Test", domain_template="general")
        ctx = _build_agent_context(rec, assessment)
        assert len(ctx["guardrails"]) == 2
        assert "Follow established patterns and conventions" in ctx["guardrails"]

    def test_skill_context_basic(self, sample_assessment: AssessmentResult):
        rec = SkillRecommendation(
            name="code-review", purpose="Review code", domain_template="software"
        )
        ctx = _build_skill_context(rec, sample_assessment)
        assert ctx["skill_name"] == "code-review"
        assert ctx["title"] == "Code Review"
        assert ctx["language"] == "python"
        assert ctx["framework"] == "fastapi"
        assert len(ctx["steps"]) == 3
        assert len(ctx["triggers"]) == 2


# ============================================================================
# Agent Generation
# ============================================================================


def _mock_httpx_client(existing_agents=None, existing_skills=None):
    """Create a mock httpx.AsyncClient that simulates the definitions API."""
    existing_agents = existing_agents or []
    existing_skills = existing_skills or []
    created_agents = []
    created_skills = []

    async def mock_get(url, **kwargs):
        resp = MagicMock()
        if "/definitions/agents" in url:
            resp.status_code = 200
            resp.json.return_value = [{"name": n} for n in existing_agents]
        elif "/definitions/skills" in url:
            resp.status_code = 200
            resp.json.return_value = [{"name": n} for n in existing_skills]
        else:
            resp.status_code = 404
        return resp

    async def mock_post(url, **kwargs):
        resp = MagicMock()
        body = kwargs.get("json", {})
        if "/definitions/agents" in url:
            created_agents.append(body)
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
        elif "/definitions/skills" in url:
            created_skills.append(body)
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
        else:
            resp.status_code = 404
        return resp

    client = AsyncMock()
    client.get = mock_get
    client.post = mock_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    return client, created_agents, created_skills


class TestAgentGeneration:
    """Tests for generating agent definitions via the API."""

    @pytest.mark.asyncio
    async def test_proposes_agent_definitions(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        client, created_agents, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) == 2
        assert len(created_agents) == 2
        for body in created_agents:
            assert "name" in body
            assert "content" in body
            assert "description" in body
            assert body["scope"] == "instance"
            assert body["status"] == "proposed"
            artifact_path = _redirect_generated_definition_dirs["agents_dir"] / f"{body['name']}.md"
            assert artifact_path.exists()
            assert artifact_path.read_text(encoding="utf-8").strip()

    @pytest.mark.asyncio
    async def test_skips_existing_agents(self, sample_assessment: AssessmentResult):
        client, created_agents, _ = _mock_httpx_client(existing_agents=["security"])
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Only deployment should be proposed, security was skipped
        assert len(names) == 1
        assert names[0] == "deployment"

    @pytest.mark.asyncio
    async def test_generates_nothing_with_no_recommendations(self):
        assessment = AssessmentResult()
        client, created_agents, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert names == []
        assert created_agents == []

    @pytest.mark.asyncio
    async def test_generates_nothing_without_api_config(self, sample_assessment: AssessmentResult):
        pipeline = AdaptationPipeline(sample_assessment)
        names = await pipeline.generate_agents()
        assert names == []

    @pytest.mark.asyncio
    async def test_cleans_up_agent_artifact_when_db_persist_fails(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert names == []
        for rec in sample_assessment.recommended_agents:
            artifact_path = _redirect_generated_definition_dirs["agents_dir"] / f"{rec.name}.md"
            assert not artifact_path.exists()

    @pytest.mark.asyncio
    async def test_duplicate_prevention_on_second_run(self, sample_assessment: AssessmentResult):
        existing_agents: set[str] = set()
        created_agents: list[dict] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = [{"name": n} for n in sorted(existing_agents)]
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            created_agents.append(body)
            existing_agents.add(body["name"])
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            first = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )
            second = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(first) == 2
        assert second == []
        assert len(created_agents) == 2


# ============================================================================
# Skill Generation
# ============================================================================


class TestSkillGeneration:
    """Tests for generating skill definitions via the API."""

    @pytest.mark.asyncio
    async def test_proposes_skill_definitions(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        client, _, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) == 2
        assert len(created_skills) == 2
        for body in created_skills:
            assert "name" in body
            assert "content" in body
            assert body["scope"] == "instance"
            assert body["status"] == "proposed"
            artifact_path = (
                _redirect_generated_definition_dirs["skills_dir"] / body["name"] / "SKILL.md"
            )
            assert artifact_path.exists()
            assert artifact_path.read_text(encoding="utf-8").strip()

    @pytest.mark.asyncio
    async def test_skips_existing_skills(self, sample_assessment: AssessmentResult):
        client, _, created_skills = _mock_httpx_client(existing_skills=["code-review"])
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Only dev-workflow should be proposed
        assert len(names) == 1
        assert names[0] == "dev-workflow"

    @pytest.mark.asyncio
    async def test_generates_nothing_with_no_recommendations(self):
        assessment = AssessmentResult()
        client, _, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert names == []
        assert created_skills == []

    @pytest.mark.asyncio
    async def test_cleans_up_skill_artifact_when_db_persist_fails(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert names == []
        for rec in sample_assessment.recommended_skills:
            artifact_path = (
                _redirect_generated_definition_dirs["skills_dir"] / rec.name / "SKILL.md"
            )
            assert not artifact_path.exists()

    @pytest.mark.asyncio
    async def test_duplicate_prevention_on_second_run(self, sample_assessment: AssessmentResult):
        existing_skills: set[str] = set()
        created_skills: list[dict] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = [{"name": n} for n in sorted(existing_skills)]
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            created_skills.append(body)
            existing_skills.add(body["name"])
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            first = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )
            second = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(first) == 2
        assert second == []
        assert len(created_skills) == 2


# ============================================================================
# Proposals Visibility
# ============================================================================


class TestProposalsVisibility:
    """Generated definitions should appear in the proposals listing."""

    @pytest.mark.asyncio
    async def test_generated_definitions_appear_in_proposals(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        db_agents: list[dict] = []
        db_skills: list[dict] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/definitions/agents" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": row["name"]} for row in db_agents]
            elif "/definitions/skills" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": row["name"]} for row in db_skills]
            elif "/definitions/proposals" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "agents": [dict(row) for row in db_agents],
                    "skills": [dict(row) for row in db_skills],
                    "mcp_servers": [],
                    "total": len(db_agents) + len(db_skills),
                }
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            row = {
                "id": f"test-{body.get('name', 'id')}",
                "name": body["name"],
                "description": body.get("description", ""),
                "content": body["content"],
                "scope": body.get("scope", "instance"),
                "status": body.get("status", "proposed"),
            }
            if "/definitions/agents" in url:
                db_agents.append(row)
                resp.status_code = 201
                resp.json.return_value = row
            elif "/definitions/skills" in url:
                db_skills.append(row)
                resp.status_code = 201
                resp.json.return_value = row
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

            proposals_resp = await client.get("http://test/api/definitions/proposals")

        assert proposals_resp.status_code == 200
        proposals = proposals_resp.json()
        proposed_agent_names = {item["name"] for item in proposals["agents"]}
        proposed_skill_names = {item["name"] for item in proposals["skills"]}

        assert set(result["agents_proposed"]).issubset(proposed_agent_names)
        assert set(result["skills_proposed"]).issubset(proposed_skill_names)
        assert all(item["scope"] == "instance" for item in proposals["agents"])
        assert all(item["status"] == "proposed" for item in proposals["agents"])
        assert all(item["scope"] == "instance" for item in proposals["skills"])
        assert all(item["status"] == "proposed" for item in proposals["skills"])


# ============================================================================
# Pipeline Summary
# ============================================================================


class TestPipelineSummary:
    """Tests for the build_adaptation_summary method."""

    def test_summary_includes_domain(self, sample_assessment: AssessmentResult):
        pipeline = AdaptationPipeline(sample_assessment)
        pipeline.generated_agents = ["security"]
        pipeline.generated_skills = ["code-review"]
        summary = pipeline.build_adaptation_summary()
        assert "software" in summary
        assert "security" in summary
        assert "code-review" in summary

    def test_summary_includes_tech_stack(self, sample_assessment: AssessmentResult):
        pipeline = AdaptationPipeline(sample_assessment)
        summary = pipeline.build_adaptation_summary()
        assert "python" in summary
        assert "fastapi" in summary

    def test_summary_with_empty_assessment(self):
        assessment = AssessmentResult()
        pipeline = AdaptationPipeline(assessment)
        summary = pipeline.build_adaptation_summary()
        assert "software" in summary  # default domain


# ============================================================================
# Agent Registry Metadata
# ============================================================================


class TestAgentRegistryMetadata:
    """Tests for the registry metadata builder."""

    def test_registry_includes_existing_and_generated(self, sample_assessment: AssessmentResult):
        pipeline = AdaptationPipeline(sample_assessment)
        pipeline.generated_agents = ["security"]
        meta = pipeline.build_agent_registry_metadata()
        assert meta["domain"] == "software"
        assert "security" in meta["agent_registry"]
        assert meta["agent_registry"]["security"]["source"] == "generated"
        assert "code" in meta["agent_registry"]
        assert meta["agent_registry"]["code"]["source"] == "existing"
        assert "security" in meta["generated_agents"]


# ============================================================================
# End-to-end Pipeline
# ============================================================================


class TestPipelineEndToEnd:
    """Tests for the full pipeline run."""

    @pytest.mark.asyncio
    async def test_run_without_memory_api(
        self,
        sample_assessment: AssessmentResult,
    ):
        client, created_agents, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert result["domain"] == "software"
        assert result["requires_approval"] is True
        # Explicit recommendations + archetype fills
        assert len(result["agents_proposed"]) >= 2
        assert len(result["skills_proposed"]) >= 2
        assert "validation_warnings" in result

    @pytest.mark.asyncio
    async def test_run_reports_skipped(self, sample_assessment: AssessmentResult):
        # Pre-create security agent and code-review skill in mock API
        client, _, _ = _mock_httpx_client(
            existing_agents=["security"],
            existing_skills=["code-review"],
        )
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # security agent and code-review skill were pre-existing, so should be skipped
        assert "security" in result["agents_skipped"]
        assert "code-review" in result["skills_skipped"]
        # deployment agent should still be proposed (not pre-existing)
        assert "deployment" in result["agents_proposed"]


# ============================================================================
# run_adaptation convenience function
# ============================================================================


class TestRunAdaptation:
    """Tests for the run_adaptation convenience function."""

    @pytest.mark.asyncio
    async def test_success(self, sample_raw_output: str):
        client, _, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            result = await run_adaptation(
                sample_raw_output,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert result is not None
        assert result["domain"] == "software"
        assert result["requires_approval"] is True
        # At least the 2 explicit + archetype additions
        assert len(result["agents_proposed"]) >= 2

    @pytest.mark.asyncio
    async def test_returns_none_for_bad_input(self):
        result = await run_adaptation("no assessment here")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_malformed_json(self):
        result = await run_adaptation("<assessment_result>{bad json}</assessment_result>")
        assert result is None


# ============================================================================
# Domain Signal Parser
# ============================================================================


class TestDomainSignalParser:
    """Tests for extracting and scoring domain signals."""

    def test_software_domain_detection(self):
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A Python web application",
            tech_stack={
                "languages": ["python"],
                "frameworks": ["fastapi"],
                "tools": ["pytest", "docker", "git"],
            },
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "software"
        assert signals.domain_scores["software"] > 0

    def test_legal_domain_detection(self):
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="A law firm case management system with contracts and compliance",
            tech_stack={"tools": ["legal-research-db"]},
            collaborators=[{"name": "Jane", "role": "Attorney"}],
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "legal"
        assert signals.domain_scores["legal"] > 0

    def test_support_domain_detection(self):
        assessment = AssessmentResult(
            domain_primary="support",
            domain_description="Customer support helpdesk with ticketing and escalation",
            tech_stack={"tools": ["jira", "zendesk"]},
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "support"
        assert signals.domain_scores["support"] > 0

    def test_extracts_tech_indicators(self):
        assessment = AssessmentResult(
            tech_stack={
                "languages": ["python", "typescript"],
                "frameworks": ["fastapi"],
                "infrastructure": ["docker", "kubernetes"],
                "databases": ["postgresql"],
            },
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert "python" in signals.tech_indicators
        assert "typescript" in signals.tech_indicators
        assert "fastapi" in signals.tech_indicators
        assert "docker" in signals.tech_indicators

    def test_extracts_role_indicators(self):
        assessment = AssessmentResult(
            collaborators=[
                {"name": "Kyle", "role": "Lead Developer"},
                {"name": "Jane", "role": "QA Engineer"},
            ],
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert "Lead Developer" in signals.role_indicators
        assert "QA Engineer" in signals.role_indicators

    def test_extracts_tool_indicators(self):
        assessment = AssessmentResult(
            tech_stack={"tools": ["git", "docker"]},
            mcp_servers={"connected": ["memory-server"], "recommended": ["code-server"]},
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert "git" in signals.tool_indicators
        assert "memory-server" in signals.tool_indicators

    def test_secondary_domains_detected(self):
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description=(
                "A software project with compliance monitoring and regulatory checks"
            ),
            tech_stack={"languages": ["python"], "tools": ["pytest"]},
            guardrails=["Ensure regulatory compliance"],
        )
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "software"
        # Legal should appear as secondary due to "compliance" and "regulatory"
        assert "legal" in signals.secondary_domains

    def test_empty_assessment_defaults_to_software(self):
        assessment = AssessmentResult()
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "software"

    def test_explicit_non_software_domain_preserved(self):
        assessment = AssessmentResult(domain_primary="research")
        parser = DomainSignalParser()
        signals = parser.parse(assessment)
        assert signals.primary_domain == "research"


# ============================================================================
# Domain Archetypes
# ============================================================================


class TestDomainArchetypes:
    """Tests for the archetype registry structure."""

    def test_software_archetypes_exist(self):
        assert "software" in DOMAIN_ARCHETYPES
        assert len(DOMAIN_ARCHETYPES["software"]["agents"]) >= 3
        assert len(DOMAIN_ARCHETYPES["software"]["skills"]) >= 1

    def test_legal_archetypes_exist(self):
        assert "legal" in DOMAIN_ARCHETYPES
        agents = DOMAIN_ARCHETYPES["legal"]["agents"]
        agent_names = [a.name for a in agents]
        assert "legal-research" in agent_names
        assert "contract-review" in agent_names
        assert "compliance" in agent_names

    def test_support_archetypes_exist(self):
        assert "support" in DOMAIN_ARCHETYPES
        agents = DOMAIN_ARCHETYPES["support"]["agents"]
        agent_names = [a.name for a in agents]
        assert "triage" in agent_names
        assert "incident-response" in agent_names

    def test_research_archetypes_exist(self):
        assert "research" in DOMAIN_ARCHETYPES
        agents = DOMAIN_ARCHETYPES["research"]["agents"]
        agent_names = [a.name for a in agents]
        assert "literature-review" in agent_names

    def test_archetype_agents_have_required_fields(self):
        for domain, archetypes in DOMAIN_ARCHETYPES.items():
            for agent in archetypes["agents"]:
                assert agent.name, f"Agent in {domain} missing name"
                assert agent.purpose, f"Agent {agent.name} in {domain} missing purpose"
                assert agent.domain_template, f"Agent {agent.name} in {domain} missing template"

    def test_archetype_skills_have_required_fields(self):
        for domain, archetypes in DOMAIN_ARCHETYPES.items():
            for skill in archetypes["skills"]:
                assert skill.name, f"Skill in {domain} missing name"
                assert skill.purpose, f"Skill {skill.name} in {domain} missing purpose"
                assert skill.domain_template, f"Skill {skill.name} in {domain} missing template"


# ============================================================================
# Validation
# ============================================================================


class TestValidation:
    """Tests for agent and skill validation."""

    def test_valid_agent_passes(self):
        content = textwrap.dedent("""\
            # Test Agent

            ## Your Role
            You review code.

            ## Domain Context
            Software project.

            ## Tools
            - grep/glob

            ## Guardrails
            - Don't break things
            - Tag all output with 'daemon'

            ## Feedback Protocol
            Tag with needs-review.
        """)
        result = validate_agent(content, "test")
        assert result.valid
        assert result.errors == []

    def test_empty_agent_fails(self):
        result = validate_agent("", "empty")
        assert not result.valid
        assert any("empty" in e.lower() for e in result.errors)

    def test_agent_missing_heading_fails(self):
        result = validate_agent("no heading here, just text about role and domain", "bad")
        assert not result.valid

    def test_agent_missing_sections_warns(self):
        content = "# Agent\nSome content about reviewing things.\n"
        result = validate_agent(content, "minimal")
        assert result.valid  # Warnings don't fail validation
        assert len(result.warnings) > 0

    def test_valid_skill_passes(self):
        content = textwrap.dedent("""\
            ---
            name: test-skill
            description: 'A test skill'
            ---

            # Test Skill

            ## When to Use
            - When testing

            ## Process Steps
            1. Do the thing

            ## Best Practices
            - Be good

            ## Common Pitfalls
            - Avoid this
        """)
        result = validate_skill(content, "test-skill")
        assert result.valid
        assert result.errors == []

    def test_empty_skill_fails(self):
        result = validate_skill("", "empty")
        assert not result.valid

    def test_skill_missing_frontmatter_fails(self):
        content = "# No Frontmatter\nJust some content about process steps."
        result = validate_skill(content, "bad")
        assert not result.valid
        assert any("frontmatter" in e.lower() for e in result.errors)

    def test_skill_missing_name_in_frontmatter_fails(self):
        content = "---\ndescription: 'test'\n---\n# Skill\n"
        result = validate_skill(content, "bad")
        assert not result.valid
        assert any("name" in e.lower() for e in result.errors)

    def test_skill_missing_sections_warns(self):
        content = "---\nname: test\ndescription: 'test'\n---\n# Skill\nContent.\n"
        result = validate_skill(content, "minimal")
        assert result.valid
        assert len(result.warnings) > 0


# ============================================================================
# Archetype Application
# ============================================================================


class TestArchetypeApplication:
    """Tests for automatic archetype-based recommendations."""

    def test_archetypes_fill_gaps_for_software(self):
        """When assessment has no explicit recommendations, archetypes fill in."""
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A Python web app",
            tech_stack={"languages": ["python"]},
        )
        pipeline = AdaptationPipeline(assessment)
        pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "code-review" in agent_names
        assert "security" in agent_names
        skill_names = [r.name for r in assessment.recommended_skills]
        assert "code-review" in skill_names

    def test_archetypes_fill_gaps_for_legal(self):
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="A law firm",
        )
        pipeline = AdaptationPipeline(assessment)
        pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "legal-research" in agent_names
        assert "contract-review" in agent_names
        assert "compliance" in agent_names

    def test_archetypes_dont_duplicate_existing(self):
        """Archetypes skip agents/skills that already exist."""
        assessment = AssessmentResult(
            domain_primary="software",
            existing_agents=["code-review", "security"],
            existing_skills=["code-review"],
        )
        pipeline = AdaptationPipeline(assessment)
        pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "code-review" not in agent_names  # Already exists
        assert "security" not in agent_names  # Already exists
        assert "testing" in agent_names  # New from archetype

    def test_archetypes_dont_duplicate_recommendations(self):
        """Archetypes skip agents that are already recommended."""
        assessment = AssessmentResult(
            domain_primary="software",
            recommended_agents=[
                AgentRecommendation(
                    name="code-review",
                    purpose="Custom review",
                    domain_template="software",
                ),
            ],
        )
        pipeline = AdaptationPipeline(assessment)
        pipeline.apply_archetypes()

        # code-review should appear exactly once
        agent_names = [r.name for r in assessment.recommended_agents]
        assert agent_names.count("code-review") == 1
        # But others from archetype should be added
        assert "security" in agent_names

    def test_cross_domain_archetypes(self):
        """Secondary domains contribute archetypes too."""
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A compliance monitoring tool with regulatory checks",
            tech_stack={"languages": ["python"], "tools": ["pytest"]},
            guardrails=["Ensure regulatory compliance"],
        )
        pipeline = AdaptationPipeline(assessment)
        pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        # Should have software agents
        assert "code-review" in agent_names
        # Should also have legal agents from secondary domain detection
        assert "legal-research" in agent_names or "compliance" in agent_names


# ============================================================================
# Legal Template Generation
# ============================================================================


class TestLegalTemplateGeneration:
    """Tests for legal domain template rendering."""

    @pytest.mark.asyncio
    async def test_legal_agent_generates(self):
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="Corporate law firm",
            recommended_agents=[
                AgentRecommendation(
                    name="legal-research",
                    purpose="Research legal precedents and regulations",
                    domain_template="legal",
                ),
            ],
            guardrails=["Maintain client confidentiality"],
        )
        client, created_agents, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) == 1
        assert names[0] == "legal-research"
        content = created_agents[0]["content"]
        assert "Legal Research" in content
        assert "legal environment" in content.lower()
        assert "privilege" in content.lower()
        assert "jurisdiction" in content.lower()
        assert "daemon" in content

    @pytest.mark.asyncio
    async def test_legal_skill_generates(self):
        assessment = AssessmentResult(
            domain_primary="legal",
            recommended_skills=[
                SkillRecommendation(
                    name="case-analysis",
                    purpose="Structured legal case analysis",
                    domain_template="legal",
                ),
                SkillRecommendation(
                    name="compliance-review",
                    purpose="Regulatory compliance review",
                    domain_template="legal",
                ),
            ],
        )
        client, _, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) == 2
        assert "case-analysis" in names
        assert "compliance-review" in names
        for body in created_skills:
            content = body["content"]
            assert "---" in content  # Frontmatter
            assert "name:" in content


# ============================================================================
# Pipeline with Archetypes (End-to-End)
# ============================================================================


class TestPipelineWithArchetypes:
    """Tests for the full pipeline including archetype application."""

    @pytest.mark.asyncio
    async def test_legal_domain_end_to_end(self):
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="Corporate law practice with contracts and compliance",
        )

        client, created_agents, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert result["domain"] == "legal"
        assert result["requires_approval"] is True
        # Archetypes should have filled in legal agents/skills
        assert len(result["agents_proposed"]) >= 3  # legal-research, contract-review, compliance
        assert len(result["skills_proposed"]) >= 2  # case-analysis, compliance-review
        assert "validation_warnings" in result

    @pytest.mark.asyncio
    async def test_support_domain_end_to_end(self):
        assessment = AssessmentResult(
            domain_primary="support",
            domain_description="Customer support team with ticketing",
        )

        client, _, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert result["domain"] == "support"
        assert len(result["agents_proposed"]) >= 3  # triage, incident-response, knowledge-base
        assert len(result["skills_proposed"]) >= 1  # triage

    @pytest.mark.asyncio
    async def test_pipeline_includes_validation_warnings(self):
        assessment = AssessmentResult(
            domain_primary="software",
            recommended_agents=[
                AgentRecommendation(name="test-agent", purpose="Test", domain_template="software"),
            ],
        )

        client, _, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # validation_warnings key should exist (may be empty if templates pass)
        assert "validation_warnings" in result

    @pytest.mark.asyncio
    async def test_pipeline_signals_stored_in_metadata(self):
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A Python project",
            tech_stack={"languages": ["python"]},
        )

        client, _, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )
            metadata = pipeline.build_agent_registry_metadata()

        assert "domain_signals" in metadata
        assert metadata["domain_signals"]["primary"] == "software"
        assert "scores" in metadata["domain_signals"]


# ============================================================================
# DB Persistence Contract — end-to-end verification
# ============================================================================


class TestDBPersistenceContract:
    """Verify the full contract between the adaptation pipeline and the definitions DB.

    These tests strengthen coverage of the 4 key persistence guarantees:
    1. Happy path: files AND DB rows created with correct scope/status/content
    2. Duplicate prevention: no duplicate definitions on repeated runs
    3. Proposals visibility: generated definitions appear in proposals endpoint
    4. Atomicity: file-write and DB-write fail or succeed together

    Existing tests in TestAgentGeneration/TestSkillGeneration/TestProposalsVisibility
    cover individual aspects; this class focuses on gaps: content consistency,
    reverse atomicity (file failure → no DB), and end-to-end pipeline.run() flow.
    """

    @pytest.mark.asyncio
    async def test_agent_file_and_db_content_are_identical(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        """The rendered template content written to disk must exactly match the POST body."""
        client, created_agents, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) >= 2
        for body in created_agents:
            artifact_path = _redirect_generated_definition_dirs["agents_dir"] / f"{body['name']}.md"
            file_content = artifact_path.read_text(encoding="utf-8")
            assert file_content == body["content"], (
                f"Content mismatch for agent '{body['name']}': "
                f"file has {len(file_content)} chars, DB payload has {len(body['content'])} chars"
            )

    @pytest.mark.asyncio
    async def test_skill_file_and_db_content_are_identical(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        """The rendered template content written to disk must exactly match the POST body."""
        client, _, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert len(names) >= 2
        for body in created_skills:
            artifact_path = (
                _redirect_generated_definition_dirs["skills_dir"] / body["name"] / "SKILL.md"
            )
            file_content = artifact_path.read_text(encoding="utf-8")
            assert file_content == body["content"], (
                f"Content mismatch for skill '{body['name']}'"
            )

    @pytest.mark.asyncio
    async def test_agent_description_from_recommendation_purpose(
        self,
        sample_assessment: AssessmentResult,
    ):
        """POST body 'description' should be set from the recommendation's 'purpose'."""
        client, created_agents, _ = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Build a map of recommendation name → purpose
        purpose_by_name = {r.name: r.purpose for r in sample_assessment.recommended_agents}
        for body in created_agents:
            expected_desc = purpose_by_name.get(body["name"])
            if expected_desc is not None:
                assert body["description"] == expected_desc, (
                    f"Agent '{body['name']}' description should be '{expected_desc}', "
                    f"got '{body['description']}'"
                )

    @pytest.mark.asyncio
    async def test_skill_description_from_recommendation_purpose(
        self,
        sample_assessment: AssessmentResult,
    ):
        """POST body 'description' should be set from the recommendation's 'purpose'."""
        client, _, created_skills = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        purpose_by_name = {r.name: r.purpose for r in sample_assessment.recommended_skills}
        for body in created_skills:
            expected_desc = purpose_by_name.get(body["name"])
            if expected_desc is not None:
                assert body["description"] == expected_desc

    @pytest.mark.asyncio
    async def test_file_write_failure_prevents_db_post_for_agents(
        self,
        sample_assessment: AssessmentResult,
    ):
        """If _write_generated_definition raises, no DB POST should be attempted."""
        post_calls: list[str] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            body = kwargs.get("json", {})
            post_calls.append(body.get("name", "unknown"))
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        def failing_write(path, content):
            raise OSError("Disk full")

        with (
            patch("daemon.adaptation.httpx.AsyncClient", return_value=client),
            patch("daemon.adaptation._write_generated_definition", failing_write),
        ):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # No agents should be created and no POST calls should have been made
        assert names == []
        assert post_calls == [], (
            f"DB POST was attempted for {post_calls} despite file write failure"
        )

    @pytest.mark.asyncio
    async def test_file_write_failure_prevents_db_post_for_skills(
        self,
        sample_assessment: AssessmentResult,
    ):
        """If _write_generated_definition raises for skills, no DB POST should be attempted."""
        post_calls: list[str] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            body = kwargs.get("json", {})
            post_calls.append(body.get("name", "unknown"))
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        def failing_write(path, content):
            raise OSError("Disk full")

        with (
            patch("daemon.adaptation.httpx.AsyncClient", return_value=client),
            patch("daemon.adaptation._write_generated_definition", failing_write),
        ):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert names == []
        assert post_calls == [], (
            f"DB POST was attempted for {post_calls} despite file write failure"
        )

    @pytest.mark.asyncio
    async def test_pipeline_run_creates_files_and_db_rows(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        """pipeline.run() should create both file artifacts and DB rows for all definitions."""
        db_agents: list[dict] = []
        db_skills: list[dict] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/definitions/agents" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": row["name"]} for row in db_agents]
            elif "/definitions/skills" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": row["name"]} for row in db_skills]
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            row = {"id": f"test-{body['name']}", **body}
            if "/definitions/agents" in url:
                db_agents.append(row)
                resp.status_code = 201
                resp.json.return_value = row
            elif "/definitions/skills" in url:
                db_skills.append(row)
                resp.status_code = 201
                resp.json.return_value = row
            else:
                resp.status_code = 404
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(
                memory_api=None,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Verify DB rows were created
        assert len(result["agents_proposed"]) >= 2
        assert len(result["skills_proposed"]) >= 2
        assert len(db_agents) == len(result["agents_proposed"])
        assert len(db_skills) == len(result["skills_proposed"])

        # Verify every proposed agent has a file AND correct DB metadata
        for row in db_agents:
            assert row["scope"] == "instance"
            assert row["status"] == "proposed"
            artifact = _redirect_generated_definition_dirs["agents_dir"] / f"{row['name']}.md"
            assert artifact.exists(), f"No file artifact for agent '{row['name']}'"

        # Verify every proposed skill has a file AND correct DB metadata
        for row in db_skills:
            assert row["scope"] == "instance"
            assert row["status"] == "proposed"
            artifact = (
                _redirect_generated_definition_dirs["skills_dir"] / row["name"] / "SKILL.md"
            )
            assert artifact.exists(), f"No file artifact for skill '{row['name']}'"

    @pytest.mark.asyncio
    async def test_duplicate_recommendations_deduped_within_single_run(
        self,
        _redirect_generated_definition_dirs,
    ):
        """If recommendations contain duplicate names, only one POST is made per name."""
        assessment = AssessmentResult.from_json({
            "recommended_agents": [
                {"name": "sec-agent", "purpose": "First", "domain_template": "software"},
                {"name": "sec-agent", "purpose": "Duplicate", "domain_template": "software"},
            ],
            "recommended_skills": [
                {"name": "sec-skill", "purpose": "First", "domain_template": "software"},
                {"name": "sec-skill", "purpose": "Duplicate", "domain_template": "software"},
            ],
        })

        agent_posts: list[dict] = []
        skill_posts: list[dict] = []
        created_agent_names: set[str] = set()
        created_skill_names: set[str] = set()

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/definitions/agents" in url:
                resp.json.return_value = [{"name": n} for n in created_agent_names]
            elif "/definitions/skills" in url:
                resp.json.return_value = [{"name": n} for n in created_skill_names]
            else:
                resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            if "/definitions/agents" in url:
                agent_posts.append(body)
                created_agent_names.add(body["name"])
            elif "/definitions/skills" in url:
                skill_posts.append(body)
                created_skill_names.add(body["name"])
            resp.status_code = 201
            resp.json.return_value = {"id": "test-id", **body}
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            agents = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )
            skills = await pipeline.generate_skills(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Only one POST per unique name, despite duplicate recommendations
        assert len(agents) == 1
        assert len(agent_posts) == 1
        assert agent_posts[0]["name"] == "sec-agent"

        assert len(skills) == 1
        assert len(skill_posts) == 1
        assert skill_posts[0]["name"] == "sec-skill"

    @pytest.mark.asyncio
    async def test_run_adaptation_convenience_persists_to_db(
        self,
        sample_raw_output: str,
        _redirect_generated_definition_dirs,
    ):
        """The run_adaptation() convenience function should trigger full DB persistence."""
        db_agents: list[dict] = []
        db_skills: list[dict] = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/definitions/agents" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": r["name"]} for r in db_agents]
            elif "/definitions/skills" in url:
                resp.status_code = 200
                resp.json.return_value = [{"name": r["name"]} for r in db_skills]
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            body = kwargs.get("json", {})
            row = {"id": f"id-{body['name']}", **body}
            if "/definitions/agents" in url:
                db_agents.append(row)
            elif "/definitions/skills" in url:
                db_skills.append(row)
            resp.status_code = 201
            resp.json.return_value = row
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            result = await run_adaptation(
                sample_raw_output,
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        assert result is not None
        assert result["requires_approval"] is True
        # DB rows were created
        assert len(db_agents) >= 2
        assert len(db_skills) >= 2
        # All have correct scope and status
        for row in db_agents + db_skills:
            assert row["scope"] == "instance"
            assert row["status"] == "proposed"

    @pytest.mark.asyncio
    async def test_partial_db_failure_only_cleans_failed_agents(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
    ):
        """If DB fails for one agent but succeeds for another, only the failed one is cleaned up."""
        call_count = 0

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            body = kwargs.get("json", {})
            if call_count == 1:
                # First agent succeeds
                resp.status_code = 201
                resp.json.return_value = {"id": "ok", **body}
            else:
                # Second agent fails
                resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            names = await pipeline.generate_agents(
                api_base="http://test/api",
                api_headers={"Authorization": "Bearer test"},
            )

        # Only the first agent should be in the result
        assert len(names) == 1
        agents_dir = _redirect_generated_definition_dirs["agents_dir"]
        # The successful agent's file should still exist
        assert (agents_dir / f"{names[0]}.md").exists()
        # The failed agent's file should have been cleaned up
        failed_name = [
            r.name for r in sample_assessment.recommended_agents if r.name != names[0]
        ][0]
        assert not (agents_dir / f"{failed_name}.md").exists()


# ============================================================================
# Atomicity — logging verification and exception paths
# ============================================================================


class TestAtomicityLogging:
    """Verify that DB persistence failures are logged, not silently swallowed.

    The existing TestAgentGeneration/TestSkillGeneration tests verify artifact
    cleanup on DB failure (status 500). This class covers the gaps:
    - Warning is actually logged when DB returns non-201
    - Debug is logged when httpx raises an exception
    - Artifacts are cleaned up on exception path (not just non-201)
    - Inconsistent-state warning logged when cleanup itself fails
    """

    @pytest.mark.asyncio
    async def test_agent_db_failure_logs_warning(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """Non-201 from DB POST logs a warning with the agent name and status code."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.WARNING, logger="daemon.adaptation"):
                names = await pipeline.generate_agents(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        assert names == []
        warnings = [r for r in caplog.records if "Failed to persist generated agent" in r.message]
        assert len(warnings) >= 1
        # Check that agent name and status code appear in at least one warning
        assert any("security" in r.message or "deployment" in r.message for r in warnings)

    @pytest.mark.asyncio
    async def test_skill_db_failure_logs_warning(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """Non-201 from DB POST logs a warning with the skill name and status code."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.WARNING, logger="daemon.adaptation"):
                names = await pipeline.generate_skills(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        assert names == []
        warnings = [r for r in caplog.records if "Failed to persist generated skill" in r.message]
        assert len(warnings) >= 1

    @pytest.mark.asyncio
    async def test_agent_db_exception_cleans_up_and_logs(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """If httpx raises an exception during POST, the artifact is cleaned up."""
        call_count = 0

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("Connection refused")

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.DEBUG, logger="daemon.adaptation"):
                names = await pipeline.generate_agents(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        # No agents should be created
        assert names == []
        # All recommendation POSTs should have been attempted
        assert call_count == len(sample_assessment.recommended_agents)
        # Artifacts should have been cleaned up
        for rec in sample_assessment.recommended_agents:
            artifact = _redirect_generated_definition_dirs["agents_dir"] / f"{rec.name}.md"
            assert not artifact.exists(), f"Artifact for {rec.name} should be cleaned up"
        # Debug log should mention the failure
        debug_msgs = [
            r for r in caplog.records
            if "Failed to create agent definition" in r.message
        ]
        assert len(debug_msgs) >= 1

    @pytest.mark.asyncio
    async def test_skill_db_exception_cleans_up_and_logs(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """If httpx raises an exception during skill POST, artifact + dir are cleaned up."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            raise httpx.ConnectError("Connection refused")

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.DEBUG, logger="daemon.adaptation"):
                names = await pipeline.generate_skills(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        assert names == []
        for rec in sample_assessment.recommended_skills:
            artifact = (
                _redirect_generated_definition_dirs["skills_dir"] / rec.name / "SKILL.md"
            )
            assert not artifact.exists(), f"Artifact for {rec.name} should be cleaned up"
        debug_msgs = [
            r for r in caplog.records
            if "Failed to create skill definition" in r.message
        ]
        assert len(debug_msgs) >= 1

    @pytest.mark.asyncio
    async def test_agent_cleanup_failure_logs_inconsistent_state(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """When DB fails AND cleanup fails, a warning about inconsistent state is logged."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        def exploding_cleanup(path):
            raise OSError("Permission denied")

        with (
            patch("daemon.adaptation.httpx.AsyncClient", return_value=client),
            patch("daemon.adaptation._cleanup_generated_definition", exploding_cleanup),
        ):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.WARNING, logger="daemon.adaptation"):
                names = await pipeline.generate_agents(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        assert names == []
        inconsistent_warnings = [
            r for r in caplog.records
            if "Inconsistent adaptation state" in r.message
        ]
        assert len(inconsistent_warnings) >= 1

    @pytest.mark.asyncio
    async def test_skill_cleanup_failure_logs_inconsistent_state(
        self,
        sample_assessment: AssessmentResult,
        _redirect_generated_definition_dirs,
        caplog,
    ):
        """When skill DB fails AND cleanup fails, inconsistent state warning is logged."""
        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = []
            return resp

        async def mock_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        client = AsyncMock()
        client.get = mock_get
        client.post = mock_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        def exploding_cleanup(path):
            raise OSError("Permission denied")

        with (
            patch("daemon.adaptation.httpx.AsyncClient", return_value=client),
            patch("daemon.adaptation._cleanup_generated_definition", exploding_cleanup),
        ):
            pipeline = AdaptationPipeline(sample_assessment)
            with caplog.at_level(logging.WARNING, logger="daemon.adaptation"):
                names = await pipeline.generate_skills(
                    api_base="http://test/api",
                    api_headers={"Authorization": "Bearer test"},
                )

        assert names == []
        inconsistent_warnings = [
            r for r in caplog.records
            if "Inconsistent adaptation state" in r.message
        ]
        assert len(inconsistent_warnings) >= 1

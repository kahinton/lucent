"""Tests for the Lucent adaptation pipeline.

Validates:
- Assessment output parsing (<assessment_result> JSON extraction)
- AssessmentResult data model construction
- Agent definition generation from Jinja2 templates
- Skill directory/file creation from templates
- Graceful handling of missing/malformed assessment output
- End-to-end pipeline execution (without memory API)
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from daemon.adaptation import (
    AGENTS_DIR,
    SKILLS_DIR,
    AdaptationPipeline,
    AgentRecommendation,
    AssessmentResult,
    SkillRecommendation,
    _build_agent_context,
    _build_skill_context,
    _select_agent_template,
    _select_skill_template,
    parse_assessment_output,
    run_adaptation,
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
    "collaborators": [
        {"name": "Kyle", "role": "Lead Developer", "preferences": "Concise, direct"}
    ],
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
            "<assessment_result>{\"domain\": {\"primary\": \"legal\"}}</assessment_result>\n"
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
        data = {
            "recommended_agents": [
                {"name": "test-agent"}
            ]
        }
        result = AssessmentResult.from_json(data)
        agent = result.recommended_agents[0]
        assert agent.name == "test-agent"
        assert agent.purpose == ""
        assert agent.domain_template == "general"
        assert agent.specialization == {}

    def test_skill_recommendation_defaults(self):
        data = {
            "recommended_skills": [
                {"name": "test-skill"}
            ]
        }
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
        rec = AgentRecommendation(name="code-review", purpose="Review code", domain_template="software")
        assert _select_agent_template(rec) == "agents/software_agent.md.j2"

    def test_support_agent_template(self):
        rec = AgentRecommendation(name="triage", purpose="Triage issues", domain_template="support")
        assert _select_agent_template(rec) == "agents/support_agent.md.j2"

    def test_research_agent_template(self):
        rec = AgentRecommendation(name="literature", purpose="Literature review", domain_template="research")
        assert _select_agent_template(rec) == "agents/research_agent.md.j2"

    def test_general_agent_template(self):
        rec = AgentRecommendation(name="generic", purpose="General tasks", domain_template="general")
        assert _select_agent_template(rec) == "agents/base_agent.md.j2"

    def test_unknown_domain_falls_back_to_base(self):
        rec = AgentRecommendation(name="custom", purpose="Custom", domain_template="unknown-domain")
        assert _select_agent_template(rec) == "agents/base_agent.md.j2"

    def test_software_code_review_skill_template(self):
        rec = SkillRecommendation(name="code-review", purpose="Review code", domain_template="software")
        assert _select_skill_template(rec) == "skills/software_code_review.md.j2"

    def test_software_dev_workflow_skill_template(self):
        rec = SkillRecommendation(name="dev-workflow", purpose="Dev workflow", domain_template="software")
        assert _select_skill_template(rec) == "skills/software_dev_workflow.md.j2"

    def test_general_skill_template(self):
        rec = SkillRecommendation(name="unknown-skill", purpose="Something", domain_template="general")
        assert _select_skill_template(rec) == "skills/base_skill.md.j2"

    def test_unknown_skill_falls_back_to_base(self):
        rec = SkillRecommendation(name="weird", purpose="Weird", domain_template="unknown")
        assert _select_skill_template(rec) == "skills/base_skill.md.j2"


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
        rec = SkillRecommendation(name="code-review", purpose="Review code", domain_template="software")
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

class TestAgentGeneration:
    """Tests for generating agent .md files from templates."""

    def test_generates_agent_files(self, sample_assessment: AssessmentResult, tmp_path: Path):
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path):
            pipeline = AdaptationPipeline(sample_assessment)
            paths = pipeline.generate_agents()

        assert len(paths) == 2
        for p in paths:
            assert p.exists()
            assert p.suffix == ".md"
            content = p.read_text()
            assert "# " in content  # Has a heading
            assert "daemon" in content  # Tags reference

    def test_skips_existing_agents(self, sample_assessment: AssessmentResult, tmp_path: Path):
        # Pre-create one agent
        existing = tmp_path / "security.agent.md"
        existing.write_text("# Existing agent\n")

        with patch("daemon.adaptation.AGENTS_DIR", tmp_path):
            pipeline = AdaptationPipeline(sample_assessment)
            paths = pipeline.generate_agents()

        # Only deployment should be created, security was skipped
        assert len(paths) == 1
        assert paths[0].name == "deployment.agent.md"
        # Existing file should be untouched
        assert existing.read_text() == "# Existing agent\n"

    def test_generates_nothing_with_no_recommendations(self, tmp_path: Path):
        assessment = AssessmentResult()
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            paths = pipeline.generate_agents()

        assert paths == []

    def test_software_template_includes_language_guidance(self, sample_assessment: AssessmentResult, tmp_path: Path):
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path):
            pipeline = AdaptationPipeline(sample_assessment)
            paths = pipeline.generate_agents()

        content = paths[0].read_text()
        assert "python" in content.lower()
        assert "ruff" in content.lower()


# ============================================================================
# Skill Generation
# ============================================================================

class TestSkillGeneration:
    """Tests for generating skill directories with SKILL.md files."""

    def test_generates_skill_directories(self, sample_assessment: AssessmentResult, tmp_path: Path):
        with patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(sample_assessment)
            paths = pipeline.generate_skills()

        assert len(paths) == 2
        for p in paths:
            assert p.exists()
            assert p.name == "SKILL.md"
            assert p.parent.is_dir()
            content = p.read_text()
            assert "---" in content  # Has frontmatter
            assert "name:" in content

    def test_skips_existing_skills(self, sample_assessment: AssessmentResult, tmp_path: Path):
        # Pre-create one skill
        existing_dir = tmp_path / "code-review"
        existing_dir.mkdir()
        (existing_dir / "SKILL.md").write_text("# Existing skill\n")

        with patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(sample_assessment)
            paths = pipeline.generate_skills()

        # Only dev-workflow should be created
        assert len(paths) == 1
        assert paths[0].parent.name == "dev-workflow"

    def test_generates_nothing_with_no_recommendations(self, tmp_path: Path):
        assessment = AssessmentResult()
        with patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            paths = pipeline.generate_skills()

        assert paths == []


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
    async def test_run_without_memory_api(self, sample_assessment: AssessmentResult, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "software"
        assert len(result["agents_created"]) == 2
        assert len(result["skills_created"]) == 2
        assert result["agents_skipped"] == []
        assert result["skills_skipped"] == []

    @pytest.mark.asyncio
    async def test_run_reports_skipped(self, sample_assessment: AssessmentResult, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Pre-create one agent and one skill
        (agents_dir / "security.agent.md").write_text("existing")
        code_review_dir = skills_dir / "code-review"
        code_review_dir.mkdir()
        (code_review_dir / "SKILL.md").write_text("existing")

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(sample_assessment)
            result = await pipeline.run(memory_api=None)

        assert len(result["agents_created"]) == 1
        assert "security" in result["agents_skipped"]
        assert len(result["skills_created"]) == 1
        assert "code-review" in result["skills_skipped"]


# ============================================================================
# run_adaptation convenience function
# ============================================================================

class TestRunAdaptation:
    """Tests for the run_adaptation convenience function."""

    @pytest.mark.asyncio
    async def test_success(self, sample_raw_output: str, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            result = await run_adaptation(sample_raw_output)

        assert result is not None
        assert result["domain"] == "software"
        assert len(result["agents_created"]) == 2

    @pytest.mark.asyncio
    async def test_returns_none_for_bad_input(self):
        result = await run_adaptation("no assessment here")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_malformed_json(self):
        result = await run_adaptation("<assessment_result>{bad json}</assessment_result>")
        assert result is None

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

    def test_legal_agent_template(self):
        rec = AgentRecommendation(
            name="legal-research", purpose="Legal research", domain_template="legal",
        )
        assert _select_agent_template(rec) == "agents/legal_agent.md.j2"

    def test_legal_case_analysis_skill_template(self):
        rec = SkillRecommendation(
            name="case-analysis", purpose="Case analysis", domain_template="legal",
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
        # Explicit recommendations + archetype fills
        assert len(result["agents_created"]) >= 2
        assert len(result["skills_created"]) >= 2
        assert "validation_warnings" in result

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

        # security agent and code-review skill were pre-created, so should be skipped
        assert "security" in result["agents_skipped"]
        assert "code-review" in result["skills_skipped"]
        # deployment agent should still be created (not pre-existing)
        created_paths = [Path(p).stem for p in result["agents_created"]]
        assert "deployment.agent" in created_paths


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
        # At least the 2 explicit + archetype additions
        assert len(result["agents_created"]) >= 2

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
                "A software project with compliance monitoring"
                " and regulatory checks"
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

    def test_archetypes_fill_gaps_for_software(self, tmp_path: Path):
        """When assessment has no explicit recommendations, archetypes fill in."""
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A Python web app",
            tech_stack={"languages": ["python"]},
        )
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path), \
             patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "code-review" in agent_names
        assert "security" in agent_names
        skill_names = [r.name for r in assessment.recommended_skills]
        assert "code-review" in skill_names

    def test_archetypes_fill_gaps_for_legal(self, tmp_path: Path):
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="A law firm",
        )
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path), \
             patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "legal-research" in agent_names
        assert "contract-review" in agent_names
        assert "compliance" in agent_names

    def test_archetypes_dont_duplicate_existing(self, tmp_path: Path):
        """Archetypes skip agents/skills that already exist."""
        assessment = AssessmentResult(
            domain_primary="software",
            existing_agents=["code-review", "security"],
            existing_skills=["code-review"],
        )
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path), \
             patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            pipeline.apply_archetypes()

        agent_names = [r.name for r in assessment.recommended_agents]
        assert "code-review" not in agent_names  # Already exists
        assert "security" not in agent_names  # Already exists
        assert "testing" in agent_names  # New from archetype

    def test_archetypes_dont_duplicate_recommendations(self, tmp_path: Path):
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
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path), \
             patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            pipeline.apply_archetypes()

        # code-review should appear exactly once
        agent_names = [r.name for r in assessment.recommended_agents]
        assert agent_names.count("code-review") == 1
        # But others from archetype should be added
        assert "security" in agent_names

    def test_cross_domain_archetypes(self, tmp_path: Path):
        """Secondary domains contribute archetypes too."""
        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A compliance monitoring tool with regulatory checks",
            tech_stack={"languages": ["python"], "tools": ["pytest"]},
            guardrails=["Ensure regulatory compliance"],
        )
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path), \
             patch("daemon.adaptation.SKILLS_DIR", tmp_path):
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

    def test_legal_agent_generates(self, tmp_path: Path):
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
        with patch("daemon.adaptation.AGENTS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            paths = pipeline.generate_agents()

        assert len(paths) == 1
        content = paths[0].read_text()
        assert "Legal Research" in content
        assert "legal environment" in content.lower()
        assert "privilege" in content.lower()
        assert "jurisdiction" in content.lower()
        assert "daemon" in content

    def test_legal_skill_generates(self, tmp_path: Path):
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
        with patch("daemon.adaptation.SKILLS_DIR", tmp_path):
            pipeline = AdaptationPipeline(assessment)
            paths = pipeline.generate_skills()

        assert len(paths) == 2
        names = [p.parent.name for p in paths]
        assert "case-analysis" in names
        assert "compliance-review" in names
        for p in paths:
            content = p.read_text()
            assert "---" in content  # Frontmatter
            assert "name:" in content


# ============================================================================
# Pipeline with Archetypes (End-to-End)
# ============================================================================

class TestPipelineWithArchetypes:
    """Tests for the full pipeline including archetype application."""

    @pytest.mark.asyncio
    async def test_legal_domain_end_to_end(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="Corporate law practice with contracts and compliance",
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "legal"
        # Archetypes should have filled in legal agents/skills
        assert len(result["agents_created"]) >= 3  # legal-research, contract-review, compliance
        assert len(result["skills_created"]) >= 2  # case-analysis, compliance-review
        assert "validation_warnings" in result

    @pytest.mark.asyncio
    async def test_support_domain_end_to_end(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="support",
            domain_description="Customer support team with ticketing",
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "support"
        assert len(result["agents_created"]) >= 3  # triage, incident-response, knowledge-base
        assert len(result["skills_created"]) >= 1  # triage

    @pytest.mark.asyncio
    async def test_pipeline_includes_validation_warnings(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="software",
            recommended_agents=[
                AgentRecommendation(name="test-agent", purpose="Test", domain_template="software"),
            ],
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        # validation_warnings key should exist (may be empty if templates pass)
        assert "validation_warnings" in result

    @pytest.mark.asyncio
    async def test_pipeline_signals_stored_in_metadata(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="software",
            domain_description="A Python project",
            tech_stack={"languages": ["python"]},
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)
            metadata = pipeline.build_agent_registry_metadata()

        assert "domain_signals" in metadata
        assert metadata["domain_signals"]["primary"] == "software"
        assert "scores" in metadata["domain_signals"]

"""Integration tests for the Lucent adaptation pipeline end-to-end.

Verifies the core value proposition: drop into a non-software environment
and generate valid, deployable agents and skills from a realistic assessment.

Each test runs the full pipeline (parse → signals → archetypes → generate → validate)
against synthetic but realistic assessment data for a non-software domain.
No LLM calls — all data is deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from daemon.adaptation import (
    AdaptationPipeline,
    AssessmentResult,
    parse_assessment_output,
    validate_agent,
    validate_skill,
)

# ============================================================================
# Realistic Assessment Data — Customer Support Domain
# ============================================================================

SUPPORT_ASSESSMENT_JSON = {
    "domain": {
        "primary": "support",
        "secondary": ["software"],
        "description": (
            "Enterprise customer support organization handling B2B SaaS incidents, "
            "escalation management, and knowledge base maintenance. Uses Zendesk for "
            "ticketing, PagerDuty for on-call, and Confluence for runbooks."
        ),
    },
    "tech_stack": {
        "languages": [],
        "frameworks": [],
        "infrastructure": ["aws"],
        "databases": [],
        "tools": ["zendesk", "pagerduty", "confluence", "slack", "jira"],
    },
    "collaborators": [
        {
            "name": "Maria Chen",
            "role": "Support Team Lead",
            "preferences": "Structured handoffs, clear escalation paths",
        },
        {
            "name": "James Park",
            "role": "Senior Support Engineer",
            "preferences": "Detailed runbooks, root cause analysis",
        },
        {
            "name": "Alex Rivera",
            "role": "Customer Success Manager",
            "preferences": "Customer-facing summaries, SLA tracking",
        },
    ],
    "existing_agents": [],
    "existing_skills": ["memory-init", "memory-search"],
    "recommended_agents": [
        {
            "name": "triage",
            "purpose": "Classify incoming support tickets by severity, product area, and route to the right team",
            "domain_template": "support",
            "specialization": {},
        },
        {
            "name": "incident-response",
            "purpose": "Coordinate incident resolution with structured updates and stakeholder communication",
            "domain_template": "support",
            "specialization": {},
        },
        {
            "name": "knowledge-base",
            "purpose": "Maintain and improve runbooks, FAQs, and resolution playbooks",
            "domain_template": "support",
            "specialization": {},
        },
    ],
    "recommended_skills": [
        {
            "name": "triage",
            "purpose": "Issue triage and classification with severity assessment and routing",
            "domain_template": "support",
        },
    ],
    "guardrails": [
        "Never share customer data between accounts",
        "Follow SLA commitments — P1 response within 15 minutes",
        "Escalate to engineering after 2 failed resolution attempts",
        "All customer-facing communication requires human review",
    ],
    "mcp_servers": {
        "connected": ["memory-server"],
        "recommended": [],
    },
}


# ============================================================================
# Realistic Assessment Data — Legal Research Domain
# ============================================================================

LEGAL_ASSESSMENT_JSON = {
    "domain": {
        "primary": "legal",
        "secondary": [],
        "description": (
            "Mid-size corporate law firm specializing in mergers & acquisitions, "
            "regulatory compliance, and contract negotiation. Handles due diligence, "
            "regulatory filings, and risk assessment across multiple jurisdictions."
        ),
    },
    "tech_stack": {
        "languages": [],
        "frameworks": [],
        "infrastructure": [],
        "databases": [],
        "tools": ["westlaw", "lexisnexis", "docusign", "netdocuments"],
    },
    "collaborators": [
        {
            "name": "Sarah Thompson",
            "role": "Senior Partner",
            "preferences": "Concise memos, risk-focused analysis",
        },
        {
            "name": "David Kim",
            "role": "Associate Attorney",
            "preferences": "Thorough research with full citation chains",
        },
        {
            "name": "Lisa Patel",
            "role": "Paralegal",
            "preferences": "Document organization, timeline tracking",
        },
    ],
    "existing_agents": [],
    "existing_skills": ["memory-init"],
    "recommended_agents": [
        {
            "name": "legal-research",
            "purpose": "Research case law, statutes, and regulatory requirements across jurisdictions",
            "domain_template": "legal",
            "specialization": {},
        },
        {
            "name": "contract-review",
            "purpose": "Analyze contracts for risks, obligations, indemnification clauses, and key terms",
            "domain_template": "legal",
            "specialization": {},
        },
        {
            "name": "compliance",
            "purpose": "Monitor regulatory changes and assess compliance posture across jurisdictions",
            "domain_template": "legal",
            "specialization": {},
        },
    ],
    "recommended_skills": [
        {
            "name": "case-analysis",
            "purpose": "Structured legal case analysis with jurisdiction-aware research",
            "domain_template": "legal",
        },
        {
            "name": "compliance-review",
            "purpose": "Regulatory compliance review with gap analysis and remediation planning",
            "domain_template": "legal",
        },
    ],
    "guardrails": [
        "Maintain attorney-client privilege at all times",
        "Never provide legal advice — provide research and analysis only",
        "Always note jurisdiction and date of research",
        "Flag items requiring partner review before client communication",
    ],
    "mcp_servers": {
        "connected": ["memory-server"],
        "recommended": [],
    },
}


# ============================================================================
# Helper
# ============================================================================

def _wrap_assessment(data: dict) -> str:
    """Wrap assessment JSON in the expected XML tags with surrounding prose."""
    return (
        "I've completed the environment assessment. Here are my findings:\n\n"
        f"<assessment_result>\n{json.dumps(data, indent=2)}\n</assessment_result>\n"
    )


# ============================================================================
# Integration Tests — Customer Support Domain
# ============================================================================

class TestSupportDomainIntegration:
    """End-to-end: realistic customer support assessment → generated capabilities."""

    @pytest.mark.asyncio
    async def test_full_pipeline_from_raw_output(self, tmp_path: Path):
        """Parse raw assessment text → run pipeline → verify all outputs."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        raw_output = _wrap_assessment(SUPPORT_ASSESSMENT_JSON)
        assessment = parse_assessment_output(raw_output)
        assert assessment is not None, "Failed to parse assessment output"

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        # Domain is correct
        assert result["domain"] == "support"

        # At least the 3 explicit agents, plus archetype additions from secondary domains
        assert len(result["agents_created"]) >= 3
        created_agent_files = {Path(p).name for p in result["agents_created"]}
        assert "triage.agent.md" in created_agent_files
        assert "incident-response.agent.md" in created_agent_files
        assert "knowledge-base.agent.md" in created_agent_files

        # At least the 1 explicit skill was created
        assert len(result["skills_created"]) >= 1
        created_skill_dirs = {Path(p).parent.name for p in result["skills_created"]}
        assert "triage" in created_skill_dirs

        # No agents were skipped (none pre-existed)
        assert result["agents_skipped"] == []

    @pytest.mark.asyncio
    async def test_generated_agents_are_well_formed(self, tmp_path: Path):
        """Every generated agent passes structural validation."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for agent_path_str in [str(p) for p in agents_dir.glob("*.agent.md")]:
            agent_path = Path(agent_path_str)
            content = agent_path.read_text()
            name = agent_path.stem.replace(".agent", "")

            vr = validate_agent(content, name)
            assert vr.valid, f"Agent {name} failed validation: {vr.errors}"
            # Agents should have a heading
            assert "# " in content, f"Agent {name} missing heading"
            # Agents should reference daemon tag
            assert "daemon" in content, f"Agent {name} missing daemon tag"

    @pytest.mark.asyncio
    async def test_generated_agents_are_domain_appropriate(self, tmp_path: Path):
        """Support agents reference support-specific tools and concepts."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        triage_content = (agents_dir / "triage.agent.md").read_text()
        triage_lower = triage_content.lower()
        # Triage agent should reference support-domain tools
        assert "search_memories" in triage_content, "Triage agent missing search_memories tool"
        # Should reference the domain description
        assert "customer" in triage_lower or "support" in triage_lower, (
            "Triage agent missing support domain context"
        )
        # Should include the custom guardrails
        assert "customer data" in triage_lower or "sla" in triage_lower, (
            "Triage agent missing domain guardrails"
        )

    @pytest.mark.asyncio
    async def test_generated_skills_have_valid_structure(self, tmp_path: Path):
        """Every generated skill has frontmatter and passes validation."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for skill_md in skills_dir.glob("*/SKILL.md"):
            content = skill_md.read_text()
            name = skill_md.parent.name

            vr = validate_skill(content, name)
            assert vr.valid, f"Skill {name} failed validation: {vr.errors}"
            # Must have YAML frontmatter
            assert content.startswith("---"), f"Skill {name} missing frontmatter"
            assert "name:" in content, f"Skill {name} frontmatter missing name"
            assert "description:" in content, f"Skill {name} frontmatter missing description"

    @pytest.mark.asyncio
    async def test_pipeline_summary_reflects_support_domain(self, tmp_path: Path):
        """The adaptation summary correctly describes the support domain."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)
            summary = pipeline.build_adaptation_summary()

        assert "support" in summary.lower()
        assert "triage" in summary.lower()
        assert "incident-response" in summary.lower() or "incident" in summary.lower()

    @pytest.mark.asyncio
    async def test_registry_metadata_is_complete(self, tmp_path: Path):
        """Registry metadata includes domain signals and all generated agents."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)
            metadata = pipeline.build_agent_registry_metadata()

        assert metadata["domain"] == "support"
        assert "domain_signals" in metadata
        assert metadata["domain_signals"]["primary"] == "support"
        # All generated agents should be in the registry
        for agent_name in metadata["generated_agents"]:
            assert agent_name in metadata["agent_registry"]
            assert metadata["agent_registry"][agent_name]["source"] == "generated"


# ============================================================================
# Integration Tests — Legal Research Domain
# ============================================================================

class TestLegalDomainIntegration:
    """End-to-end: realistic legal research assessment → generated capabilities."""

    @pytest.mark.asyncio
    async def test_full_pipeline_from_raw_output(self, tmp_path: Path):
        """Parse raw assessment text → run pipeline → verify all outputs."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        raw_output = _wrap_assessment(LEGAL_ASSESSMENT_JSON)
        assessment = parse_assessment_output(raw_output)
        assert assessment is not None, "Failed to parse assessment output"

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "legal"

        # At least the 3 explicit agents, plus archetype additions from secondary domains
        assert len(result["agents_created"]) >= 3
        created_agent_files = {Path(p).name for p in result["agents_created"]}
        assert "legal-research.agent.md" in created_agent_files
        assert "contract-review.agent.md" in created_agent_files
        assert "compliance.agent.md" in created_agent_files

        # At least the 2 explicit skills, plus any archetype additions
        assert len(result["skills_created"]) >= 2
        created_skill_dirs = {Path(p).parent.name for p in result["skills_created"]}
        assert "case-analysis" in created_skill_dirs
        assert "compliance-review" in created_skill_dirs

        assert result["agents_skipped"] == []
        assert result["skills_skipped"] == []

    @pytest.mark.asyncio
    async def test_legal_agents_contain_domain_specific_content(self, tmp_path: Path):
        """Legal agents reference jurisdiction, privilege, citations — not code/PRs."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        # Check legal-research agent
        research_content = (agents_dir / "legal-research.agent.md").read_text()
        research_lower = research_content.lower()
        assert "jurisdiction" in research_lower, "Legal agent missing jurisdiction reference"
        assert "privilege" in research_lower, "Legal agent missing privilege reference"
        assert "citation" in research_lower, "Legal agent missing citation reference"
        assert "legal environment" in research_lower, "Legal agent missing domain context"

        # Check contract-review agent
        contract_content = (agents_dir / "contract-review.agent.md").read_text()
        contract_lower = contract_content.lower()
        assert "jurisdiction" in contract_lower, "Contract agent missing jurisdiction"
        assert "daemon" in contract_content, "Contract agent missing daemon tag"

    @pytest.mark.asyncio
    async def test_legal_agents_include_custom_guardrails(self, tmp_path: Path):
        """Legal agents include both template guardrails and assessment-specific ones."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for agent_file in agents_dir.glob("*.agent.md"):
            content = agent_file.read_text()
            name = agent_file.stem
            # Template-level legal guardrails
            assert "privilege" in content.lower(), (
                f"Agent {name} missing privilege guardrail"
            )
            # Assessment-specific guardrails should be rendered
            assert "attorney-client privilege" in content.lower() or "partner review" in content.lower(), (
                f"Agent {name} missing custom guardrail from assessment"
            )

    @pytest.mark.asyncio
    async def test_legal_agents_reference_appropriate_tools(self, tmp_path: Path):
        """Legal agents use legal-domain tools, not software-domain ones."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        research_content = (agents_dir / "legal-research.agent.md").read_text()
        # Legal agents should have legal-domain tools
        assert "search_memories" in research_content, "Legal agent missing search_memories tool"
        assert "web_fetch" in research_content, "Legal agent missing web_fetch tool"

    @pytest.mark.asyncio
    async def test_legal_skills_have_domain_specific_process(self, tmp_path: Path):
        """Legal skills reference legal process steps, not software workflows."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        case_analysis = (skills_dir / "case-analysis" / "SKILL.md").read_text()
        case_lower = case_analysis.lower()
        # Case analysis skill should reference legal concepts
        assert "jurisdiction" in case_lower, "Case analysis skill missing jurisdiction"
        assert "case law" in case_lower or "statute" in case_lower, (
            "Case analysis skill missing legal research references"
        )
        assert "cite" in case_lower, "Case analysis skill missing citation guidance"

        compliance = (skills_dir / "compliance-review" / "SKILL.md").read_text()
        compliance_lower = compliance.lower()
        assert "regulation" in compliance_lower, "Compliance skill missing regulation reference"
        assert "compliance" in compliance_lower


# ============================================================================
# Integration Tests — Archetype-Only Generation (No Explicit Recommendations)
# ============================================================================

class TestArchetypeOnlyIntegration:
    """Pipeline should work with domain-only assessment, filling from archetypes."""

    @pytest.mark.asyncio
    async def test_support_domain_with_no_recommendations(self, tmp_path: Path):
        """A minimal support assessment still generates useful agents via archetypes."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="support",
            domain_description=(
                "Customer support helpdesk handling tickets, escalations, "
                "and incident management"
            ),
            tech_stack={"tools": ["zendesk", "pagerduty"]},
            guardrails=["Respect customer data privacy"],
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "support"
        # Archetypes should fill in standard support agents
        created_names = {Path(p).stem.replace(".agent", "") for p in result["agents_created"]}
        assert "triage" in created_names, "Archetype should have created triage agent"
        assert "incident-response" in created_names, "Archetype should have created incident-response agent"
        assert "knowledge-base" in created_names, "Archetype should have created knowledge-base agent"

        # Should have at least the triage skill
        assert len(result["skills_created"]) >= 1

        # All generated files should actually exist on disk
        for path_str in result["agents_created"] + result["skills_created"]:
            assert Path(path_str).exists(), f"Generated file not found: {path_str}"

    @pytest.mark.asyncio
    async def test_legal_domain_with_no_recommendations(self, tmp_path: Path):
        """A minimal legal assessment still generates useful agents via archetypes."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="Law firm handling contracts and compliance",
            guardrails=["Maintain attorney-client privilege"],
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "legal"
        created_names = {Path(p).stem.replace(".agent", "") for p in result["agents_created"]}
        assert "legal-research" in created_names
        assert "contract-review" in created_names
        assert "compliance" in created_names

        assert len(result["skills_created"]) >= 2

    @pytest.mark.asyncio
    async def test_research_domain_with_no_recommendations(self, tmp_path: Path):
        """A minimal research assessment generates research-domain agents."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult(
            domain_primary="research",
            domain_description="Academic research lab studying climate data and statistical models",
            tech_stack={"tools": ["jupyter", "r-studio"]},
        )

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None)

        assert result["domain"] == "research"
        created_names = {Path(p).stem.replace(".agent", "") for p in result["agents_created"]}
        assert "literature-review" in created_names
        assert "data-analysis" in created_names


# ============================================================================
# Integration Tests — Cross-Cutting Concerns
# ============================================================================

class TestCrossCuttingIntegration:
    """Validate properties that should hold across ALL generated domains."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("domain_json", [
        SUPPORT_ASSESSMENT_JSON,
        LEGAL_ASSESSMENT_JSON,
    ], ids=["support", "legal"])
    async def test_all_generated_agents_pass_validation(self, tmp_path: Path, domain_json: dict):
        """Every agent generated by the pipeline passes validate_agent()."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for agent_file in agents_dir.glob("*.agent.md"):
            content = agent_file.read_text()
            name = agent_file.stem.replace(".agent", "")
            vr = validate_agent(content, name)
            assert vr.valid, f"Agent {name} in {domain_json['domain']['primary']} failed: {vr.errors}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("domain_json", [
        SUPPORT_ASSESSMENT_JSON,
        LEGAL_ASSESSMENT_JSON,
    ], ids=["support", "legal"])
    async def test_all_generated_skills_pass_validation(self, tmp_path: Path, domain_json: dict):
        """Every skill generated by the pipeline passes validate_skill()."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for skill_md in skills_dir.glob("*/SKILL.md"):
            content = skill_md.read_text()
            name = skill_md.parent.name
            vr = validate_skill(content, name)
            assert vr.valid, f"Skill {name} in {domain_json['domain']['primary']} failed: {vr.errors}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("domain_json", [
        SUPPORT_ASSESSMENT_JSON,
        LEGAL_ASSESSMENT_JSON,
    ], ids=["support", "legal"])
    async def test_generated_files_are_non_empty(self, tmp_path: Path, domain_json: dict):
        """No generated file should be empty or trivially short."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None)

        for agent_file in agents_dir.glob("*.agent.md"):
            content = agent_file.read_text()
            assert len(content) > 200, (
                f"Agent {agent_file.name} suspiciously short ({len(content)} chars)"
            )

        for skill_md in skills_dir.glob("*/SKILL.md"):
            content = skill_md.read_text()
            assert len(content) > 100, (
                f"Skill {skill_md.parent.name} suspiciously short ({len(content)} chars)"
            )

    @pytest.mark.asyncio
    async def test_idempotent_reruns_skip_existing(self, tmp_path: Path):
        """Running the pipeline twice doesn't duplicate or overwrite files."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.AGENTS_DIR", agents_dir), \
             patch("daemon.adaptation.SKILLS_DIR", skills_dir):
            pipeline1 = AdaptationPipeline(assessment)
            result1 = await pipeline1.run(memory_api=None)

            # Capture content from first run
            first_run_content = {}
            for p in result1["agents_created"]:
                first_run_content[p] = Path(p).read_text()

            # Run again with fresh pipeline
            assessment2 = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)
            pipeline2 = AdaptationPipeline(assessment2)
            result2 = await pipeline2.run(memory_api=None)

        # Second run should create nothing (all exist)
        assert len(result2["agents_created"]) == 0
        assert len(result2["skills_created"]) == 0
        assert len(result2["agents_skipped"]) == len(result1["agents_created"])
        assert len(result2["skills_skipped"]) == len(result1["skills_created"])

        # Original files should be untouched
        for path_str, original_content in first_run_content.items():
            assert Path(path_str).read_text() == original_content

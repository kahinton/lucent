"""Integration tests for the Lucent adaptation pipeline end-to-end.

Verifies the core value proposition: drop into a non-software environment
and generate valid, deployable agent/skill definitions via the definitions API.

Each test runs the full pipeline (parse → signals → archetypes → generate → validate)
against synthetic but realistic assessment data for a non-software domain.
No LLM calls — all data is deterministic. Definitions are created as 'proposed'
status requiring human approval before use.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.adaptation import (
    AdaptationPipeline,
    AssessmentResult,
    parse_assessment_output,
    validate_agent,
    validate_skill,
)

# ── Mock helper ──────────────────────────────────────────────────────────


def _mock_httpx_client(existing_agents=None, existing_skills=None):
    """Create a mock httpx.AsyncClient that simulates the definitions API."""
    existing_agents = existing_agents or []
    existing_skills = existing_skills or []
    created_agents: list[dict] = []
    created_skills: list[dict] = []

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
        elif "/definitions/skills" in url:
            created_skills.append(body)
            resp.status_code = 201
        else:
            resp.status_code = 404
        return resp

    client = AsyncMock()
    client.get = mock_get
    client.post = mock_post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, created_agents, created_skills


def _find_agent(created_agents: list[dict], name: str) -> dict | None:
    return next((a for a in created_agents if a["name"] == name), None)


def _find_skill(created_skills: list[dict], name: str) -> dict | None:
    return next((s for s in created_skills if s["name"] == name), None)


API_KWARGS = dict(api_base="http://test/api", api_headers={"Authorization": "Bearer test"})

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
    ],
    "existing_agents": [],
    "existing_skills": ["memory-init", "memory-search"],
    "recommended_agents": [
        {
            "name": "triage",
            "purpose": "Classify incoming support tickets by severity, "
            "product area, and route to the right team",
            "domain_template": "support",
            "specialization": {},
        },
        {
            "name": "incident-response",
            "purpose": "Coordinate incident resolution with structured "
            "updates and stakeholder communication",
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
    "mcp_servers": {"connected": ["memory-server"], "recommended": []},
}


LEGAL_ASSESSMENT_JSON = {
    "domain": {
        "primary": "legal",
        "secondary": [],
        "description": (
            "Mid-size corporate law firm specializing in mergers & acquisitions, "
            "regulatory compliance, and contract negotiation."
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
    ],
    "existing_agents": [],
    "existing_skills": ["memory-init", "memory-search"],
    "recommended_agents": [
        {
            "name": "legal-research",
            "purpose": "Research legal precedents, statutes, and regulations across jurisdictions",
            "domain_template": "legal",
            "specialization": {},
        },
        {
            "name": "contract-review",
            "purpose": "Analyze contracts for risks, obligations, and compliance issues",
            "domain_template": "legal",
            "specialization": {},
        },
        {
            "name": "compliance",
            "purpose": "Monitor regulatory requirements and ensure organizational compliance",
            "domain_template": "legal",
            "specialization": {},
        },
    ],
    "recommended_skills": [
        {
            "name": "case-analysis",
            "purpose": "Structured legal case analysis with precedent mapping",
            "domain_template": "legal",
        },
        {
            "name": "compliance-review",
            "purpose": "Regulatory compliance review and gap analysis",
            "domain_template": "legal",
        },
    ],
    "guardrails": [
        "Maintain attorney-client privilege at all times",
        "All legal opinions require partner review before delivery",
        "Never provide legal advice — only analysis and research",
        "Cite specific statutes, cases, or regulations for every conclusion",
    ],
    "mcp_servers": {"connected": ["memory-server"], "recommended": []},
}


def _wrap_assessment(data: dict) -> str:
    return f"Assessment complete.\n<assessment_result>\n{json.dumps(data)}\n</assessment_result>"


# ============================================================================
# Support Domain Integration
# ============================================================================


class TestSupportDomainIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline_from_raw_output(self):
        client, created_agents, created_skills = _mock_httpx_client()
        raw_output = _wrap_assessment(SUPPORT_ASSESSMENT_JSON)
        assessment = parse_assessment_output(raw_output)
        assert assessment is not None

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None, **API_KWARGS)

        assert result["domain"] == "support"
        assert result["requires_approval"] is True
        assert len(result["agents_proposed"]) >= 3
        names = set(result["agents_proposed"])
        assert "triage" in names
        assert "incident-response" in names
        assert "knowledge-base" in names
        assert len(result["skills_proposed"]) >= 1

    @pytest.mark.asyncio
    async def test_generated_agents_are_well_formed(self):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for agent_body in created_agents:
            content = agent_body["content"]
            name = agent_body["name"]
            vr = validate_agent(content, name)
            assert vr.valid, f"Agent {name} failed validation: {vr.errors}"
            assert "# " in content, f"Agent {name} missing heading"
            assert "daemon" in content, f"Agent {name} missing daemon tag"

    @pytest.mark.asyncio
    async def test_generated_agents_are_domain_appropriate(self):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        triage = _find_agent(created_agents, "triage")
        assert triage, "Triage agent not created"
        content = triage["content"].lower()
        assert "search_memories" in triage["content"]
        assert "customer" in content or "support" in content

    @pytest.mark.asyncio
    async def test_generated_skills_have_valid_structure(self):
        client, _, created_skills = _mock_httpx_client()
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for skill_body in created_skills:
            content = skill_body["content"]
            name = skill_body["name"]
            vr = validate_skill(content, name)
            assert vr.valid, f"Skill {name} failed validation: {vr.errors}"
            assert content.startswith("---"), f"Skill {name} missing frontmatter"
            assert "name:" in content

    @pytest.mark.asyncio
    async def test_pipeline_summary_reflects_support_domain(self):
        client, _, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)
            summary = pipeline.build_adaptation_summary()

        assert "support" in summary.lower()
        assert "triage" in summary.lower()

    @pytest.mark.asyncio
    async def test_registry_metadata_is_complete(self):
        client, _, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)
            metadata = pipeline.build_agent_registry_metadata()

        assert metadata["domain"] == "support"
        assert "domain_signals" in metadata
        assert metadata["domain_signals"]["primary"] == "support"
        for agent_name in metadata["generated_agents"]:
            assert agent_name in metadata["agent_registry"]
            assert metadata["agent_registry"][agent_name]["source"] == "generated"


# ============================================================================
# Legal Domain Integration
# ============================================================================


class TestLegalDomainIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline_from_raw_output(self):
        client, created_agents, created_skills = _mock_httpx_client()
        raw_output = _wrap_assessment(LEGAL_ASSESSMENT_JSON)
        assessment = parse_assessment_output(raw_output)
        assert assessment is not None

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None, **API_KWARGS)

        assert result["domain"] == "legal"
        assert len(result["agents_proposed"]) >= 3
        names = set(result["agents_proposed"])
        assert "legal-research" in names
        assert "contract-review" in names
        assert "compliance" in names
        assert len(result["skills_proposed"]) >= 2

    @pytest.mark.asyncio
    async def test_legal_agents_contain_domain_specific_content(self):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        research = _find_agent(created_agents, "legal-research")
        assert research
        content = research["content"].lower()
        assert "jurisdiction" in content
        assert "privilege" in content
        assert "citation" in content
        assert "legal environment" in content

        contract = _find_agent(created_agents, "contract-review")
        assert contract
        assert "jurisdiction" in contract["content"].lower()
        assert "daemon" in contract["content"]

    @pytest.mark.asyncio
    async def test_legal_agents_include_custom_guardrails(self):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for agent_body in created_agents:
            content = agent_body["content"].lower()
            name = agent_body["name"]
            assert "privilege" in content, f"Agent {name} missing privilege guardrail"

    @pytest.mark.asyncio
    async def test_legal_agents_reference_appropriate_tools(self):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        research = _find_agent(created_agents, "legal-research")
        assert research
        assert "search_memories" in research["content"]
        assert "web_fetch" in research["content"]

    @pytest.mark.asyncio
    async def test_legal_skills_have_domain_specific_process(self):
        client, _, created_skills = _mock_httpx_client()
        assessment = AssessmentResult.from_json(LEGAL_ASSESSMENT_JSON)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        case_skill = _find_skill(created_skills, "case-analysis")
        assert case_skill
        content = case_skill["content"].lower()
        assert "jurisdiction" in content
        assert "case law" in content or "statute" in content
        assert "cite" in content

        compliance_skill = _find_skill(created_skills, "compliance-review")
        assert compliance_skill
        assert "regulation" in compliance_skill["content"].lower()


# ============================================================================
# Archetype-Only Integration (No Explicit Recommendations)
# ============================================================================


class TestArchetypeOnlyIntegration:
    @pytest.mark.asyncio
    async def test_support_domain_with_no_recommendations(self):
        client, _, _ = _mock_httpx_client()
        assessment = AssessmentResult(
            domain_primary="support",
            domain_description="Customer support helpdesk handling tickets and escalations",
            tech_stack={"tools": ["zendesk", "pagerduty"]},
            guardrails=["Respect customer data privacy"],
        )

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None, **API_KWARGS)

        assert result["domain"] == "support"
        names = set(result["agents_proposed"])
        assert "triage" in names
        assert "incident-response" in names
        assert "knowledge-base" in names

    @pytest.mark.asyncio
    async def test_legal_domain_with_no_recommendations(self):
        client, _, _ = _mock_httpx_client()
        assessment = AssessmentResult(
            domain_primary="legal",
            domain_description="Law firm handling contracts and compliance",
            guardrails=["Maintain attorney-client privilege"],
        )

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None, **API_KWARGS)

        assert result["domain"] == "legal"
        names = set(result["agents_proposed"])
        assert "legal-research" in names
        assert "contract-review" in names
        assert "compliance" in names

    @pytest.mark.asyncio
    async def test_research_domain_with_no_recommendations(self):
        client, _, _ = _mock_httpx_client()
        assessment = AssessmentResult(
            domain_primary="research",
            domain_description="Academic research lab studying climate data",
            tech_stack={"tools": ["jupyter", "r-studio"]},
        )

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            result = await pipeline.run(memory_api=None, **API_KWARGS)

        assert result["domain"] == "research"
        names = set(result["agents_proposed"])
        assert "literature-review" in names
        assert "data-analysis" in names


# ============================================================================
# Cross-Cutting Concerns
# ============================================================================


class TestCrossCuttingIntegration:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "domain_json",
        [SUPPORT_ASSESSMENT_JSON, LEGAL_ASSESSMENT_JSON],
        ids=["support", "legal"],
    )
    async def test_all_generated_agents_pass_validation(self, domain_json: dict):
        client, created_agents, _ = _mock_httpx_client()
        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for agent_body in created_agents:
            vr = validate_agent(agent_body["content"], agent_body["name"])
            assert vr.valid, f"Agent {agent_body['name']} failed: {vr.errors}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "domain_json",
        [SUPPORT_ASSESSMENT_JSON, LEGAL_ASSESSMENT_JSON],
        ids=["support", "legal"],
    )
    async def test_all_generated_skills_pass_validation(self, domain_json: dict):
        client, _, created_skills = _mock_httpx_client()
        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for skill_body in created_skills:
            vr = validate_skill(skill_body["content"], skill_body["name"])
            assert vr.valid, f"Skill {skill_body['name']} failed: {vr.errors}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "domain_json",
        [SUPPORT_ASSESSMENT_JSON, LEGAL_ASSESSMENT_JSON],
        ids=["support", "legal"],
    )
    async def test_generated_definitions_are_non_empty(self, domain_json: dict):
        client, created_agents, created_skills = _mock_httpx_client()
        assessment = AssessmentResult.from_json(domain_json)

        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client):
            pipeline = AdaptationPipeline(assessment)
            await pipeline.run(memory_api=None, **API_KWARGS)

        for body in created_agents:
            assert len(body["content"]) > 200, f"Agent {body['name']} suspiciously short"
        for body in created_skills:
            assert len(body["content"]) > 100, f"Skill {body['name']} suspiciously short"

    @pytest.mark.asyncio
    async def test_idempotent_reruns_skip_existing(self):
        """Running pipeline twice with same agents already existing skips all."""
        assessment = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)

        # First run — creates agents
        client1, agents1, skills1 = _mock_httpx_client()
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client1):
            pipeline1 = AdaptationPipeline(assessment)
            await pipeline1.run(memory_api=None, **API_KWARGS)

        # Second run — all agents/skills already exist
        agent_names = [a["name"] for a in agents1]
        skill_names = [s["name"] for s in skills1]
        client2, _, _ = _mock_httpx_client(
            existing_agents=agent_names,
            existing_skills=skill_names,
        )
        assessment2 = AssessmentResult.from_json(SUPPORT_ASSESSMENT_JSON)
        with patch("daemon.adaptation.httpx.AsyncClient", return_value=client2):
            pipeline2 = AdaptationPipeline(assessment2)
            result2 = await pipeline2.run(memory_api=None, **API_KWARGS)

        assert len(result2["agents_proposed"]) == 0
        assert len(result2["skills_proposed"]) == 0
        assert len(result2["agents_skipped"]) > 0

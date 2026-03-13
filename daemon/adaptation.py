"""Lucent Environment Adaptation Pipeline.

Takes structured assessment output from the assessment agent and generates
domain-specific agents, skills, and memories. This is the core capability
that lets Lucent walk into any environment and learn to do the job.

Pipeline:
  1. Parse assessment JSON from agent output
  2. Extract domain signals and classify the environment
  3. Map domain classification to agent/skill archetypes
  4. Select appropriate templates based on domain
  5. Generate agent .md files with domain-specific content
  6. Generate skill directories with SKILL.md files
  7. Validate generated artifacts against quality checklist
  8. Store adaptation results as memories via MemoryAPI
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader

# Paths
DAEMON_DIR = Path(__file__).parent
TEMPLATES_DIR = DAEMON_DIR / "templates"


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class AgentRecommendation:
    """A recommended agent to generate."""

    name: str
    purpose: str
    domain_template: str = "general"
    specialization: dict = field(default_factory=dict)


@dataclass
class SkillRecommendation:
    """A recommended skill to generate."""

    name: str
    purpose: str
    domain_template: str = "general"


@dataclass
class AssessmentResult:
    """Parsed output from the assessment agent."""

    domain_primary: str = "software"
    domain_secondary: list[str] = field(default_factory=list)
    domain_description: str = ""
    tech_stack: dict = field(default_factory=dict)
    collaborators: list[dict] = field(default_factory=list)
    existing_agents: list[str] = field(default_factory=list)
    existing_skills: list[str] = field(default_factory=list)
    recommended_agents: list[AgentRecommendation] = field(default_factory=list)
    recommended_skills: list[SkillRecommendation] = field(default_factory=list)
    guardrails: list[str] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> AssessmentResult:
        """Parse from the JSON output of the assessment agent."""
        domain = data.get("domain", {})
        agents = [
            AgentRecommendation(
                name=a["name"],
                purpose=a.get("purpose", ""),
                domain_template=a.get("domain_template", "general"),
                specialization=a.get("specialization", {}),
            )
            for a in data.get("recommended_agents", [])
        ]
        skills = [
            SkillRecommendation(
                name=s["name"],
                purpose=s.get("purpose", ""),
                domain_template=s.get("domain_template", "general"),
            )
            for s in data.get("recommended_skills", [])
        ]
        return cls(
            domain_primary=domain.get("primary", "software"),
            domain_secondary=domain.get("secondary", []),
            domain_description=domain.get("description", ""),
            tech_stack=data.get("tech_stack", {}),
            collaborators=data.get("collaborators", []),
            existing_agents=data.get("existing_agents", []),
            existing_skills=data.get("existing_skills", []),
            recommended_agents=agents,
            recommended_skills=skills,
            guardrails=data.get("guardrails", []),
            mcp_servers=data.get("mcp_servers", {}),
        )


def parse_assessment_output(raw_output: str) -> AssessmentResult | None:
    """Extract and parse the structured JSON from assessment agent output.

    The assessment agent wraps its JSON in <assessment_result> tags.
    """
    match = re.search(
        r"<assessment_result>\s*(.*?)\s*</assessment_result>",
        raw_output,
        re.DOTALL,
    )
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
        return AssessmentResult.from_json(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ============================================================================
# Template Engine
# ============================================================================

# Maps domain_template values to agent template files
AGENT_TEMPLATE_MAP = {
    "software": "agents/software_agent.md.j2",
    "support": "agents/support_agent.md.j2",
    "research": "agents/research_agent.md.j2",
    "legal": "agents/legal_agent.md.j2",
    "general": "agents/base_agent.md.j2",
}

# Maps domain_template values to skill template files
SKILL_TEMPLATE_MAP = {
    "software-code-review": "skills/software_code_review.md.j2",
    "software-dev-workflow": "skills/software_dev_workflow.md.j2",
    "support-triage": "skills/support_triage.md.j2",
    "research-methodology": "skills/research_methodology.md.j2",
    "legal-case-analysis": "skills/legal_case_analysis.md.j2",
    "legal-compliance-review": "skills/legal_compliance.md.j2",
    "general": "skills/base_skill.md.j2",
}

# Default tool preferences by domain
DEFAULT_TOOLS = {
    "software": [
        {"name": "grep/glob", "usage": "Code search and file discovery"},
        {"name": "view/edit", "usage": "Reading and modifying source files"},
        {"name": "bash", "usage": "Running tests, linters, build commands"},
    ],
    "legal": [
        {"name": "search_memories", "usage": "Finding previous research, case law, and precedents"},
        {"name": "web_fetch", "usage": "Accessing legal databases and regulatory sources"},
        {"name": "view/edit", "usage": "Reading and drafting legal documents"},
        {"name": "grep/glob", "usage": "Searching document repositories"},
    ],
    "support": [
        {"name": "search_memories", "usage": "Finding past resolutions and customer history"},
        {"name": "web_fetch", "usage": "Checking external documentation and status pages"},
        {"name": "create_memory", "usage": "Documenting resolutions for future reference"},
    ],
    "research": [
        {"name": "web_fetch", "usage": "Gathering external sources and documentation"},
        {"name": "search_memories", "usage": "Finding previous research and analysis"},
        {"name": "grep/glob", "usage": "Searching local documents and data"},
    ],
    "general": [
        {"name": "search_memories", "usage": "Finding relevant context and history"},
        {"name": "view/edit", "usage": "Reading and modifying files"},
        {"name": "bash", "usage": "Running commands as needed"},
    ],
}

# Role descriptions by agent specialization
ROLE_DESCRIPTIONS = {
    "code-review": "You review code changes for correctness, security, performance, and style.",
    "testing": "You write and maintain tests, ensuring code quality and coverage.",
    "security": "You identify security vulnerabilities and recommend fixes.",
    "documentation": "You create and maintain documentation for code, APIs, and processes.",
    "deployment": "You manage deployment processes, CI/CD pipelines, and infrastructure.",
    "triage": "You classify and route incoming issues to the right team or resolution.",
    "knowledge-base": "You maintain and improve the knowledge base for common issues.",
    "literature-review": "You survey existing research and synthesize findings.",
    "data-analysis": "You analyze data sets and produce structured insights.",
    "legal-research": "You research legal precedents, regulations, and compliance requirements.",
    "case-analysis": "You analyze specific cases and produce structured legal analysis.",
    "contract-review": "You review contracts for risks, obligations, and key terms.",
    "compliance": "You monitor and ensure regulatory compliance across the organization.",
}


# ============================================================================
# Domain Archetypes
# ============================================================================

# Each domain maps to a set of recommended agents and skills that are known
# to work well together. When an assessment doesn't include explicit
# recommendations, these archetypes fill in the gaps.


@dataclass
class AgentArchetype:
    """A prototypical agent for a domain."""

    name: str
    purpose: str
    domain_template: str
    default_specialization: dict = field(default_factory=dict)


@dataclass
class SkillArchetype:
    """A prototypical skill for a domain."""

    name: str
    purpose: str
    domain_template: str


DOMAIN_ARCHETYPES: dict[str, dict] = {
    "software": {
        "agents": [
            AgentArchetype(
                name="code-review",
                purpose="Review code changes for correctness, security, and performance",
                domain_template="software",
            ),
            AgentArchetype(
                name="testing",
                purpose="Write and maintain tests, ensuring code quality and coverage",
                domain_template="software",
            ),
            AgentArchetype(
                name="security",
                purpose="Identify security vulnerabilities and recommend fixes",
                domain_template="software",
            ),
            AgentArchetype(
                name="documentation",
                purpose="Create and maintain documentation for code, APIs, and processes",
                domain_template="software",
            ),
            AgentArchetype(
                name="deployment",
                purpose="Manage CI/CD pipelines and infrastructure",
                domain_template="software",
            ),
        ],
        "skills": [
            SkillArchetype(
                name="code-review",
                purpose="Structured code review process",
                domain_template="software",
            ),
            SkillArchetype(
                name="dev-workflow",
                purpose="Standard development workflow",
                domain_template="software",
            ),
        ],
    },
    "legal": {
        "agents": [
            AgentArchetype(
                name="legal-research",
                purpose="Research legal precedents, regulations, and compliance requirements",
                domain_template="legal",
            ),
            AgentArchetype(
                name="contract-review",
                purpose="Analyze contracts for risks, obligations, and key terms",
                domain_template="legal",
            ),
            AgentArchetype(
                name="compliance",
                purpose="Monitor and ensure regulatory compliance",
                domain_template="legal",
            ),
        ],
        "skills": [
            SkillArchetype(
                name="case-analysis",
                purpose="Structured legal case analysis",
                domain_template="legal",
            ),
            SkillArchetype(
                name="compliance-review",
                purpose="Regulatory compliance review process",
                domain_template="legal",
            ),
        ],
    },
    "support": {
        "agents": [
            AgentArchetype(
                name="triage",
                purpose="Classify and route incoming issues to the right resolution",
                domain_template="support",
            ),
            AgentArchetype(
                name="incident-response",
                purpose="Handle and resolve production incidents",
                domain_template="support",
            ),
            AgentArchetype(
                name="knowledge-base",
                purpose="Maintain and improve the knowledge base for common issues",
                domain_template="support",
            ),
        ],
        "skills": [
            SkillArchetype(
                name="triage",
                purpose="Issue triage and classification process",
                domain_template="support",
            ),
        ],
    },
    "research": {
        "agents": [
            AgentArchetype(
                name="literature-review",
                purpose="Survey existing research and synthesize findings",
                domain_template="research",
            ),
            AgentArchetype(
                name="data-analysis",
                purpose="Analyze datasets and produce structured insights",
                domain_template="research",
            ),
        ],
        "skills": [
            SkillArchetype(
                name="methodology",
                purpose="Research methodology and rigor guidelines",
                domain_template="research",
            ),
        ],
    },
}

# Domain signal keywords used by DomainSignalParser
DOMAIN_SIGNALS: dict[str, list[str]] = {
    "software": [
        "src",
        "tests",
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "tsconfig",
        ".eslintrc",
        "jest",
        "pytest",
        "docker",
        "ci",
        "cd",
        "github/workflows",
        "python",
        "typescript",
        "javascript",
        "go",
        "rust",
        "java",
        "kotlin",
        "swift",
    ],
    "legal": [
        "contracts",
        "cases",
        "briefs",
        "legal",
        "compliance",
        "regulatory",
        "statute",
        "regulation",
        "court",
        "litigation",
        "counsel",
        "attorney",
        "jurisdiction",
        "precedent",
        "clause",
        "indemnif",
        "liability",
    ],
    "support": [
        "runbooks",
        "playbooks",
        "tickets",
        "incidents",
        "sla",
        "monitoring",
        "triage",
        "escalation",
        "on-call",
        "pagerduty",
        "jira",
        "zendesk",
        "customer",
        "helpdesk",
        "support",
        "resolution",
    ],
    "research": [
        "papers",
        "notebooks",
        "data",
        "analysis",
        "research",
        "hypothesis",
        "experiment",
        "methodology",
        "citations",
        "journal",
        "publication",
        "dataset",
        "statistical",
        "survey",
        "study",
    ],
}


# ============================================================================
# Domain Signal Parser
# ============================================================================


@dataclass
class DomainSignals:
    """Structured signals extracted from an assessment."""

    domain_scores: dict[str, float] = field(default_factory=dict)
    primary_domain: str = "software"
    secondary_domains: list[str] = field(default_factory=list)
    tech_indicators: list[str] = field(default_factory=list)
    role_indicators: list[str] = field(default_factory=list)
    workflow_indicators: list[str] = field(default_factory=list)
    tool_indicators: list[str] = field(default_factory=list)


class DomainSignalParser:
    """Extracts structured domain signals from assessment data and classifies the environment."""

    def parse(self, assessment: AssessmentResult) -> DomainSignals:
        """Parse an AssessmentResult into structured domain signals."""
        signals = DomainSignals()

        # Extract indicators from assessment fields
        signals.tech_indicators = self._extract_tech_indicators(assessment)
        signals.role_indicators = self._extract_role_indicators(assessment)
        signals.tool_indicators = self._extract_tool_indicators(assessment)
        signals.workflow_indicators = self._extract_workflow_indicators(assessment)

        # Score each domain based on signal matches
        all_text = self._build_signal_text(assessment, signals)
        signals.domain_scores = self._score_domains(all_text)

        # Use assessment's explicit domain if provided, otherwise use highest score
        if assessment.domain_primary and assessment.domain_primary != "software":
            signals.primary_domain = assessment.domain_primary
        elif signals.domain_scores:
            best = max(signals.domain_scores, key=signals.domain_scores.get)
            if signals.domain_scores[best] > 0:
                signals.primary_domain = best
            else:
                signals.primary_domain = assessment.domain_primary or "software"
        else:
            signals.primary_domain = assessment.domain_primary or "software"

        # Secondary domains: any with score > 0 that aren't primary
        signals.secondary_domains = sorted(
            [d for d, s in signals.domain_scores.items() if s > 0 and d != signals.primary_domain],
            key=lambda d: signals.domain_scores[d],
            reverse=True,
        )

        return signals

    def _extract_tech_indicators(self, assessment: AssessmentResult) -> list[str]:
        indicators = []
        ts = assessment.tech_stack
        for key in ("languages", "frameworks", "infrastructure", "databases"):
            if key in ts and isinstance(ts[key], list):
                indicators.extend(ts[key])
        return indicators

    def _extract_role_indicators(self, assessment: AssessmentResult) -> list[str]:
        return [c.get("role", "") for c in assessment.collaborators if c.get("role")]

    def _extract_tool_indicators(self, assessment: AssessmentResult) -> list[str]:
        indicators = []
        ts = assessment.tech_stack
        if "tools" in ts and isinstance(ts["tools"], list):
            indicators.extend(ts["tools"])
        if assessment.mcp_servers:
            for key in ("connected", "recommended"):
                if key in assessment.mcp_servers and isinstance(assessment.mcp_servers[key], list):
                    indicators.extend(assessment.mcp_servers[key])
        return indicators

    def _extract_workflow_indicators(self, assessment: AssessmentResult) -> list[str]:
        indicators = []
        if assessment.guardrails:
            indicators.extend(assessment.guardrails)
        return indicators

    def _build_signal_text(self, assessment: AssessmentResult, signals: DomainSignals) -> str:
        """Combine all available text for keyword scoring."""
        parts = [
            assessment.domain_description,
            " ".join(signals.tech_indicators),
            " ".join(signals.role_indicators),
            " ".join(signals.tool_indicators),
            " ".join(signals.workflow_indicators),
        ]
        return " ".join(parts).lower()

    def _score_domains(self, text: str) -> dict[str, float]:
        """Score each domain by counting keyword matches in the combined signal text."""
        scores: dict[str, float] = {}
        for domain, keywords in DOMAIN_SIGNALS.items():
            score = sum(1.0 for kw in keywords if kw.lower() in text)
            scores[domain] = score
        return scores


# ============================================================================
# Validation
# ============================================================================


@dataclass
class ValidationResult:
    """Result of validating a generated agent or skill."""

    valid: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_agent(content: str, name: str) -> ValidationResult:
    """Validate a generated agent definition against a quality checklist."""
    warnings = []
    errors = []

    if not content.strip():
        errors.append("Agent content is empty")
        return ValidationResult(valid=False, warnings=warnings, errors=errors)

    # Must have a heading
    if "# " not in content:
        errors.append("Missing heading — agent has no title")

    # Must have a clear purpose / role section
    if "role" not in content.lower() and "purpose" not in content.lower():
        warnings.append("No explicit role or purpose section found")

    # Must have domain context
    if "domain" not in content.lower() and "context" not in content.lower():
        warnings.append("No domain context section found")

    # Must have tools
    if "tool" not in content.lower():
        warnings.append("No tools section found")

    # Must have guardrails
    if "guardrail" not in content.lower() and "constraint" not in content.lower():
        warnings.append("No guardrails section found")

    # Must reference memory tags
    if "daemon" not in content.lower():
        warnings.append("No reference to 'daemon' tag for memory output")

    # Must have feedback/review protocol
    if "feedback" not in content.lower() and "review" not in content.lower():
        warnings.append("No feedback/review protocol found")

    return ValidationResult(valid=len(errors) == 0, warnings=warnings, errors=errors)


def validate_skill(content: str, name: str) -> ValidationResult:
    """Validate a generated skill definition against a quality checklist."""
    warnings = []
    errors = []

    if not content.strip():
        errors.append("Skill content is empty")
        return ValidationResult(valid=False, warnings=warnings, errors=errors)

    # Must have YAML frontmatter
    if not content.startswith("---"):
        errors.append("Missing YAML frontmatter")
    else:
        if "name:" not in content:
            errors.append("Frontmatter missing 'name' field")
        if "description:" not in content:
            errors.append("Frontmatter missing 'description' field")

    # Must have triggers / when to use
    if "when to use" not in content.lower() and "trigger" not in content.lower():
        warnings.append("No 'When to Use' or triggers section found")

    # Must have process steps
    if "step" not in content.lower() and "process" not in content.lower():
        warnings.append("No process steps found")

    # Must have best practices
    if "best practice" not in content.lower() and "practice" not in content.lower():
        warnings.append("No best practices section found")

    # Should have pitfall warnings
    if "pitfall" not in content.lower() and "avoid" not in content.lower():
        warnings.append("No pitfall warnings found")

    return ValidationResult(valid=len(errors) == 0, warnings=warnings, errors=errors)


def _get_jinja_env() -> Environment:
    """Create a Jinja2 environment with the templates directory."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _select_agent_template(rec: AgentRecommendation) -> str:
    """Select the best template file for an agent recommendation."""
    return AGENT_TEMPLATE_MAP.get(rec.domain_template, "agents/base_agent.md.j2")


def _select_skill_template(rec: SkillRecommendation) -> str:
    """Select the best template file for a skill recommendation."""
    key = f"{rec.domain_template}-{rec.name}"
    if key in SKILL_TEMPLATE_MAP:
        return SKILL_TEMPLATE_MAP[key]
    # Try domain-specific patterns
    for pattern_key, template in SKILL_TEMPLATE_MAP.items():
        if rec.domain_template in pattern_key:
            return template
    return "skills/base_skill.md.j2"


def _build_agent_context(rec: AgentRecommendation, assessment: AssessmentResult) -> dict:
    """Build the template context for rendering an agent definition."""
    # Determine language and framework from specialization or tech stack
    language = rec.specialization.get("language", "")
    if not language and assessment.tech_stack.get("languages"):
        language = assessment.tech_stack["languages"][0]
    framework = rec.specialization.get("framework", "")
    if not framework and assessment.tech_stack.get("frameworks"):
        framework = assessment.tech_stack["frameworks"][0]

    # Get tools for this domain
    tools = DEFAULT_TOOLS.get(rec.domain_template, DEFAULT_TOOLS["general"])

    # Determine linter config
    linter_config = "project linter"
    if language == "python":
        linter_config = "ruff (pyproject.toml)"
    elif language in ("typescript", "javascript"):
        linter_config = "eslint/biome"

    # Build role description
    role_key = rec.name.replace("_", "-")
    role_description = ROLE_DESCRIPTIONS.get(role_key, rec.purpose)

    # Determine primary tag
    primary_tag = rec.name.replace("_", "-")

    return {
        "agent_name": rec.name.replace("-", " ").replace("_", " ").title(),
        "purpose": rec.purpose,
        "domain_description": assessment.domain_description,
        "role_description": role_description,
        "language": language,
        "framework": framework,
        "linter_config": linter_config,
        "preferred_tools": tools,
        "guardrails": assessment.guardrails
        or [
            "Follow established patterns and conventions",
            "Ask for clarification rather than guessing",
        ],
        "primary_tag": primary_tag,
    }


def _build_skill_context(rec: SkillRecommendation, assessment: AssessmentResult) -> dict:
    """Build the template context for rendering a skill definition."""
    language = ""
    framework = ""
    if assessment.tech_stack.get("languages"):
        language = assessment.tech_stack["languages"][0]
    if assessment.tech_stack.get("frameworks"):
        framework = assessment.tech_stack["frameworks"][0]

    return {
        "skill_name": rec.name,
        "title": rec.name.replace("-", " ").replace("_", " ").title(),
        "description": rec.purpose,
        "overview": rec.purpose,
        "language": language,
        "framework": framework,
        "triggers": [
            f"When working on {rec.name.replace('-', ' ')} tasks",
            "When the daemon dispatches a related task",
        ],
        "steps": [
            {
                "name": "Understand",
                "description": "Read the task and gather context.",
                "substeps": [],
            },
            {
                "name": "Execute",
                "description": "Perform the work following best practices.",
                "substeps": [],
            },
            {
                "name": "Document",
                "description": "Save results and lessons learned.",
                "substeps": [],
            },
        ],
        "best_practices": [
            "Search memories for previous approaches before starting",
            "Follow established patterns in this environment",
            "Document what you learn for future reference",
        ],
        "pitfalls": [
            {
                "name": "Skipping context",
                "description": "Always check memories and existing code before making changes",
            },
        ],
        "tips": [
            "Use validated patterns from previous work when available",
        ],
    }


# ============================================================================
# Pipeline
# ============================================================================


class AdaptationPipeline:
    """Generates domain-specific agents and skills from assessment results."""

    def __init__(self, assessment: AssessmentResult):
        self.assessment = assessment
        self.jinja_env = _get_jinja_env()
        self.generated_agents: list[str] = []
        self.generated_skills: list[str] = []
        self.validation_results: dict[str, ValidationResult] = {}
        self.signals: DomainSignals | None = None

    def apply_archetypes(self) -> None:
        """Fill in recommended agents/skills from domain archetypes when not explicitly provided.

        If the assessment already includes explicit recommendations, those are kept.
        Archetype recommendations are added only for agents/skills not already recommended
        or existing.
        """
        parser = DomainSignalParser()
        self.signals = parser.parse(self.assessment)

        # Collect all domains to draw archetypes from (primary + secondary)
        domains = [self.signals.primary_domain] + self.signals.secondary_domains

        existing_agent_names = set(
            self.assessment.existing_agents + [r.name for r in self.assessment.recommended_agents]
        )
        existing_skill_names = set(
            self.assessment.existing_skills + [r.name for r in self.assessment.recommended_skills]
        )

        for domain in domains:
            archetypes = DOMAIN_ARCHETYPES.get(domain)
            if not archetypes:
                continue

            for arch in archetypes.get("agents", []):
                if arch.name not in existing_agent_names:
                    self.assessment.recommended_agents.append(
                        AgentRecommendation(
                            name=arch.name,
                            purpose=arch.purpose,
                            domain_template=arch.domain_template,
                            specialization=dict(arch.default_specialization),
                        )
                    )
                    existing_agent_names.add(arch.name)

            for arch in archetypes.get("skills", []):
                if arch.name not in existing_skill_names:
                    self.assessment.recommended_skills.append(
                        SkillRecommendation(
                            name=arch.name,
                            purpose=arch.purpose,
                            domain_template=arch.domain_template,
                        )
                    )
                    existing_skill_names.add(arch.name)

    async def generate_agents(self, api_base: str = "", api_headers: dict | None = None) -> list[str]:
        """Generate agent definitions via the definitions API (proposed status).

        Definitions are created in 'proposed' status and require human approval
        before the daemon can use them. Returns names of proposed agents.
        """
        created = []
        if not api_base or not api_headers:
            return created

        # Check which agents already exist as definitions
        existing_names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{api_base}/definitions/agents",
                    headers=api_headers,
                )
                if resp.status_code == 200:
                    existing_names = {a["name"] for a in resp.json()}
        except Exception:
            pass

        for rec in self.assessment.recommended_agents:
            if rec.name in existing_names:
                continue

            template_file = _select_agent_template(rec)
            try:
                template = self.jinja_env.get_template(template_file)
            except Exception:
                template = self.jinja_env.get_template("agents/base_agent.md.j2")

            context = _build_agent_context(rec, self.assessment)
            content = template.render(**context)

            result = validate_agent(content, rec.name)
            self.validation_results[f"agent:{rec.name}"] = result

            # Create in DB as 'proposed' — requires human approval
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{api_base}/definitions/agents",
                        json={
                            "name": rec.name,
                            "description": rec.purpose,
                            "content": content,
                        },
                        headers=api_headers,
                    )
                    if resp.status_code == 201:
                        created.append(rec.name)
                        self.generated_agents.append(rec.name)
            except Exception:
                pass

        return created

    async def generate_skills(self, api_base: str = "", api_headers: dict | None = None) -> list[str]:
        """Generate skill definitions via the definitions API (proposed status).

        Definitions are created in 'proposed' status and require human approval.
        Returns names of proposed skills.
        """
        created = []
        if not api_base or not api_headers:
            return created

        # Check which skills already exist as definitions
        existing_names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{api_base}/definitions/skills",
                    headers=api_headers,
                )
                if resp.status_code == 200:
                    existing_names = {s["name"] for s in resp.json()}
        except Exception:
            pass

        for rec in self.assessment.recommended_skills:
            if rec.name in existing_names:
                continue

            template_file = _select_skill_template(rec)
            try:
                template = self.jinja_env.get_template(template_file)
            except Exception:
                template = self.jinja_env.get_template("skills/base_skill.md.j2")

            context = _build_skill_context(rec, self.assessment)
            content = template.render(**context)

            result = validate_skill(content, rec.name)
            self.validation_results[f"skill:{rec.name}"] = result

            # Create in DB as 'proposed' — requires human approval
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{api_base}/definitions/skills",
                        json={
                            "name": rec.name,
                            "description": rec.purpose,
                            "content": content,
                        },
                        headers=api_headers,
                    )
                    if resp.status_code == 201:
                        created.append(rec.name)
                        self.generated_skills.append(rec.name)
            except Exception:
                pass

        return created

    def build_adaptation_summary(self) -> str:
        """Build a summary of what was generated for memory storage."""
        lines = [
            f"## Environment Adaptation — {self.assessment.domain_primary}",
            "",
            f"**Domain**: {self.assessment.domain_primary}",
            f"**Description**: {self.assessment.domain_description}",
            "",
        ]
        if self.assessment.tech_stack:
            lines.append("**Tech Stack**:")
            for key, values in self.assessment.tech_stack.items():
                if values:
                    val_str = ", ".join(values) if isinstance(values, list) else values
                    lines.append(f"  - {key}: {val_str}")
            lines.append("")

        if self.generated_agents:
            lines.append("**Generated Agents**:")
            for name in self.generated_agents:
                rec = next((r for r in self.assessment.recommended_agents if r.name == name), None)
                purpose = rec.purpose if rec else ""
                lines.append(f"  - `{name}`: {purpose}")
            lines.append("")

        if self.generated_skills:
            lines.append("**Generated Skills**:")
            for name in self.generated_skills:
                rec = next((r for r in self.assessment.recommended_skills if r.name == name), None)
                purpose = rec.purpose if rec else ""
                lines.append(f"  - `{name}`: {purpose}")
            lines.append("")

        if self.assessment.guardrails:
            lines.append("**Domain Guardrails**:")
            for g in self.assessment.guardrails:
                lines.append(f"  - {g}")
            lines.append("")

        # Validation warnings
        warnings = {k: v for k, v in self.validation_results.items() if v.warnings}
        if warnings:
            lines.append("**Validation Warnings**:")
            for key, result in warnings.items():
                for w in result.warnings:
                    lines.append(f"  - `{key}`: {w}")
            lines.append("")

        # Agent registry — list all available agents
        all_agents = sorted(set(self.assessment.existing_agents + self.generated_agents))
        if all_agents:
            lines.append("**Agent Registry** (all available agents):")
            for name in all_agents:
                rec = next((r for r in self.assessment.recommended_agents if r.name == name), None)
                if rec:
                    lines.append(f"  - `{name}` [NEW]: {rec.purpose}")
                else:
                    lines.append(f"  - `{name}` [existing]")
            lines.append("")

        return "\n".join(lines)

    def build_agent_registry_metadata(self) -> dict:
        """Build metadata for the agent registry memory."""
        registry = {}
        for name in sorted(set(self.assessment.existing_agents + self.generated_agents)):
            rec = next((r for r in self.assessment.recommended_agents if r.name == name), None)
            vr = self.validation_results.get(f"agent:{name}")
            entry = {
                "source": "generated" if name in self.generated_agents else "existing",
                "domain_template": rec.domain_template if rec else "unknown",
                "purpose": rec.purpose if rec else "pre-existing agent",
            }
            if vr and vr.warnings:
                entry["validation_warnings"] = vr.warnings
            registry[name] = entry
        result = {
            "domain": self.assessment.domain_primary,
            "agent_registry": registry,
            "generated_agents": self.generated_agents,
            "generated_skills": self.generated_skills,
        }
        if self.signals:
            result["domain_signals"] = {
                "primary": self.signals.primary_domain,
                "secondary": self.signals.secondary_domains,
                "scores": self.signals.domain_scores,
            }
        return result

    async def run(self, memory_api=None, api_base: str = "", api_headers: dict | None = None) -> dict:
        """Execute the full adaptation pipeline.

        Generates agent and skill definitions in the database with 'proposed'
        status. A human must approve them via the definitions UI before the
        daemon can use them as sub-agent roles.

        Args:
            memory_api: Optional MemoryAPI class for storing memories.
            api_base: Base URL for the Lucent API (e.g. http://localhost:8766/api).
            api_headers: Auth headers for API calls.

        Returns a summary dict with what was proposed.
        """
        # Apply archetype recommendations based on domain signals
        self.apply_archetypes()

        # Propose definitions via API (human approval required)
        agents_proposed = await self.generate_agents(api_base, api_headers)
        skills_proposed = await self.generate_skills(api_base, api_headers)

        # Collect validation info
        validation_warnings = {
            k: v.warnings for k, v in self.validation_results.items() if v.warnings
        }

        summary = {
            "domain": self.assessment.domain_primary,
            "agents_proposed": agents_proposed,
            "skills_proposed": skills_proposed,
            "agents_skipped": [
                r.name
                for r in self.assessment.recommended_agents
                if r.name not in self.generated_agents
            ],
            "skills_skipped": [
                r.name
                for r in self.assessment.recommended_skills
                if r.name not in self.generated_skills
            ],
            "validation_warnings": validation_warnings,
            "requires_approval": True,
        }

        # Store memories if API is available
        if memory_api is not None:
            # Store adaptation summary
            await memory_api.create(
                type="technical",
                content=self.build_adaptation_summary(),
                tags=["daemon", "environment", "adaptation", "agent-registry"],
                importance=8,
                metadata=self.build_agent_registry_metadata(),
            )

        return summary


async def run_adaptation(
    raw_assessment_output: str,
    memory_api=None,
    api_base: str = "",
    api_headers: dict | None = None,
) -> dict | None:
    """Convenience function: parse assessment output and run the pipeline.

    Args:
        raw_assessment_output: The full text output from the assessment agent.
        memory_api: Optional MemoryAPI class for storing memories.
        api_base: Base URL for the Lucent API.
        api_headers: Auth headers for API calls.

    Returns a summary dict, or None if parsing failed.
    """
    assessment = parse_assessment_output(raw_assessment_output)
    if assessment is None:
        return None

    pipeline = AdaptationPipeline(assessment)
    return await pipeline.run(memory_api=memory_api, api_base=api_base, api_headers=api_headers)

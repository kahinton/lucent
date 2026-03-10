"""Lucent Environment Adaptation Pipeline.

Takes structured assessment output from the assessment agent and generates
domain-specific agents, skills, and memories. This is the core capability
that lets Lucent walk into any environment and learn to do the job.

Pipeline:
  1. Parse assessment JSON from agent output
  2. Select appropriate templates based on domain
  3. Generate agent .md files with domain-specific content
  4. Generate skill directories with SKILL.md files
  5. Store adaptation results as memories via MemoryAPI
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Paths
DAEMON_DIR = Path(__file__).parent
TEMPLATES_DIR = DAEMON_DIR / "templates"
AGENTS_DIR = DAEMON_DIR / "agents"
SKILLS_DIR = DAEMON_DIR.parent / ".github" / "skills"


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
    "general": "agents/base_agent.md.j2",
    "legal": "agents/base_agent.md.j2",
}

# Maps domain_template values to skill template files
SKILL_TEMPLATE_MAP = {
    "software-code-review": "skills/software_code_review.md.j2",
    "software-dev-workflow": "skills/software_dev_workflow.md.j2",
    "support-triage": "skills/support_triage.md.j2",
    "research-methodology": "skills/research_methodology.md.j2",
    "general": "skills/base_skill.md.j2",
}

# Default tool preferences by domain
DEFAULT_TOOLS = {
    "software": [
        {"name": "grep/glob", "usage": "Code search and file discovery"},
        {"name": "view/edit", "usage": "Reading and modifying source files"},
        {"name": "bash", "usage": "Running tests, linters, build commands"},
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
}


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
        "guardrails": assessment.guardrails or [
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
                "description": "Always check memories and existing "
                "code before making changes",
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

    def generate_agents(self) -> list[Path]:
        """Generate agent .md files from templates. Returns paths of created files."""
        created = []
        for rec in self.assessment.recommended_agents:
            # Skip if agent already exists
            agent_path = AGENTS_DIR / f"{rec.name}.agent.md"
            if agent_path.exists():
                continue

            template_file = _select_agent_template(rec)
            try:
                template = self.jinja_env.get_template(template_file)
            except Exception:
                # Fall back to base template
                template = self.jinja_env.get_template("agents/base_agent.md.j2")

            context = _build_agent_context(rec, self.assessment)
            content = template.render(**context)

            agent_path.write_text(content)
            created.append(agent_path)
            self.generated_agents.append(rec.name)

        return created

    def generate_skills(self) -> list[Path]:
        """Generate skill directories with SKILL.md files. Returns paths of created files."""
        created = []
        for rec in self.assessment.recommended_skills:
            # Skip if skill already exists
            skill_dir = SKILLS_DIR / rec.name
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                continue

            template_file = _select_skill_template(rec)
            try:
                template = self.jinja_env.get_template(template_file)
            except Exception:
                # Fall back to base template
                template = self.jinja_env.get_template("skills/base_skill.md.j2")

            context = _build_skill_context(rec, self.assessment)
            content = template.render(**context)

            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content)
            created.append(skill_file)
            self.generated_skills.append(rec.name)

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
                    val_str = (
                        ", ".join(values) if isinstance(values, list)
                        else values
                    )
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
            registry[name] = {
                "source": "generated" if name in self.generated_agents else "existing",
                "domain_template": rec.domain_template if rec else "unknown",
                "purpose": rec.purpose if rec else "pre-existing agent",
            }
        return {
            "domain": self.assessment.domain_primary,
            "agent_registry": registry,
            "generated_agents": self.generated_agents,
            "generated_skills": self.generated_skills,
        }

    async def run(self, memory_api=None) -> dict:
        """Execute the full adaptation pipeline.

        Args:
            memory_api: Optional MemoryAPI class for storing memories.
                        If None, only generates files (useful for testing).

        Returns a summary dict with what was created.
        """
        # Generate files
        agent_paths = self.generate_agents()
        skill_paths = self.generate_skills()

        summary = {
            "domain": self.assessment.domain_primary,
            "agents_created": [str(p) for p in agent_paths],
            "skills_created": [str(p) for p in skill_paths],
            "agents_skipped": [
                r.name for r in self.assessment.recommended_agents
                if r.name not in self.generated_agents
            ],
            "skills_skipped": [
                r.name for r in self.assessment.recommended_skills
                if r.name not in self.generated_skills
            ],
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


async def run_adaptation(raw_assessment_output: str, memory_api=None) -> dict | None:
    """Convenience function: parse assessment output and run the pipeline.

    Args:
        raw_assessment_output: The full text output from the assessment agent.
        memory_api: Optional MemoryAPI class for storing memories.

    Returns a summary dict, or None if parsing failed.
    """
    assessment = parse_assessment_output(raw_assessment_output)
    if assessment is None:
        return None

    pipeline = AdaptationPipeline(assessment)
    return await pipeline.run(memory_api=memory_api)

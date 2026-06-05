"""Definition import pipeline — fetch, parse, scan, and prepare definitions for import.

Supports importing agent definitions (AGENT.md), skill definitions (SKILL.md),
and MCP server configs from URLs, GitHub repos, or raw content.
All imports go through security scanning before being created in proposed status.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urljoin

import httpx
import yaml

from lucent.security import scan_content_for_injection
from lucent.url_validation import validate_url

logger = logging.getLogger(__name__)


class ImportSourceType(str, Enum):
    URL = "url"
    GITHUB = "github"
    RAW = "raw"


class DefinitionType(str, Enum):
    AGENT = "agent"
    SKILL = "skill"


class SecuritySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class SecurityFinding:
    severity: SecuritySeverity
    category: str
    detail: str


@dataclass
class ImportResult:
    """Result of the import pipeline."""
    success: bool
    definition_type: DefinitionType | None = None
    name: str | None = None
    description: str | None = None
    content: str | None = None
    skill_names: list[str] = field(default_factory=list)  # For agents
    source_url: str | None = None
    content_hash: str | None = None
    security_findings: list[SecurityFinding] = field(default_factory=list)
    error: str | None = None

    @property
    def has_critical_findings(self) -> bool:
        return any(f.severity == SecuritySeverity.CRITICAL for f in self.security_findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == SecuritySeverity.WARNING for f in self.security_findings)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "definition_type": self.definition_type.value if self.definition_type else None,
            "name": self.name,
            "description": self.description,
            "content": (
                self.content[:500] + "..."
                if self.content and len(self.content) > 500
                else self.content
            ),
            "content_length": len(self.content) if self.content else 0,
            "skill_names": self.skill_names,
            "source_url": self.source_url,
            "content_hash": self.content_hash,
            "security_findings": [
                {"severity": f.severity.value, "category": f.category, "detail": f.detail}
                for f in self.security_findings
            ],
            "has_critical_findings": self.has_critical_findings,
            "has_warnings": self.has_warnings,
            "error": self.error,
        }


# Maximum content size (500KB — generous for markdown definitions)
MAX_CONTENT_SIZE = 512 * 1024

# Patterns that indicate dangerous capabilities in agent/skill definitions
_DANGEROUS_TOOL_PATTERNS = [
    (r"run_command|execute_command|shell|bash|subprocess", "shell_execution",
     "References shell/command execution — review carefully"),
    (r"file_system|write_file|delete_file|rm\s+-rf", "filesystem_write",
     "References filesystem write/delete operations"),
    (r"send_email|send_message|post_to|webhook", "external_communication",
     "References external communication channels"),
    (r"api_key|secret|password|credential|token", "credential_access",
     "References credentials or secrets"),
    (r"network|http_request|fetch_url|curl|wget", "network_access",
     "References network/HTTP operations"),
    (r"database|sql|query|drop\s+table", "database_access",
     "References database operations"),
]

_INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?previous\s+instructions", "instruction_override",
     "Contains instruction override pattern"),
    (r"you\s+are\s+now\s+a|your\s+new\s+role\s+is", "role_hijacking",
     "Contains role reassignment pattern"),
    (r"system\s*:\s*you\s+are", "system_prompt_injection",
     "Contains system prompt injection pattern"),
    (r"do\s+not\s+follow\s+(any|the)\s+(previous|above)", "instruction_negation",
     "Contains instruction negation pattern"),
    (r"<\s*system\s*>|<\s*/\s*system\s*>", "xml_injection",
     "Contains XML system tag injection"),
    (r"forget\s+(everything|all|what)\s+(you|i)\s+(told|said|know)", "memory_wipe",
     "Contains memory/instruction wipe pattern"),
]


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, body_without_frontmatter).
    """
    if not content.startswith("---"):
        return {}, content

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}, content

    fm_text = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]

    try:
        fm = yaml.safe_load(fm_text)
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


def _detect_definition_type(
    frontmatter: dict, content: str, source_hint: str | None = None
) -> DefinitionType:
    """Detect whether content is an agent or skill definition."""
    # Check frontmatter for skill_names (agent indicator)
    if "skill_names" in frontmatter:
        return DefinitionType.AGENT

    # Check source path hints
    if source_hint:
        lower = source_hint.lower()
        if "agent" in lower:
            return DefinitionType.AGENT
        if "skill" in lower:
            return DefinitionType.SKILL

    # Default to skill (simpler, safer assumption)
    return DefinitionType.SKILL


def _run_security_scan(content: str, name: str) -> list[SecurityFinding]:
    """Run comprehensive security analysis on definition content."""
    findings: list[SecurityFinding] = []
    content_lower = content.lower()

    # 1. Run existing injection scanner
    injection_hits = scan_content_for_injection(content)
    for hit in injection_hits:
        findings.append(SecurityFinding(
            severity=SecuritySeverity.CRITICAL,
            category="prompt_injection",
            detail=f"Injection pattern detected: {hit}",
        ))

    # 2. Check for prompt injection patterns specific to definitions
    for pattern, category, detail in _INJECTION_PATTERNS:
        if re.search(pattern, content_lower):
            findings.append(SecurityFinding(
                severity=SecuritySeverity.CRITICAL,
                category=category,
                detail=detail,
            ))

    # 3. Check for dangerous capability references (warning, not blocking)
    for pattern, category, detail in _DANGEROUS_TOOL_PATTERNS:
        if re.search(pattern, content_lower):
            findings.append(SecurityFinding(
                severity=SecuritySeverity.WARNING,
                category=category,
                detail=detail,
            ))

    # 4. Content size check
    if len(content) > MAX_CONTENT_SIZE:
        findings.append(SecurityFinding(
            severity=SecuritySeverity.WARNING,
            category="content_size",
            detail=f"Content is {len(content)} bytes (limit: {MAX_CONTENT_SIZE})",
        ))

    # 5. Check for encoded/obfuscated content
    if re.search(r"(?:base64|atob|btoa|eval)\s*\(", content_lower):
        findings.append(SecurityFinding(
            severity=SecuritySeverity.CRITICAL,
            category="obfuscation",
            detail="Contains encoded/obfuscated content patterns",
        ))

    # 6. Check for data URI or embedded scripts
    if re.search(r"data:.*base64|javascript:", content_lower):
        findings.append(SecurityFinding(
            severity=SecuritySeverity.CRITICAL,
            category="embedded_code",
            detail="Contains embedded data URI or script",
        ))

    return findings


async def fetch_from_url(url: str) -> tuple[str | None, str | None]:
    """Fetch content from a URL with SSRF protection.

    Returns (content, error).
    """
    # Validate URL (SSRF protection)
    try:
        validate_url(url)
    except Exception as e:
        return None, f"URL validation failed: {e}"

    try:
        current_url = url
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for _redirect_count in range(4):
                resp = await client.get(
                    current_url,
                    headers={"Accept": "text/plain, text/markdown, */*"},
                )
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                location = resp.headers.get("location")
                if not location:
                    return None, f"HTTP {resp.status_code}: redirect without Location"
                current_url = urljoin(str(resp.url), location)
                try:
                    validate_url(current_url)
                except Exception as e:
                    return None, f"Redirect URL validation failed: {e}"
            else:
                return None, "Too many redirects"

            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {resp.reason_phrase}"

            # Check content type
            ct = resp.headers.get("content-type", "")
            if "html" in ct and "markdown" not in ct:
                # Might be a GitHub web page — try raw URL conversion
                return None, "URL returned HTML. For GitHub, use the raw file URL."

            content = resp.text
            if len(content) > MAX_CONTENT_SIZE:
                return None, f"Content too large: {len(content)} bytes (max {MAX_CONTENT_SIZE})"

            return content, None
    except httpx.TimeoutException:
        return None, "Request timed out"
    except Exception as e:
        return None, f"Fetch failed: {e}"


async def fetch_from_github(path: str) -> tuple[str | None, str | None, str | None]:
    """Fetch a file from GitHub.

    Path formats:
    - owner/repo/path/to/file.md (uses default branch)
    - owner/repo/blob/branch/path/to/file.md (specific branch)

    Returns (content, raw_url, error).
    """
    # Parse the path
    parts = path.strip("/").split("/")
    if len(parts) < 3:
        return None, None, "Invalid GitHub path. Use: owner/repo/path/to/file.md"

    owner, repo = parts[0], parts[1]

    # Handle blob/branch pattern
    if len(parts) > 3 and parts[2] == "blob":
        branch = parts[3]
        file_path = "/".join(parts[4:])
    else:
        branch = "main"
        file_path = "/".join(parts[2:])

    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"

    content, error = await fetch_from_url(raw_url)
    if error:
        # Try 'master' branch as fallback
        if branch == "main":
            raw_url_master = f"https://raw.githubusercontent.com/{owner}/{repo}/master/{file_path}"
            content, error2 = await fetch_from_url(raw_url_master)
            if not error2:
                return content, raw_url_master, None
        return None, raw_url, error

    return content, raw_url, None


async def import_definition(
    *,
    source_type: ImportSourceType,
    source: str,
    definition_type_hint: str | None = None,
) -> ImportResult:
    """Main import pipeline: fetch -> parse -> scan -> return result.

    Args:
        source_type: 'url', 'github', or 'raw'
        source: URL, GitHub path, or raw markdown content
        definition_type_hint: Optional hint ('agent' or 'skill')

    Returns:
        ImportResult with parsed content and security findings.
    """
    content: str | None = None
    source_url: str | None = None

    # 1. Fetch content
    if source_type == ImportSourceType.RAW:
        content = source
        if len(content) > MAX_CONTENT_SIZE:
            return ImportResult(
                success=False,
                error=f"Content too large: {len(content)} bytes (max {MAX_CONTENT_SIZE})",
            )
    elif source_type == ImportSourceType.URL:
        content, error = await fetch_from_url(source)
        if error:
            return ImportResult(success=False, error=error)
        source_url = source
    elif source_type == ImportSourceType.GITHUB:
        content, source_url, error = await fetch_from_github(source)
        if error:
            return ImportResult(success=False, error=error, source_url=source_url)
    else:
        return ImportResult(success=False, error=f"Unknown source type: {source_type}")

    if not content or not content.strip():
        return ImportResult(success=False, error="Empty content")

    # 2. Parse frontmatter
    frontmatter, body = _parse_frontmatter(content)

    # 3. Detect definition type
    def_type = None
    if definition_type_hint:
        try:
            def_type = DefinitionType(definition_type_hint)
        except ValueError:
            pass
    if not def_type:
        def_type = _detect_definition_type(frontmatter, content, source_url or source)

    # 4. Extract metadata
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    skill_names = frontmatter.get("skill_names", [])

    if not name:
        return ImportResult(
            success=False,
            error="No 'name' field in YAML frontmatter. Expected: ---\\nname: my-definition\\n---",
            content=content[:200],
        )

    # 5. Compute content hash
    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # 6. Run security scan
    security_findings = _run_security_scan(content, name)

    logger.info(
        "Import scan complete: name=%s, type=%s, findings=%d (critical=%d)",
        name, def_type.value, len(security_findings),
        sum(1 for f in security_findings if f.severity == SecuritySeverity.CRITICAL),
    )

    return ImportResult(
        success=True,
        definition_type=def_type,
        name=name,
        description=description,
        content=content,
        skill_names=skill_names if isinstance(skill_names, list) else [],
        source_url=source_url,
        content_hash=content_hash,
        security_findings=security_findings,
    )

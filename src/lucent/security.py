"""Content security scanning for prompt injection defense-in-depth.

Scans memory content for known prompt injection patterns before saving.
This is a defense-in-depth measure — suspicious content is flagged but
not blocked, since legitimate memories may discuss these topics.

See: Security audit finding 10.2
"""

import re

from lucent.logging import get_logger

logger = get_logger("security.content_scan")

# Each pattern is a tuple of (compiled_regex, description).
# Patterns use IGNORECASE and word boundaries where appropriate.
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Instructions to override system prompt
    (
        re.compile(
            r"ignore\s+(all\s+)?previous\s+(instructions|prompts|rules)",
            re.IGNORECASE,
        ),
        "instruction_override",
    ),
    (
        re.compile(r"disregard\s+(all\s+)?(your\s+)?instructions", re.IGNORECASE),
        "instruction_override",
    ),
    (
        re.compile(r"forget\s+(all\s+)?(your\s+)?instructions", re.IGNORECASE),
        "instruction_override",
    ),
    # Tool execution commands
    (
        re.compile(r"\bcall\s+bash\b", re.IGNORECASE),
        "tool_execution",
    ),
    (
        re.compile(r"\brun\s+command\b", re.IGNORECASE),
        "tool_execution",
    ),
    (
        re.compile(r"\bexecute\s+code\b", re.IGNORECASE),
        "tool_execution",
    ),
    (
        re.compile(r"\buse\s+the\s+bash\s+tool\b", re.IGNORECASE),
        "tool_execution",
    ),
    # File system access
    (
        re.compile(r"\bread\s+file\b", re.IGNORECASE),
        "filesystem_access",
    ),
    (
        re.compile(r"\bcat\s+/etc\b", re.IGNORECASE),
        "filesystem_access",
    ),
    (
        re.compile(r"\bread\s+\.daemon_api_key\b", re.IGNORECASE),
        "filesystem_access",
    ),
    (
        re.compile(r"\bread\s+\.env\b", re.IGNORECASE),
        "filesystem_access",
    ),
    # Exfiltration attempts
    (
        re.compile(r"\bcurl\s", re.IGNORECASE),
        "exfiltration",
    ),
    (
        re.compile(r"\bwget\s", re.IGNORECASE),
        "exfiltration",
    ),
    (
        re.compile(r"\bsend\s+to\b", re.IGNORECASE),
        "exfiltration",
    ),
    (
        re.compile(r"https?://", re.IGNORECASE),
        "exfiltration",
    ),
    # Secret access
    (
        re.compile(r"\bsecret://", re.IGNORECASE),
        "secret_access",
    ),
    (
        re.compile(r"\bapi_key\b", re.IGNORECASE),
        "secret_access",
    ),
    (
        re.compile(r"GET\s+/api/secrets", re.IGNORECASE),
        "secret_access",
    ),
]


def scan_content_for_injection(content: str) -> list[str]:
    """Scan memory content for known prompt injection patterns.

    Args:
        content: The memory content string to scan.

    Returns:
        A list of matched pattern descriptions (e.g. ["instruction_override",
        "exfiltration"]). Empty list if no patterns matched.
    """
    if not content:
        return []

    matched: list[str] = []
    seen: set[str] = set()

    for pattern, description in _INJECTION_PATTERNS:
        if description not in seen and pattern.search(content):
            matched.append(description)
            seen.add(description)

    if matched:
        logger.warning(
            "Suspicious content detected in memory: patterns=%s",
            matched,
        )

    return matched

"""Repository-name normalization helpers for metadata fields."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def normalize_repository_full_name(value: str) -> str:
    """Normalize a GitHub repository reference to ``owner/repo``.

    Technical memory metadata uses ``repo`` as an ACL and Knowledge Tree grouping
    key. Bare project names such as ``lucent`` create orphan tree roots and skip
    GitHub existence checks, so repository metadata must be an explicit full
    name. GitHub clone/browser URLs are accepted and reduced to their first two
    path components for convenience.

    Args:
        value: Repository full name or GitHub URL.

    Returns:
        The normalized ``owner/repo`` full name.

    Raises:
        ValueError: If the value cannot be interpreted as a GitHub ``owner/repo``.
    """
    if not isinstance(value, str):
        raise ValueError("Repository name must be a string in owner/repo format.")

    cleaned = value.strip().strip("`'\" ")
    if not cleaned:
        raise ValueError("Repository name cannot be empty.")

    cleaned = _repo_from_github_reference(cleaned)
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    cleaned = cleaned.strip("/")

    parts = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "Technical memory metadata.repo must be a GitHub repository full name "
            "in owner/repo format. Use metadata.repo='owner/repo', or omit repo "
            "when the canonical repository is unknown."
        )

    owner, repo = parts
    if not _GITHUB_OWNER_RE.fullmatch(owner) or not _GITHUB_REPO_RE.fullmatch(repo):
        raise ValueError(
            "Technical memory metadata.repo must be a valid GitHub owner/repo "
            "full name with no whitespace or extra path segments."
        )

    return f"{owner}/{repo}"


def is_repository_full_name(value: str | None) -> bool:
    """Return True when ``value`` is a valid repository full name/reference."""
    if not value:
        return False
    try:
        normalize_repository_full_name(value)
    except ValueError:
        return False
    return True


def _repo_from_github_reference(value: str) -> str:
    """Extract owner/repo from common GitHub URL/SSH forms if present."""
    ssh_match = re.fullmatch(r"git@github\.com:([^/]+)/(.+)", value, flags=re.IGNORECASE)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2).rstrip('/')}"

    if value.lower().startswith("github.com/"):
        value = f"https://{value}"

    parsed = urlparse(value)
    if parsed.scheme:
        hostname = (parsed.hostname or "").lower()
        if hostname != "github.com":
            raise ValueError(
                "Repository URLs in memory metadata.repo must point to github.com "
                "and identify a repository as owner/repo."
            )
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(path_parts) < 2:
            raise ValueError(
                "GitHub repository URLs in memory metadata.repo must include owner/repo."
            )
        return f"{path_parts[0]}/{path_parts[1]}"

    return value

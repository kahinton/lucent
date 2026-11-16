"""Script task kind: config validation, network checks, and output parsing.

A ``script`` task executes a program directly inside an approved sandbox
template — no agent session, no chat model, no engine. This module owns the
parts shared by authoring (config shape, template/network checks) and dispatch
(interpreter derivation, stdout parsing, output-contract validation), so a
script authored through any surface is held to the same rules.

Config shape (``execution_config``)::

    {
      "source": "inline" | "git",          # required
      "script": "<body>",                  # required for inline
      "interpreter": "python3",            # optional; shebang wins, else python3
      "repo_url": "https://...", "ref": "...", "path": "bin/run",  # for git
      "credential": "secret://git.token",  # optional; a reference, never a token
      "stdin": "...",                      # optional payload on stdin
      "env": {"NAME": "secret://..."},     # optional process environment
    }

A git source is resolved at dispatch: the executor shallow-fetches ``ref``
(commit SHA, branch, or tag) into a task-scoped directory under the sandbox
working dir and runs ``path`` from the checkout. Only ``https://`` URLs are
accepted — ssh/scp-style transports are rejected at authoring time. Tokens are
never stored in the config: ``credential`` (and any sensitive ``env`` value)
must be a ``secret://`` or ``credential://`` reference resolved by the secrets
service at dispatch.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from typing import Any
from urllib.parse import urlparse

from jsonschema import ValidationError, validate

SCRIPT_KIND = "script"
SCRIPT_SOURCES = ("inline", "git")
SCRIPT_DEFAULT_INTERPRETER = "python3"

# A ref that looks like a commit SHA. Git resolves short SHAs; classification
# only decides whether the result must be reported as "not pinned".
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# The credential helper written into the sandbox for git-sourced clones. It
# reads the token from the exec environment (never from the script, the
# command line, the repository URL, or on-disk config), so the token exists
# only in the lifetime of the clone process.
GIT_ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  *Username*) printf "%s\\n" "${LUCENT_GIT_USERNAME:-x-access-token}" ;;\n'
    '  *Password*) printf "%s\\n" "${LUCENT_GIT_TOKEN:-}" ;;\n'
    '  *) printf "\\n" ;;\n'
    "esac\n"
)

# Explicit transport caps. stdout/stderr are bounded when they leave the
# sandbox, and failure diagnostics carry a bounded preview — a runaway script
# must not be able to fill the task row or the event log.
SCRIPT_STDOUT_CAP = 50_000
SCRIPT_STDERR_CAP = 20_000
SCRIPT_PREVIEW_CHARS = 2_000

SCRIPT_CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source"],
    "additionalProperties": False,
    "properties": {
        "source": {"enum": list(SCRIPT_SOURCES)},
        "script": {"type": "string", "description": "Program body for inline source"},
        "interpreter": {
            "type": "string",
            "description": "Command to run the script with; derived from the shebang when omitted",
        },
        "repo_url": {"type": "string"},
        "ref": {"type": "string"},
        "path": {"type": "string"},
        "credential": {
            "type": "string",
            "description": (
                "secret:// or credential:// reference for a private repository; "
                "a literal token is rejected"
            ),
        },
        "stdin": {"type": "string"},
        "env": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}

_KNOWN_CONFIG_KEYS = frozenset(SCRIPT_CONFIG_SCHEMA["properties"])


class ScriptConfigError(ValueError):
    """Raised when a script task's execution_config is malformed."""


def validate_repo_url(repo_url: str) -> str:
    """Require an HTTPS repository URL and return it unchanged.

    Sandboxed clones are HTTPS-only: ssh/scp-style transports need host keys
    and an agent socket the sandbox does not have, and plain http would put a
    credential on the wire. Credentials must come from a reference, so a URL
    carrying userinfo is rejected before it can be persisted.
    """
    url = str(repo_url or "").strip()
    if not url:
        raise ScriptConfigError("A git-sourced script task requires 'repo_url'")
    if any(char in url for char in ("\n", "\r", "\t", " ")):
        raise ScriptConfigError(
            "execution_config.repo_url must not contain whitespace or control characters"
        )
    if "://" not in url:
        raise ScriptConfigError(
            "execution_config.repo_url must be an https:// URL — ssh/scp-style "
            f"URLs are not supported in sandboxed clones (got {url!r}); use "
            "https://host/owner/repo.git"
        )

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise ScriptConfigError(
            "execution_config.repo_url must use https:// — "
            f"{scheme or 'unknown'!r} URLs are not supported in sandboxed "
            "clones; use https://host/owner/repo.git"
        )
    if not parsed.hostname:
        raise ScriptConfigError(
            "execution_config.repo_url must name a host (https://host/owner/repo.git)"
        )
    if parsed.username or parsed.password:
        raise ScriptConfigError(
            "execution_config.repo_url must not embed credentials — set "
            "execution_config.credential to a secret:// or credential:// "
            "reference instead"
        )
    return url


def validate_git_ref(ref: str) -> str:
    """Require a non-empty ref that cannot be mistaken for a git option."""
    value = str(ref or "").strip()
    if not value:
        raise ScriptConfigError("A git-sourced script task requires 'ref'")
    if value.startswith("-"):
        raise ScriptConfigError(
            f"execution_config.ref must not start with '-' (got {value!r})"
        )
    if any(char in value for char in ("\n", "\r", "\t", " ")):
        raise ScriptConfigError(
            "execution_config.ref must not contain whitespace or control characters"
        )
    return value


def normalize_repo_path(path: str) -> str:
    """Return a repository-relative path that cannot escape the checkout."""
    value = str(path or "").strip()
    if not value:
        raise ScriptConfigError("A git-sourced script task requires 'path'")
    if value.startswith(("/", "~")) or "\\" in value or "\x00" in value:
        raise ScriptConfigError(
            f"execution_config.path must be relative to the repository root "
            f"(got {value!r})"
        )
    normalized = posixpath.normpath(value)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise ScriptConfigError(
            f"execution_config.path must name a file inside the repository "
            f"(got {value!r})"
        )
    return normalized


def validate_credential_reference(value: str) -> str:
    """Require a secrets-service reference, never a literal token."""
    from lucent.secrets.utils import is_credential_reference, is_secret_reference

    text = str(value or "").strip()
    if not (is_secret_reference(text) or is_credential_reference(text)):
        raise ScriptConfigError(
            "execution_config.credential must be a secret:// or credential:// "
            "reference resolved by the secrets service — a literal token must "
            "never be stored in execution_config"
        )
    return text


def is_commit_sha(ref: str) -> bool:
    """Whether ``ref`` is shaped like a commit SHA rather than a branch/tag."""
    return bool(_COMMIT_SHA_RE.match(str(ref or "").strip().lower()))


def ref_pinning(ref: str, resolved_sha: str | None) -> dict[str, Any]:
    """Describe whether the configured ref resolved to a pinned commit.

    A branch or tag resolves to whatever it points at now, so a run is not
    reproducible from the config alone. The result records the actual SHA and
    a warning instead of silently claiming determinism.
    """
    requested = str(ref or "").strip()
    sha = str(resolved_sha or "").strip().lower()
    pinned = bool(sha) and is_commit_sha(requested) and sha.startswith(
        requested.lower()
    )
    if pinned:
        return {
            "ref": requested,
            "ref_kind": "commit",
            "resolved_sha": sha,
            "pinned": True,
            "warning": None,
        }
    if is_commit_sha(requested):
        kind = "commit"
        warning = (
            f"Ref '{requested}' did not resolve to that commit (resolved to "
            f"{sha or 'unknown'}); the run is not pinned to the requested SHA"
        )
    else:
        kind = "branch_or_tag"
        warning = (
            f"Ref '{requested}' is a branch or tag, so it was not pinned to a "
            f"commit SHA (resolved to {sha or 'unknown'}); use a full "
            "40-character commit SHA for reproducible runs"
        )
    return {
        "ref": requested,
        "ref_kind": kind,
        "resolved_sha": sha,
        "pinned": False,
        "warning": warning,
    }


def git_resolution_steps(
    repo_url: str, ref: str, repo_dir: str
) -> list[tuple[str, list[str]]]:
    """Return the shallow, ref-addressed steps that materialize a checkout.

    ``git init`` + ``fetch --depth=1 <ref>`` handles a commit SHA, a branch,
    and a tag uniformly, and keeps history at depth 1. Every step is argv (no
    shell) so a URL or ref can never be re-interpreted as a command.
    """
    return [
        ("init", ["git", "init", "-q", repo_dir]),
        ("remote", ["git", "-C", repo_dir, "remote", "add", "origin", repo_url]),
        ("fetch", ["git", "-C", repo_dir, "fetch", "--depth=1", "origin", ref]),
        (
            "checkout",
            ["git", "-C", repo_dir, "checkout", "-q", "--detach", "FETCH_HEAD"],
        ),
        ("rev_parse", ["git", "-C", repo_dir, "rev-parse", "HEAD"]),
    ]



def validate_script_config(config: dict[str, Any] | None) -> None:
    """Structural validation of a script task's ``execution_config``.

    Registered on the task-kind spec, so every authoring surface reaches this
    through :func:`lucent.task_kinds.validate_task_kind_payload`.
    """
    if not isinstance(config, dict) or not config:
        raise ScriptConfigError(
            "A script task requires execution_config with at least 'source'"
        )

    unknown = set(config) - _KNOWN_CONFIG_KEYS
    if unknown:
        raise ScriptConfigError(
            f"execution_config has unsupported keys {sorted(unknown)}; "
            f"allowed: {sorted(_KNOWN_CONFIG_KEYS)}"
        )

    source = config.get("source")
    if source not in SCRIPT_SOURCES:
        raise ScriptConfigError(
            f"execution_config.source must be one of {list(SCRIPT_SOURCES)}; "
            f"got {source!r}"
        )

    interpreter = config.get("interpreter")
    if interpreter is not None and (
        not isinstance(interpreter, str) or not interpreter.strip()
    ):
        raise ScriptConfigError("execution_config.interpreter must be a non-empty string")

    stdin = config.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        raise ScriptConfigError("execution_config.stdin must be a string")

    env = config.get("env")
    if env is not None:
        if not isinstance(env, dict):
            raise ScriptConfigError("execution_config.env must be an object")
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ScriptConfigError(
                    "execution_config.env must map string names to string values"
                )
        # Sensitive names must carry a reference, not a literal value — the
        # config is persisted and returned on every task read.
        from lucent.secrets.utils import validate_env_var_references

        validate_env_var_references(env)

    if source == "inline":
        script = config.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ScriptConfigError(
                "An inline script task requires a non-empty 'script' body"
            )
    else:  # git
        validate_repo_url(str(config.get("repo_url") or ""))
        validate_git_ref(str(config.get("ref") or ""))
        normalize_repo_path(str(config.get("path") or ""))
        credential = config.get("credential")
        if credential is not None:
            validate_credential_reference(str(credential))


def interpreter_from_shebang(line: str | None) -> str | None:
    """Return the interpreter named by a shebang line, if it has one."""
    first_line = (line or "").splitlines()[0].strip() if (line or "").strip() else ""
    if not first_line.startswith("#!"):
        return None
    shebang = first_line[2:].strip()
    try:
        parts = shlex.split(shebang)
    except ValueError:
        parts = shebang.split()
    if len(parts) >= 2 and parts[0].endswith("/env"):
        return parts[1]
    return parts[0] if parts else None


def derive_interpreter(config: dict[str, Any]) -> str:
    """Return the command used to run an inline script.

    An explicit ``interpreter`` wins. Otherwise a shebang line is honoured
    (``#!/usr/bin/env python3`` → ``python3``), and a script with neither
    defaults to ``python3``.
    """
    explicit = config.get("interpreter")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    body = config.get("script") or ""
    return interpreter_from_shebang(body) or SCRIPT_DEFAULT_INTERPRETER


def interpreter_argv(interpreter: str) -> list[str]:
    """Split an interpreter command into an argv prefix."""
    return shlex.split(interpreter)


def repo_host(repo_url: str) -> str | None:
    """Extract the host from an http(s)/ssh/git/scp-style repository URL."""
    url = str(repo_url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.hostname
    if not host and "://" not in url and "@" in url:
        # scp-style: git@github.com:org/repo.git
        host = url.split("@", 1)[1].split(":", 1)[0]
    return host.lower() if host else None


def template_reaches_host(template_config: dict[str, Any] | None, host: str) -> bool:
    """Whether a resolved sandbox template's network policy can reach ``host``.

    An unknown/absent template (``None``) is treated as permissive here — this
    helper only narrows a decision that the template actually expresses.
    """
    if not template_config:
        return True
    mode = str(template_config.get("network_mode") or "").strip().lower()
    if mode in ("", "bridge"):
        return True
    if mode == "none":
        return False
    allowed = template_config.get("allowed_hosts") or []
    host = host.lower()
    for entry in allowed:
        candidate = str(entry).strip().lower()
        if not candidate:
            continue
        if candidate.startswith("*."):
            candidate = candidate[2:]
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def validate_script_task(
    *,
    config: dict[str, Any] | None,
    sandbox_template_id: str | None,
    sandbox_config: dict[str, Any] | None = None,
    template_config: dict[str, Any] | None = None,
    inline_sandbox_config: bool = False,
) -> None:
    """Validate a script task's whole payload at creation time.

    ``template_config`` is the resolved template (``to_sandbox_config`` output)
    when the caller could resolve one. It is what makes the git-host network
    rejection specific instead of a generic dispatch failure later.
    """
    validate_script_config(config)
    assert config is not None  # validate_script_config guarantees a dict

    if not sandbox_template_id:
        raise ScriptConfigError(
            "A script task requires sandbox_template_id — scripts run only "
            "inside an approved sandbox template"
        )
    if inline_sandbox_config and sandbox_config:
        raise ScriptConfigError(
            "A script task cannot use inline sandbox_config; choose an "
            "approved sandbox template with sandbox_template_id"
        )

    if config.get("source") != "git":
        return

    host = repo_host(str(config.get("repo_url") or ""))
    if not host:
        raise ScriptConfigError(
            "A git-sourced script task requires a repo_url with a resolvable host"
        )
    if template_config is not None and not template_reaches_host(
        template_config, host
    ):
        mode = template_config.get("network_mode")
        allowed = list(template_config.get("allowed_hosts") or [])
        raise ScriptConfigError(
            f"Sandbox template cannot reach git host '{host}' "
            f"(network_mode={mode!r}, allowed_hosts={allowed!r}); add the host "
            "to the template's network allowlist or use an inline script"
        )


def truncate(text: str | None, cap: int) -> tuple[str, bool]:
    """Cap ``text`` and report whether anything was removed."""
    value = "" if text is None else str(text)
    if len(value) <= cap:
        return value, False
    return (
        f"{value[:cap]}\n... [truncated {len(value) - cap} chars]",
        True,
    )


def preview(text: str | None, limit: int = SCRIPT_PREVIEW_CHARS) -> str:
    """A bounded, single-purpose preview for failure diagnostics."""
    return truncate(text, limit)[0]


def parse_script_stdout(
    stdout: str | None,
    output_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    """Parse stdout and, when a contract exists, validate it.

    Returns a mapping with ``parsed``, ``result_structured``,
    ``validation_status``, ``validation_errors`` and ``preview``. A schema
    violation is reported here; the executor turns it into a task failure
    (never a retry).
    """
    raw = stdout or ""
    result: dict[str, Any] = {
        "parsed": None,
        "result_structured": None,
        "validation_status": "not_applicable",
        "validation_errors": None,
        "preview": preview(raw),
    }

    if not output_contract:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return result
        result["parsed"] = parsed
        result["result_structured"] = (
            parsed if isinstance(parsed, dict) else {"value": parsed}
        )
        return result

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        result["validation_status"] = "invalid"
        result["validation_errors"] = [
            f"script stdout is not valid JSON: {exc}"
        ]
        return result

    result["parsed"] = parsed
    if not isinstance(parsed, dict):
        result["validation_status"] = "invalid"
        result["validation_errors"] = [
            "script stdout JSON must be an object to store as result_structured"
        ]
        return result

    schema = output_contract.get("json_schema") or {}
    try:
        validate(instance=parsed, schema=schema)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        result["validation_status"] = "invalid"
        result["validation_errors"] = [f"{path}: {exc.message}"]
        return result

    result["result_structured"] = parsed
    result["validation_status"] = "valid"
    return result

"""Template binding for declarative tool requests.

Declarative tools describe a request with placeholders drawn from the model's
arguments::

    {"path": "/v1/weather/{city}", "query": {"units": "{units}"},
     "headers": {"X-Api-Key": "{{credential:weather/token}}"}}

This module renders those templates. It is small on purpose, and it is
*deliberately not* a general templating engine: there is no expression
evaluation, no attribute access, no loops, no includes. A tool definition is
authored by a user, but it is *executed by the server*, so the substitution
surface has to be incapable of doing anything except copying values in.

Design rules:

* ``{name}`` resolves from the model's validated arguments only.
* ``{{env:NAME}}`` resolves from the tool's resolved credential map only.
* Substituted values are always inserted as a single opaque token — a model
  argument containing ``{other}`` is not re-expanded, so arguments cannot
  reference each other or reach credentials.
* :func:`render_path_value` percent-encodes substituted path segments, so an
  argument cannot escape its path segment with ``../`` or ``?``.
* Unknown placeholders are an error, not an empty string; silently sending a
  request with a missing path segment is how you get a 404 that looks like an
  outage.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

__all__ = [
    "TemplateError",
    "render_header_value",
    "render_path_value",
    "render_scalar",
    "referenced_argument_names",
]

# ``{name}`` for arguments, ``{{env:NAME}}`` for resolved credentials.
# A single combined pattern is used so substitution is one pass: two sequential
# passes over the same string is how a substituted value gets re-expanded as a
# template (a credential containing ``{city}`` would resolve the model's city
# argument, and an argument containing ``{{env:TOKEN}}`` would resolve the secret).
_TEMPLATE_RE = re.compile(
    r"\{\{env:([A-Za-z0-9_.:-]+)\}\}"          # group 1: credential key
    r"|(?<!\{)\{([A-Za-z_][A-Za-z0-9_.-]{0,63})\}(?!\})"  # group 2: argument name
)

# Header values must not be able to inject a second header or terminate the
# field. httpx already rejects CR/LF, but failing with our own message is
# clearer than a transport error deep in the stack.
_FORBIDDEN_HEADER_CHARS = re.compile(r"[\r\n\x00]")


class TemplateError(ValueError):
    """A template references something unavailable or malformed."""


def referenced_argument_names(template: str) -> set[str]:
    """Argument names a template depends on (used for validation + docs)."""
    if not isinstance(template, str):
        return set()
    return {match.group(2) for match in _TEMPLATE_RE.finditer(template) if match.group(2)}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    # Objects and arrays have no single sensible textual rendering in a path or
    # query value; serializing them silently is how ``[object Object]`` reaches
    # an upstream API. Reject and let the author map the field explicitly.
    raise TemplateError(
        f"Cannot substitute a {type(value).__name__} into a request template; "
        "use a scalar argument or declare a nested field in input_schema"
    )


def _resolve(
    template: str,
    arguments: dict[str, Any],
    credentials: dict[str, str],
    *,
    encode: Any = None,
) -> str:
    """One-pass substitution of credentials and arguments.

    ``encode`` transforms each substituted value (percent-encoding for path
    segments); it is applied *after* resolution and never to the template itself,
    so literal characters in the template keep their meaning.
    """
    if not isinstance(template, str):
        raise TemplateError("Template must be a string")

    missing_arguments: list[str] = []
    unresolved_credentials: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        credential_key, argument_name = match.group(1), match.group(2)
        if credential_key is not None:
            if credential_key not in credentials:
                unresolved_credentials.append(credential_key)
                return match.group(0)
            value = str(credentials[credential_key])
        else:
            if argument_name not in arguments:
                missing_arguments.append(argument_name)
                return match.group(0)
            value = _stringify(arguments[argument_name])
        return encode(value) if encode is not None else value

    rendered = _TEMPLATE_RE.sub(_replace, template)
    if unresolved_credentials:
        raise TemplateError(
            "Template references unresolved credential(s): "
            + ", ".join(sorted(set(unresolved_credentials)))
        )
    if missing_arguments:
        raise TemplateError(
            "Request template references argument(s) the tool does not receive: "
            + ", ".join(sorted(set(missing_arguments)))
        )
    return rendered


def render_path_value(
    template: str,
    arguments: dict[str, Any],
    credentials: dict[str, str],
) -> str:
    """Render a path template, percent-encoding the substituted values.

    Substituted values are fully encoded, *including* dots. Only encoding ``/``
    is not enough: a value of ``..`` would otherwise survive as a literal segment
    and an HTTP client or upstream router may normalise it, walking up out of the
    operation's path. ``quote`` leaves ``.`` unescaped by design, so it is encoded
    explicitly here.

    The template's own ``/`` separators and literal characters are preserved, so
    ``/v1/{city}/daily`` keeps its shape while a city of ``../../admin`` becomes
    ``%2E%2E%2F%2E%2E%2Fadmin``.
    """

    def _encode_path_segment(value: str) -> str:
        return quote(value, safe="").replace(".", "%2E")

    return _resolve(template, arguments, credentials, encode=_encode_path_segment)


def render_scalar(
    template: str,
    arguments: dict[str, Any],
    credentials: dict[str, str],
) -> str:
    """Render a query/body scalar. No percent-encoding; the client encodes."""
    return _resolve(template, arguments, credentials)


def render_header_value(
    template: str,
    arguments: dict[str, Any],
    credentials: dict[str, str],
) -> str:
    """Render a header value, rejecting CR/LF and NUL injection."""
    rendered = _resolve(template, arguments, credentials)
    if _FORBIDDEN_HEADER_CHARS.search(rendered):
        raise TemplateError(
            "A substituted value contains a newline or NUL byte, which cannot appear "
            "in an HTTP header"
        )
    return rendered

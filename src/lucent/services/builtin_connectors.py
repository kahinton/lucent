"""Built-in MCP connectors: hosted endpoints Lucent ships, credentialed per user.

The distinction from :mod:`lucent.services.connector_catalog` matters:

* a **preset** is a convenience — it fills a form, and the resulting definition
  belongs to whoever created it;
* a **built-in connector** is seeded into every organization as a
  ``scope='built-in'`` MCP server definition, already active, and **requires no
  administrator**. Each user supplies their own credential and it works.

That second shape is the one that answers "why is this per-person?". The endpoint is
shared; the *token* is not. GitHub is the clearest example: a hosted remote MCP
server exists at a fixed URL, and the only thing that differs between two people is
their personal access token.

## How the credential actually resolves

Nothing here stores a token. A definition declares a slot::

    {"name": "GITHUB_TOKEN", "header": "Authorization",
     "format": "Bearer {value}", "secret_key": "github.mcp.token"}

and its ``headers`` map contains ``{"Authorization": "secret://github.mcp.token"}``.
Resolution runs through :func:`lucent.secrets.utils.resolve_secret_reference`, which
is **clearance-driven for the calling user**: their own secret first, then a
group-shared one, then an explicit grant. So:

* if the user added ``github.mcp.token`` themselves, it works;
* if an admin added it to an organization-shared secret, it still works for anyone
  cleared for it — which is how a team avoids 30 identical PATs;
* if neither exists, the connector is reported as **needs setup**, not as an error.

That last point is why :func:`builtin_connector` records ``secret_key``: the
composition step needs to distinguish "not configured yet" from "configured but
broken" to say anything useful.

## What is shipped, and why so little

Only connectors whose *endpoint is stable and publicly documented* are here. Shipping
a built-in that 404s for every new organization is worse than not shipping it. Each
entry names the vendor doc it came from.

A built-in can also be disabled per organization; nothing about the mechanism
requires every connector to be on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_CONNECTORS",
    "BuiltinConnector",
    "CredentialSlot",
    "builtin_connector_names",
    "credential_slots_for",
    "get_builtin_connector",
    "list_builtin_connectors",
]


@dataclass(frozen=True)
class CredentialSlot:
    """One credential a connector needs, declared so a UI can prompt for it."""

    #: Environment-style name, unique within the connector. Used as the key in
    #: ``env_vars`` and in the error message when it is missing.
    name: str
    #: Header to inject the resolved value into.
    header: str
    #: Surrounding text. ``{value}`` is replaced with the resolved secret, so
    #: ``"Bearer {value}"`` yields an ``Authorization: Bearer …`` header.
    format: str = "{value}"
    label: str = ""
    help: str = ""
    required: bool = True
    #: The ``secret://`` key the user (or an admin) should create. Stable across
    #: organizations so one documented instruction works everywhere.
    secret_key: str = ""
    #: Where the vendor lets a user mint this credential.
    create_url: str = ""

    def header_value(self, resolved: str) -> str:
        """Render this slot's header value from a resolved credential."""
        return self.format.replace("{value}", resolved)


@dataclass(frozen=True)
class BuiltinConnector:
    """A hosted MCP endpoint Lucent seeds into every organization."""

    name: str
    vendor: str
    summary: str
    url: str
    slots: tuple[CredentialSlot, ...]
    transport: str = "http"
    #: Where this endpoint is documented.
    docs_url: str = ""
    #: What the server exposes, so an approver knows the blast radius.
    exposes: str = ""
    #: Optional read-only variant. When set, the seeded definition points at it,
    #: because a built-in that ships writable by default is a poor default.
    readonly_url: str = ""
    #: Extra headers that are *not* credentials (e.g. a required Accept type).
    static_headers: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_url(self) -> str:
        return self.readonly_url or self.url

    def header_templates(self) -> dict[str, str]:
        """The ``headers`` map a seeded definition should carry.

        Credential slots become ``secret://`` references — never literals — so the
        stored row is safe to read and the value is resolved per calling user.
        """
        headers = dict(self.static_headers)
        for slot in self.slots:
            if not slot.secret_key:
                continue
            headers[slot.header] = f"secret://{slot.secret_key}"
        return headers

    def env_vars(self) -> dict[str, str]:
        """``env_vars`` entries so the secret keys are discoverable in the UI.

        Both transports read from the same secret store; recording them here means
        a stdio connector and a remote one describe their needs identically.
        """
        return {
            slot.name: f"secret://{slot.secret_key}"
            for slot in self.slots
            if slot.secret_key
        }

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "summary": self.summary,
            "url": self.effective_url,
            "transport": self.transport,
            "docs_url": self.docs_url,
            "exposes": self.exposes,
            "readonly": bool(self.readonly_url),
            "notes": list(self.notes),
            "credential_slots": [
                {
                    "name": slot.name,
                    "header": slot.header,
                    "label": slot.label,
                    "help": slot.help,
                    "required": slot.required,
                    "secret_key": slot.secret_key,
                    "create_url": slot.create_url,
                }
                for slot in self.slots
            ],
        }


#: Shared help text: the instructions are the same for every PAT.
_PAT_HELP = (
    "Add a secret with this exact key in Settings → Secrets, or let an "
    "administrator add it organization-wide so the whole team shares one token."
)


BUILTIN_CONNECTORS: tuple[BuiltinConnector, ...] = (
    BuiltinConnector(
        name="github",
        vendor="GitHub",
        summary=(
            "Repositories, issues, pull requests, and Actions for the account that "
            "owns the token. Add a personal access token and it works — no app "
            "registration, no admin."
        ),
        # The hosted GitHub MCP server. The read-only variant is the default: a
        # built-in connector that can push to every repo the token reaches is a
        # bad first-run experience, and the writable endpoint is one URL away.
        url="https://api.githubcopilot.com/mcp/",
        readonly_url="https://api.githubcopilot.com/mcp/readonly",
        docs_url=(
            "https://docs.github.com/en/copilot/how-tos/context/"
            "extend-copilot-chat-with-mcp"
        ),
        exposes="Repos, issues, PRs, workflows, repository content",
        slots=(
            CredentialSlot(
                name="GITHUB_TOKEN",
                header="Authorization",
                format="Bearer {value}",
                label="GitHub personal access token",
                help=(
                    "Create a fine-grained token at github.com/settings/tokens with "
                    "read access to the repositories you want the agent to see. "
                    "Lucent ships the read-only endpoint by default; switch the URL "
                    "to /mcp/ if you need writes. " + _PAT_HELP
                ),
                secret_key="github.mcp.token",
                create_url="https://github.com/settings/tokens",
            ),
        ),
        notes=(
            "Seeded as the read-only endpoint. An owner can point it at /mcp/ to "
            "allow writes.",
            "Scopes on the token decide what the agent can reach; Lucent does not "
            "narrow them further.",
        ),
    ),
    BuiltinConnector(
        name="sentry",
        vendor="Sentry",
        summary="Issues, events, and traces for one Sentry organization.",
        url="https://mcp.sentry.dev/mcp",
        docs_url="https://mcp.sentry.dev",
        exposes="Issues, events, traces, Seer root-cause analysis",
        slots=(
            CredentialSlot(
                name="SENTRY_TOKEN",
                header="Authorization",
                format="Bearer {value}",
                label="Sentry auth token",
                help=(
                    "Create an auth token in Sentry under Settings → Auth Tokens. "
                    + _PAT_HELP
                ),
                secret_key="sentry.mcp.token",
                create_url="https://sentry.io/settings/account/api/auth-tokens/",
            ),
        ),
        notes=(
            "Scope the endpoint to one organization by appending /<org> to the URL.",
        ),
    ),
)


_BY_NAME = {connector.name: connector for connector in BUILTIN_CONNECTORS}


def get_builtin_connector(name: str) -> BuiltinConnector:
    """Resolve a built-in connector by name."""
    return _BY_NAME[str(name or "").strip().lower()]


def builtin_connector_names() -> tuple[str, ...]:
    """Names of every shipped connector, in seed order."""
    return tuple(connector.name for connector in BUILTIN_CONNECTORS)


def list_builtin_connectors() -> list[dict[str, Any]]:
    """Serializable descriptors for the UI."""
    return [connector.descriptor() for connector in BUILTIN_CONNECTORS]


def credential_slots_for(connector_name: str) -> list[dict[str, Any]]:
    """Declared slots for one connector, for the ``credential_slots`` column."""
    return list(get_builtin_connector(connector_name).descriptor()["credential_slots"])

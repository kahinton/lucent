"""Curated catalog of real, connectable remote MCP servers.

This is the "connect your accounts" surface: a short list of services an ordinary
person or team actually uses, with the endpoint and the exact onboarding cost.

**Every entry here was verified against the vendor's own documentation.** A
plausible-looking endpoint that 404s is worse than a missing one, because it fails
after the user has already committed to the flow. Roughly 60% of the services we
looked for have no official remote MCP server today; those are deliberately absent
rather than guessed, and Zapier is the honest answer for them (one connection, 9,000
apps).

The field that decides whether this is useful or frustrating is
:attr:`ConnectorPreset.onboarding`, because it describes *who* has to do *what*:

``sign_in``
    The service supports OAuth with dynamic client registration (DCR) or client ID
    metadata documents (CIMD). The user clicks Connect and signs in. Nothing to
    configure.
``api_key``
    The user pastes a token from the service's own settings page. Easy, but the
    token is a long-lived credential.
``admin_app``
    The service requires a **pre-registered OAuth application**: someone has to
    create an app in that vendor's console and paste a client ID/secret into
    Lucent. This is real work and must be advertised *before* the click, not
    discovered halfway through an auth flow that then fails.
``none``
    Public, unauthenticated, read-only.

``sign_in`` entries are the default path. ``admin_app`` entries are shown but
badged, so nobody starts a flow they cannot finish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CONNECTOR_CATALOG",
    "ConnectorPreset",
    "connector_descriptor",
    "connector_catalog",
    "get_connector",
    "connector_categories",
]


@dataclass(frozen=True)
class ConnectorPreset:
    """One pre-filled remote MCP server.

    ``slug`` is stable and is what the UI posts back; ``name`` becomes the
    definition's ``name`` and must stay within the 64-character column.
    """

    slug: str
    vendor: str
    name: str
    summary: str
    url: str
    category: str
    onboarding: str
    #: ``http`` (Streamable HTTP) or ``sse``. Kept explicit because a wrong guess
    #: produces a protocol error rather than a fallback.
    transport: str = "http"
    #: What the user has to do, in their words.
    setup_note: str = ""
    #: Auth mechanism, for the badge. Not used to build a request.
    auth_kind: str = "oauth"
    docs_url: str = ""
    #: True when the vendor publishes a read-only variant, so we can point at it
    #: by default and let an owner opt into writes.
    has_readonly_variant: bool = False
    readonly_url: str = ""
    #: Roughly what the server exposes, so the user knows the blast radius.
    exposes: str = ""
    #: Optional ``runtime_config``-style hints surfaced in the detail text.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_descriptor(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "vendor": self.vendor,
            "name": self.name,
            "summary": self.summary,
            "url": self.url,
            "transport": self.transport,
            "category": self.category,
            "onboarding": self.onboarding,
            "setup_note": self.setup_note,
            "auth_kind": self.auth_kind,
            "docs_url": self.docs_url,
            "has_readonly_variant": self.has_readonly_variant,
            "readonly_url": self.readonly_url,
            "exposes": self.exposes,
            "notes": list(self.notes),
            # A short human label so the UI does not have to map the enum.
            "onboarding_label": _ONBOARDING_LABELS.get(
                self.onboarding, self.onboarding
            ),
            "one_click": self.onboarding == "sign_in",
        }


_ONBOARDING_LABELS = {
    "sign_in": "Sign in — nothing to configure",
    "api_key": "Needs an API token",
    "admin_app": "Admin must create an app first",
    "none": "Public, no sign-in",
}

_CATEGORY_ORDER = (
    "Get anything done",
    "Docs & notes",
    "Project & tasks",
    "Calendar & files",
    "Money",
    "Customers",
    "Code & deploys",
    "Data & databases",
    "Marketing & support",
    "Ops & on-call",
    "Places & weather",
)


CONNECTOR_CATALOG: tuple[ConnectorPreset, ...] = (
    # ── Get anything done ────────────────────────────────────────────────
    ConnectorPreset(
        slug="zapier",
        vendor="Zapier",
        name="zapier",
        summary=(
            "One connection to 9,000+ apps and 40,000+ actions — Gmail, Calendar, "
            "Slack, Jira, Asana, HubSpot, Sheets and most things that are not listed "
            "below. Start here if the service you want is missing."
        ),
        url="https://mcp.zapier.com/api/v1/connect",
        category="Get anything done",
        onboarding="sign_in",
        setup_note=(
            "Connect Zapier once, then enable the specific actions you want. Each "
            "action you enable becomes callable."
        ),
        docs_url="https://docs.zapier.com/mcp/overview/how-connections-work",
        exposes="Whatever actions you enable, from any connected app",
        notes=(
            "Each successful tool call consumes 2 Zapier tasks.",
            "Streamable HTTP only — no SSE endpoint.",
        ),
    ),
    ConnectorPreset(
        slug="make",
        vendor="Make",
        name="make",
        summary="Run Make scenarios and manage them from chat.",
        url="https://mcp.make.com/v2",
        category="Get anything done",
        onboarding="sign_in",
        setup_note="Sign in and pick the organization plus scopes at the consent screen.",
        docs_url="https://developers.make.com/mcp-server/connect-using-oauth",
        exposes="Scenario runs; editing needs a paid plan",
    ),
    # ── Docs & notes ────────────────────────────────────────────────────
    ConnectorPreset(
        slug="notion",
        vendor="Notion",
        name="notion",
        summary="Search, read, and write pages and databases, and upload files.",
        url="https://mcp.notion.com/mcp",
        category="Docs & notes",
        onboarding="sign_in",
        setup_note="Click Connect and sign in to Notion. No app setup required.",
        docs_url="https://developers.notion.com/guides/mcp/get-started-with-mcp",
        exposes="Pages, databases, comments, file uploads, connected sources",
        notes=("Also supports the legacy SSE transport at the same host.",),
    ),
    ConnectorPreset(
        slug="atlassian",
        vendor="Atlassian",
        name="atlassian",
        summary="Jira, Confluence, Bitbucket, and Loom in one connection — 215+ tools.",
        url="https://mcp.atlassian.com/v2/mcp",
        category="Docs & notes",
        onboarding="sign_in",
        setup_note="Sign in with Atlassian; tools are limited to your own permissions.",
        docs_url=(
            "https://support.atlassian.com/atlassian-ai-gateway/docs/"
            "get-started-with-the-atlassian-remote-mcp-server/"
        ),
        exposes="10+ Atlassian apps, read and write, 215+ tools",
        notes=("Consumes Atlassian Rovo credits.",),
    ),
    ConnectorPreset(
        slug="google-maps",
        vendor="Google",
        name="google-maps",
        summary="Search places, look up weather, and compute routes.",
        url="https://mapstools.googleapis.com/mcp",
        category="Places & weather",
        onboarding="api_key",
        setup_note=(
            "Create a Google Cloud API key and put it in the Headers field as "
            "X-Goog-Api-Key, using a credential reference rather than the raw key."
        ),
        auth_kind="api_key",
        docs_url="https://developers.google.com/maps/ai/grounding-lite",
        exposes="Five read-only tools: places, weather, routes, name resolution",
        notes=("Pay per request; 10,000 free requests per month.",),
    ),
    # ── Project & tasks ─────────────────────────────────────────────────
    ConnectorPreset(
        slug="linear",
        vendor="Linear",
        name="linear",
        summary="Read and write issues, projects, and comments.",
        url="https://mcp.linear.app/mcp",
        category="Project & tasks",
        onboarding="sign_in",
        setup_note="Click Connect and sign in to Linear.",
        docs_url="https://linear.app/docs/mcp",
        exposes="Issues, projects, comments",
        has_readonly_variant=True,
        readonly_url="https://mcp.linear.app/mcp/readonly",
    ),
    ConnectorPreset(
        slug="asana",
        vendor="Asana",
        name="asana",
        summary="Tasks, projects, portfolios, and comments.",
        url="https://mcp.asana.com/v2/mcp",
        category="Project & tasks",
        onboarding="sign_in",
        setup_note=(
            "Sign in with Asana. Some clients need their own OAuth client; if the "
            "sign-in is rejected, use an Asana personal access token instead."
        ),
        docs_url="https://developers.asana.com/docs/using-asanas-mcp-server",
        exposes="Tasks, projects, portfolios, sections, comments; read and write",
    ),
    ConnectorPreset(
        slug="monday",
        vendor="monday.com",
        name="monday",
        summary="Boards, items, columns, docs, dashboards, and forms — 60+ tools.",
        url="https://mcp.monday.com/mcp",
        category="Project & tasks",
        onboarding="sign_in",
        setup_note=(
            "Sign in with monday.com. An account admin may need to approve the "
            "connection and its redirect URI before it works."
        ),
        docs_url=(
            "https://developer.monday.com/api-reference/docs/"
            "integrate-with-monday-mcp"
        ),
        exposes="Boards, items, columns, docs, dashboards, forms, automations",
    ),
    ConnectorPreset(
        slug="clickup",
        vendor="ClickUp",
        name="clickup",
        summary="Tasks, lists, docs, and time tracking.",
        url="https://mcp.clickup.com/mcp",
        category="Project & tasks",
        onboarding="sign_in",
        setup_note="Sign in with ClickUp. OAuth is the only option — no API tokens.",
        auth_kind="oauth",
        docs_url=(
            "https://developer.clickup.com/docs/"
            "connect-an-ai-assistant-to-clickups-mcp-server"
        ),
        exposes="Tasks, lists, docs, time, comments",
        notes=("Deliberately exposes no delete tools.",),
    ),
    # ── Money ───────────────────────────────────────────────────────────
    ConnectorPreset(
        slug="stripe",
        vendor="Stripe",
        name="stripe",
        summary="Customers, payments, invoices, subscriptions, and disputes.",
        url="https://mcp.stripe.com",
        category="Money",
        onboarding="sign_in",
        setup_note=(
            "Sign in with Stripe, or paste a restricted agent API key. Full secret "
            "keys are rejected."
        ),
        docs_url="https://docs.stripe.com/mcp",
        exposes="Most of the Stripe API plus search and analytics tools",
        notes=(
            "Refunds and outbound payments require human confirmation inside Stripe.",
            "Prefer an agent-restricted key over OAuth for unattended agents.",
        ),
    ),
    ConnectorPreset(
        slug="paypal",
        vendor="PayPal",
        name="paypal",
        summary="Invoices, orders, payments, disputes, and shipping.",
        url="https://mcp.paypal.com/http",
        category="Money",
        onboarding="sign_in",
        setup_note=(
            "Sign in with PayPal, or supply a client ID and secret. Use "
            "mcp.sandbox.paypal.com for sandbox testing."
        ),
        docs_url="https://developer.paypal.com/ai-tools/mcp-server",
        exposes="Invoices, orders, payments, disputes, subscriptions, shipping",
    ),
    # ── Code & deploys ──────────────────────────────────────────────────
    ConnectorPreset(
        slug="github",
        vendor="GitHub",
        name="github",
        summary="Repositories, issues, pull requests, and Actions.",
        url="https://api.githubcopilot.com/mcp/",
        category="Code & deploys",
        onboarding="sign_in",
        setup_note=(
            "Sign in with a GitHub account, or paste a personal access token. "
            "The token's scopes decide what the agent can reach."
        ),
        auth_kind="oauth_or_pat",
        has_readonly_variant=True,
        readonly_url="https://api.githubcopilot.com/mcp/readonly",
        docs_url="https://docs.github.com/en/copilot/how-tos/context/extend-copilot-chat-with-mcp",
        exposes="Repos, issues, PRs, workflows, repository content",
    ),
    ConnectorPreset(
        slug="gitlab",
        vendor="GitLab",
        name="gitlab",
        summary="Projects, issues, merge requests, CI, and wikis.",
        url="https://gitlab.com/api/v4/mcp",
        category="Code & deploys",
        onboarding="sign_in",
        setup_note=(
            "Sign in with GitLab. For a self-hosted instance use "
            "https://<your-instance>/api/v4/mcp instead."
        ),
        docs_url="https://docs.gitlab.com/user/model_context_protocol/mcp_server/",
        exposes="Projects, issues, MRs, work items, CI, wikis, code security",
        notes=("Fine-grained tokens need the `mcp` scope; legacy PATs are rejected.",),
    ),
    ConnectorPreset(
        slug="cloudflare",
        vendor="Cloudflare",
        name="cloudflare",
        summary="Search and run roughly 2,500 Cloudflare API operations.",
        url="https://mcp.cloudflare.com/mcp",
        category="Code & deploys",
        onboarding="sign_in",
        setup_note="Sign in and choose scopes at the consent screen, or paste an API token.",
        auth_kind="oauth_or_api_key",
        docs_url=(
            "https://developers.cloudflare.com/agents/model-context-protocol/"
            "cloudflare/servers-for-cloudflare/"
        ),
        exposes="Code-mode search/execute over 2,500+ API endpoints",
    ),
    ConnectorPreset(
        slug="netlify",
        vendor="Netlify",
        name="netlify",
        summary="Deploy context, project info, and docs.",
        url="https://netlify-mcp.netlify.app/mcp",
        category="Code & deploys",
        onboarding="sign_in",
        setup_note="Click Connect and sign in to Netlify.",
        docs_url="https://docs.netlify.com/build/build-with-ai/netlify-mcp-server/",
        exposes="Deploy and project context, docs, agent skills",
    ),
    ConnectorPreset(
        slug="vercel",
        vendor="Vercel",
        name="vercel",
        summary="Projects, deployments, logs, and analytics.",
        url="https://mcp.vercel.com",
        category="Code & deploys",
        onboarding="admin_app",
        setup_note=(
            "Vercel maintains an approved-client list. Submit their client review "
            "form before this connection will complete the sign-in."
        ),
        docs_url="https://vercel.com/docs/agent-resources/vercel-mcp",
        exposes="Docs search, projects, deployments, logs, Web Analytics",
    ),
    # ── Data & databases ────────────────────────────────────────────────
    ConnectorPreset(
        slug="supabase",
        vendor="Supabase",
        name="supabase",
        summary="Tables, SQL, migrations, logs, and edge functions.",
        url="https://mcp.supabase.com/mcp",
        category="Data & databases",
        onboarding="sign_in",
        setup_note=(
            "Sign in with Supabase and pick a project. Add ?read_only=true to the "
            "URL to prevent writes."
        ),
        docs_url="https://supabase.com/docs/guides/getting-started/mcp",
        exposes="Tables, migrations, SQL, logs, advisors, edge functions, storage",
    ),
    ConnectorPreset(
        slug="neon",
        vendor="Neon",
        name="neon",
        summary="Projects, branches, schema, and SQL.",
        url="https://mcp.neon.tech/mcp",
        category="Data & databases",
        onboarding="sign_in",
        setup_note=(
            "Sign in with Neon and choose scopes, or paste a Neon API key. Add "
            "?readonly=true to the URL to prevent writes."
        ),
        auth_kind="oauth_or_api_key",
        docs_url="https://neon.com/docs/ai/neon-mcp-server",
        exposes="Projects, branches, schema, SQL, auth, functions, observability",
    ),
    # ── Marketing & support ─────────────────────────────────────────────
    ConnectorPreset(
        slug="hubspot",
        vendor="HubSpot",
        name="hubspot",
        summary="CRM contacts, companies, deals, and marketing content.",
        url="https://mcp.hubspot.com",
        category="Marketing & support",
        onboarding="admin_app",
        setup_note=(
            "An admin must create an MCP connector in HubSpot (Development → MCP "
            "Connectors) and paste the client ID, secret, and redirect URL into "
            "Lucent's connection settings. PKCE is required."
        ),
        docs_url=(
            "https://developers.hubspot.com/docs/apps/developer-platform/"
            "build-apps/connect-your-app/integrate-with-hubspot-mcp-server"
        ),
        exposes="CRM read/write, marketing email, campaigns, pages, conversations",
    ),
    ConnectorPreset(
        slug="slack",
        vendor="Slack",
        name="slack",
        summary="Search messages and files, send and schedule messages, canvases.",
        url="https://mcp.slack.com/mcp",
        category="Marketing & support",
        onboarding="admin_app",
        setup_note=(
            "Slack does not support dynamic client registration. An admin must "
            "register a Slack app and share its client ID with Lucent before this "
            "can complete sign-in."
        ),
        docs_url="https://docs.slack.dev/ai/slack-mcp-server",
        exposes="Messages, files, users, channels, canvases, lists, reactions",
        notes=("Streamable HTTP only — Slack publishes no SSE endpoint.",),
    ),
    ConnectorPreset(
        slug="box",
        vendor="Box",
        name="box",
        summary="Search files, read content, and use Box AI.",
        url="https://mcp.box.com",
        category="Docs & notes",
        onboarding="admin_app",
        setup_note=(
            "A Box admin must enable MCP and create Integration Credentials, then "
            "paste the client ID and secret into Lucent's connection settings."
        ),
        docs_url="https://developer.box.com/guides/box-mcp/",
        exposes="Search, Box AI, folders, file content",
    ),
    # ── Ops & on-call ───────────────────────────────────────────────────
    ConnectorPreset(
        slug="sentry",
        vendor="Sentry",
        name="sentry",
        summary="Issues, events, traces, and root-cause analysis.",
        url="https://mcp.sentry.dev/mcp",
        category="Ops & on-call",
        onboarding="sign_in",
        setup_note=(
            "Sign in with Sentry. Scope it to one organization by using "
            "/mcp/<org> or /mcp/<org>/<project> instead of /mcp."
        ),
        docs_url="https://mcp.sentry.dev",
        exposes="Issues, events, traces, Seer root-cause analysis",
    ),
    ConnectorPreset(
        slug="pagerduty",
        vendor="PagerDuty",
        name="pagerduty",
        summary="Incidents, services, and on-call schedules.",
        url="https://mcp.pagerduty.com/mcp",
        category="Ops & on-call",
        onboarding="api_key",
        setup_note=(
            "Paste a PagerDuty API token into the Headers field as "
            "Authorization: Token token=<token>, using a credential reference."
        ),
        auth_kind="api_key_or_oauth",
        docs_url="https://docs.pagerduty.com/developer/mcp-tooling-remote-server",
        exposes="Incidents, services, schedules; read and write",
    ),
    # ── Public / no sign-in ─────────────────────────────────────────────
    ConnectorPreset(
        slug="twilio-docs",
        vendor="Twilio",
        name="twilio-docs",
        summary="Search 1,800 Twilio, SendGrid, and Segment API endpoints and docs.",
        url="https://mcp.twilio.com/docs",
        category="Code & deploys",
        onboarding="none",
        auth_kind="none",
        setup_note="No sign-in. Read-only documentation and API search.",
        docs_url="https://www.twilio.com/docs/ai/mcp",
        exposes="Read-only search and retrieve; cannot execute anything",
    ),
)


_BY_SLUG = {preset.slug: preset for preset in CONNECTOR_CATALOG}


def get_connector(slug: str) -> ConnectorPreset:
    """Resolve a preset by slug. Raises ``KeyError`` for an unknown slug."""
    return _BY_SLUG[str(slug or "").strip().lower()]


def connector_descriptor(slug: str) -> dict[str, Any]:
    """Serializable form of one preset (used by the form prefill)."""
    return get_connector(slug).to_descriptor()


def connector_categories() -> list[str]:
    """Categories present in the catalog, in presentation order."""
    present = {preset.category for preset in CONNECTOR_CATALOG}
    ordered = [name for name in _CATEGORY_ORDER if name in present]
    # Anything a future entry adds that is not in the order list still appears.
    ordered.extend(sorted(present - set(ordered)))
    return ordered


def connector_catalog() -> list[dict[str, Any]]:
    """All presets as descriptors, ordered by category then vendor.

    ``sign_in`` entries sort first within a category: they are the ones a user can
    actually finish without involving an administrator, so burying them under
    admin-gated entries would make the whole surface look harder than it is.
    """
    order = {name: index for index, name in enumerate(connector_categories())}
    # `sign_in` leads: it is the only class a user can finish unaided. `none`
    # (public, read-only) is useful but rarely the thing someone came to connect,
    # and `admin_app` is last because it cannot complete without an administrator.
    onboarding_rank = {"sign_in": 0, "api_key": 1, "none": 2, "admin_app": 3}
    presets = sorted(
        CONNECTOR_CATALOG,
        key=lambda preset: (
            order.get(preset.category, 999),
            onboarding_rank.get(preset.onboarding, 9),
            preset.vendor.lower(),
        ),
    )
    return [preset.to_descriptor() for preset in presets]

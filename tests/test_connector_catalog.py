"""Tests for the curated remote-MCP connector catalog.

These are deliberately strict about two things:

* **Verification discipline.** Every entry claims to be a real vendor endpoint. A
  wrong URL is worse than a missing one because it fails after the user has
  committed. The tests pin the shape (https, no placeholders) so a future
  hand-edit cannot slip in a guess.
* **Onboarding honesty.** ``onboarding`` drives a warning in the UI. If an entry
  that needs a pre-registered OAuth app were labelled ``sign_in``, a user would
  start a flow that cannot complete — so the classification is asserted, not
  trusted.
"""

from __future__ import annotations

import pytest

from lucent.services.connector_catalog import (
    CONNECTOR_CATALOG,
    connector_catalog,
    connector_categories,
    connector_descriptor,
    get_connector,
)

VALID_ONBOARDING = {"sign_in", "api_key", "admin_app", "none"}
VALID_TRANSPORTS = {"http", "sse"}


class TestCatalogShape:
    def test_slugs_are_unique(self):
        slugs = [preset.slug for preset in CONNECTOR_CATALOG]
        assert len(slugs) == len(set(slugs))

    def test_names_fit_the_database_column(self):
        """``mcp_server_configs.name`` is VARCHAR(64)."""
        for preset in CONNECTOR_CATALOG:
            assert len(preset.name) <= 64, preset.slug
            assert preset.name, preset.slug

    def test_every_url_is_absolute_https(self):
        for preset in CONNECTOR_CATALOG:
            assert preset.url.startswith("https://"), preset.slug
            # No documentation placeholders may reach the catalog: a user cannot
            # substitute them from a form field.
            assert "<" not in preset.url and ">" not in preset.url, preset.slug
            assert "YOUR_" not in preset.url, preset.slug

    def test_readonly_variants_are_absolute_https(self):
        for preset in CONNECTOR_CATALOG:
            if preset.has_readonly_variant:
                assert preset.readonly_url.startswith("https://"), preset.slug

    def test_onboarding_and_transport_are_known_values(self):
        for preset in CONNECTOR_CATALOG:
            assert preset.onboarding in VALID_ONBOARDING, preset.slug
            assert preset.transport in VALID_TRANSPORTS, preset.slug

    def test_every_entry_is_describable(self):
        for preset in CONNECTOR_CATALOG:
            assert preset.summary.strip(), preset.slug
            assert preset.vendor.strip(), preset.slug
            # An admin-gated entry must explain what the admin has to do, or the
            # warning banner has nothing to say.
            if preset.onboarding == "admin_app":
                assert preset.setup_note.strip(), preset.slug

    def test_categories_cover_every_entry(self):
        present = {preset.category for preset in CONNECTOR_CATALOG}
        covered = set(connector_categories())
        assert present == covered


class TestOnboardingClassification:
    def test_services_known_to_require_a_registered_app_are_labelled_so(self):
        """Slack and HubSpot document no DCR; mislabelling them would strand users."""
        for slug in ("slack", "hubspot", "box", "vercel"):
            assert get_connector(slug).onboarding == "admin_app", slug

    def test_dcr_capable_services_are_single_click(self):
        for slug in ("notion", "linear", "supabase", "gitlab", "monday"):
            assert get_connector(slug).onboarding == "sign_in", slug

    def test_unauthenticated_entry_is_marked_none(self):
        readonly = [p for p in CONNECTOR_CATALOG if p.onboarding == "none"]
        assert readonly, "the catalog should keep at least one no-sign-in entry"
        for preset in readonly:
            assert preset.auth_kind == "none", preset.slug


class TestOrdering:
    def test_sign_in_entries_lead_within_a_category(self):
        """A user-facing gallery must not bury the entries that actually work."""
        rank = {"sign_in": 0, "api_key": 1, "none": 2, "admin_app": 3}
        by_category: dict[str, list[int]] = {}
        for descriptor in connector_catalog():
            by_category.setdefault(descriptor["category"], []).append(
                rank[descriptor["onboarding"]]
            )
        for category, ranks in by_category.items():
            assert ranks == sorted(ranks), category

    def test_one_click_flag_matches_onboarding(self):
        for descriptor in connector_catalog():
            assert descriptor["one_click"] == (descriptor["onboarding"] == "sign_in")


class TestLookup:
    def test_get_connector_is_case_and_space_tolerant(self):
        assert get_connector("  Linear ").slug == "linear"

    def test_unknown_slug_raises(self):
        with pytest.raises(KeyError):
            get_connector("not-a-real-service")

    def test_descriptor_is_json_serializable(self):
        import json

        payload = connector_descriptor("stripe")
        assert json.loads(json.dumps(payload))["url"] == "https://mcp.stripe.com"


class TestStdioEnvironmentIsolation:
    """A spawned stdio MCP server must not inherit this process's secrets.

    The server launches third-party packages (`npx -y <pkg>`) as children. Copying
    the whole environment into that child hands an arbitrary npm supply chain the
    database DSN, every provider API key, and the OpenBao token. The fix is an
    allowlist, and these tests keep it from silently widening.
    """

    def test_allowlist_contains_no_secret_shaped_variable(self):
        import re

        from lucent.services.mcp_discovery import _INHERITED_ENV_ALLOWLIST

        offenders = [
            key
            for key in _INHERITED_ENV_ALLOWLIST
            if re.search(r"TOKEN|SECRET|KEY|PASSWORD|DATABASE|DSN|CREDENTIAL", key.upper())
        ]
        assert not offenders, (
            "A stdio MCP server would inherit these secrets: " + ", ".join(offenders)
        )

    def test_allowlist_covers_what_a_child_actually_needs(self):
        from lucent.services.mcp_discovery import _INHERITED_ENV_ALLOWLIST

        # Without PATH a spawned npx/uvx cannot find its own binaries.
        assert "PATH" in _INHERITED_ENV_ALLOWLIST
        assert "HOME" in _INHERITED_ENV_ALLOWLIST

    def test_lucent_runtime_variables_are_not_inherited(self):
        from lucent.services.mcp_discovery import _INHERITED_ENV_ALLOWLIST

        for key in _INHERITED_ENV_ALLOWLIST:
            assert not key.startswith("LUCENT_"), key
            assert not key.startswith("VAULT_"), key

    def test_stdio_discovery_builds_a_filtered_environment(self):
        """The child env must be the allowlist plus resolved config, nothing more."""
        import inspect

        from lucent.services import mcp_discovery

        source = inspect.getsource(mcp_discovery._discover_stdio)
        assert "os.environ.copy()" not in source, (
            "stdio discovery must not copy the full server environment"
        )
        assert "_INHERITED_ENV_ALLOWLIST" in source

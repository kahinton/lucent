"""Unit tests for identity configuration helpers.

These cover the pure decision functions — the ones that encode security rules
and therefore deserve tests that do not need a database, network, or browser.
"""

import pytest

from lucent.identity import sso_config
from lucent.identity.sso_config import SsoConfigError


class TestSlugValidation:
    def test_accepts_simple_slug(self):
        assert sso_config.validate_slug("acme-okta") == "acme-okta"

    def test_lowercases(self):
        assert sso_config.validate_slug("AcmeOkta") == "acmeokta"

    def test_rejects_path_traversal(self):
        with pytest.raises(SsoConfigError):
            sso_config.validate_slug("../etc")

    def test_rejects_leading_hyphen(self):
        with pytest.raises(SsoConfigError):
            sso_config.validate_slug("-acme")

    def test_rejects_dots(self):
        # A dot would let the slug collide with a file-like path segment.
        with pytest.raises(SsoConfigError):
            sso_config.validate_slug("acme.okta")

    def test_rejects_empty(self):
        with pytest.raises(SsoConfigError):
            sso_config.validate_slug("   ")

    def test_rejects_overlong(self):
        with pytest.raises(SsoConfigError):
            sso_config.validate_slug("a" * 64)


class TestRoleMapping:
    def test_defaults_to_member(self):
        role = sso_config.role_from_mappings(
            claim_values=[], role_mappings=[], default_role="member"
        )
        assert role == "member"

    def test_highest_matching_role_wins(self):
        # A user in both groups must get admin regardless of claim order.
        role = sso_config.role_from_mappings(
            claim_values=["lucent-members", "lucent-admins"],
            role_mappings=[
                {"idp_value": "lucent-members", "role": "member"},
                {"idp_value": "lucent-admins", "role": "admin"},
            ],
            default_role="member",
        )
        assert role == "admin"

    def test_highest_wins_regardless_of_order(self):
        role = sso_config.role_from_mappings(
            claim_values=["lucent-members", "lucent-admins"],
            role_mappings=[
                {"idp_value": "lucent-admins", "role": "admin"},
                {"idp_value": "lucent-members", "role": "member"},
            ],
            default_role="member",
        )
        assert role == "admin"

    def test_match_is_case_insensitive(self):
        role = sso_config.role_from_mappings(
            claim_values=["LUCENT-ADMINS"],
            role_mappings=[{"idp_value": "lucent-admins", "role": "admin"}],
            default_role="member",
        )
        assert role == "admin"

    def test_cannot_escalate_to_hyperadmin(self):
        role = sso_config.role_from_mappings(
            claim_values=["instance-operators"],
            role_mappings=[{"idp_value": "instance-operators", "role": "hyperadmin"}],
            default_role="member",
        )
        # An IdP claim must never mint an instance operator.
        assert role == "member"

    def test_invalid_default_role_falls_back(self):
        role = sso_config.role_from_mappings(
            claim_values=[], role_mappings=[], default_role="hyperadmin"
        )
        assert role == "member"

    def test_malformed_mappings_are_ignored(self):
        role = sso_config.role_from_mappings(
            claim_values=["admins"],
            role_mappings=["not-a-dict", {"idp_value": "admins"}, {"role": "admin"}],
            default_role="member",
        )
        assert role == "member"


class TestEmailDomainAllowlist:
    def test_empty_allowlist_permits_any(self):
        assert sso_config.email_domain_allowed("a@anywhere.com", []) is True

    def test_matching_domain_permitted(self):
        assert sso_config.email_domain_allowed("a@acme.com", ["acme.com"]) is True

    def test_matching_is_case_insensitive(self):
        assert sso_config.email_domain_allowed("a@ACME.com", ["acme.com"]) is True

    def test_other_domain_rejected(self):
        assert sso_config.email_domain_allowed("a@evil.com", ["acme.com"]) is False

    def test_subdomain_not_implicitly_allowed(self):
        # `mail.acme.com` is a different domain than `acme.com`; allowing it
        # implicitly would let a delegated mail host widen the trust boundary.
        assert sso_config.email_domain_allowed("a@mail.acme.com", ["acme.com"]) is False

    def test_missing_email_rejected_when_restricted(self):
        assert sso_config.email_domain_allowed(None, ["acme.com"]) is False

    def test_missing_email_permitted_when_unrestricted(self):
        assert sso_config.email_domain_allowed(None, []) is True


class TestSafeReturnTo:
    def test_relative_path_allowed(self):
        assert sso_config.safe_return_to("/settings/users") == "/settings/users"

    def test_none_becomes_root(self):
        assert sso_config.safe_return_to(None) == "/"

    def test_protocol_relative_rejected(self):
        # `//evil.com` is a browser-meaningful absolute URL, not a path.
        assert sso_config.safe_return_to("//evil.com") == "/"

    def test_absolute_url_rejected(self):
        assert sso_config.safe_return_to("https://evil.com") == "/"

    def test_backslash_rejected(self):
        # Some browsers treat `/\evil.com` as `//evil.com`.
        assert sso_config.safe_return_to("/\\evil.com") == "/"

    def test_header_injection_rejected(self):
        assert sso_config.safe_return_to("/foo\r\nSet-Cookie: x=1") == "/"


class TestNormalization:
    def test_normalize_domains_from_string(self):
        assert sso_config.normalize_domains("Acme.com, @beta.io") == ["acme.com", "beta.io"]

    def test_normalize_domains_from_list(self):
        assert sso_config.normalize_domains(["acme.com", "ACME.com"]) == ["acme.com"]

    def test_normalize_domains_bare_at_sign(self):
        assert sso_config.normalize_domains("@acme.com") == ["acme.com"]

    def test_normalize_certificates_extracts_pem(self):
        pem = (
            "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
        )
        assert sso_config.normalize_certificates(f"junk {pem} junk") == [pem]

    def test_normalize_certificates_skips_blank(self):
        assert sso_config.normalize_certificates(["", "   "]) == []

    def test_normalize_certificates_accepts_string(self):
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
        assert sso_config.normalize_certificates(pem) == [pem]


class TestConnectionValidation:
    def test_oidc_requires_client_id(self):
        with pytest.raises(SsoConfigError, match="client ID"):
            sso_config.validate_connection_fields(
                provider_type="oidc",
                issuer="https://idp.example.com",
                client_id=None,
                idp_entity_id=None,
                idp_sso_url=None,
                idp_certificates=None,
            )

    def test_oidc_requires_issuer(self):
        with pytest.raises(SsoConfigError, match="issuer"):
            sso_config.validate_connection_fields(
                provider_type="oidc",
                issuer=None,
                client_id="abc",
                idp_entity_id=None,
                idp_sso_url=None,
                idp_certificates=None,
            )

    def test_oidc_rejects_http_issuer(self):
        # A plaintext issuer would let anyone on the path substitute the JWKS.
        with pytest.raises(SsoConfigError, match="https"):
            sso_config.validate_connection_fields(
                provider_type="oidc",
                issuer="http://idp.example.com",
                client_id="abc",
                idp_entity_id=None,
                idp_sso_url=None,
                idp_certificates=None,
            )

    def test_github_does_not_require_issuer(self):
        # GitHub is OAuth, not OIDC: no discovery document exists.
        sso_config.validate_connection_fields(
            provider_type="github",
            issuer=None,
            client_id="abc",
            idp_entity_id=None,
            idp_sso_url=None,
            idp_certificates=None,
        )

    def test_saml_requires_certificate(self):
        with pytest.raises(SsoConfigError, match="certificate"):
            sso_config.validate_connection_fields(
                provider_type="saml",
                issuer=None,
                client_id=None,
                idp_entity_id="https://idp/entity",
                idp_sso_url="https://idp/sso",
                idp_certificates=[],
            )

    def test_saml_rejects_garbage_certificate(self):
        with pytest.raises(SsoConfigError):
            sso_config.validate_connection_fields(
                provider_type="saml",
                issuer=None,
                client_id=None,
                idp_entity_id="https://idp/entity",
                idp_sso_url="https://idp/sso",
                idp_certificates=["not a certificate"],
            )

    def test_unknown_provider_rejected(self):
        with pytest.raises(SsoConfigError, match="Unsupported"):
            sso_config.validate_connection_fields(
                provider_type="ldap",
                issuer=None,
                client_id=None,
                idp_entity_id=None,
                idp_sso_url=None,
                idp_certificates=None,
            )

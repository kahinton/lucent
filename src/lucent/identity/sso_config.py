"""Identity-provider configuration and secret handling for SSO.

The connection row in ``sso_connections`` holds everything about an IdP *except*
its client secret. Secrets live in the configured secret store (OpenBao Transit
by default, the built-in Fernet store in development) under a per-connection
key, for the same reason model-provider keys do: a database dump must not yield
a credential that can impersonate the tenant's identity provider.

This module is the single place that knows the secret key naming scheme, so
there is exactly one answer to "where is the client secret".
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import asyncpg

from lucent.logging import get_logger

logger = get_logger("identity.sso")

#: Secret key template. The connection id (not the slug) is the discriminator:
#: slugs are renameable, ids are not.
CLIENT_SECRET_KEY_TEMPLATE = "identity.sso.{connection_id}.client_secret"

#: Presets for providers whose OAuth endpoints are fixed and well known. An
#: OIDC connection can instead supply an issuer and be discovered. GitHub is
#: listed separately because it is *not* an OIDC provider — it has no id_token
#: and no discovery document — so it needs its own endpoints and an explicit
#: user API call. Pretending it is OIDC would mean shipping code that silently
#: skips signature verification.
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "google": {
        "provider_type": "oidc",
        "issuer": "https://accounts.google.com",
        "display_name": "Google",
    },
    "microsoft": {
        "provider_type": "oidc",
        "issuer": "https://login.microsoftonline.com/common/v2.0",
        "display_name": "Microsoft",
    },
    "okta": {"provider_type": "oidc", "issuer": "", "display_name": "Okta"},
    "auth0": {"provider_type": "oidc", "issuer": "", "display_name": "Auth0"},
    "github": {
        "provider_type": "github",
        "issuer": "https://github.com",
        "display_name": "GitHub",
    },
}

#: GitHub OAuth endpoints. Fixed by GitHub, so hardcoded rather than
#: configured — a tenant-supplied token URL for GitHub would be a
#: credential-exfiltration vector.
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class SsoConfigError(Exception):
    """A connection cannot serve logins as configured."""


def connection_secret_key(connection_id: UUID | str) -> str:
    return CLIENT_SECRET_KEY_TEMPLATE.format(connection_id=str(connection_id))


def _secret_scope(organization_id: UUID | str):
    from lucent.secrets import SecretScope

    return SecretScope(organization_id=str(organization_id), system_managed=True)


async def set_client_secret(
    pool: asyncpg.Pool,
    *,
    connection_id: UUID | str,
    organization_id: UUID | str,
    value: str,
) -> None:
    """Store (or replace) an IdP client secret in the secret store."""
    from lucent.secrets import SecretRegistry

    clean = (value or "").strip()
    if not clean:
        raise SsoConfigError("A client secret is required.")
    await SecretRegistry.get().set(
        connection_secret_key(connection_id), clean, _secret_scope(organization_id)
    )


async def get_client_secret(
    pool: asyncpg.Pool,
    *,
    connection_id: UUID | str,
    organization_id: UUID | str,
) -> str | None:
    """Read the IdP client secret, or ``None`` when unset."""
    from lucent.secrets import SecretRegistry

    try:
        value = await SecretRegistry.get().get(
            connection_secret_key(connection_id), _secret_scope(organization_id)
        )
    except Exception:  # noqa: BLE001 — an unreadable secret means "not configured"
        logger.warning(
            "Could not read SSO client secret for connection %s", connection_id, exc_info=True
        )
        return None
    return value or None


async def delete_client_secret(
    pool: asyncpg.Pool,
    *,
    connection_id: UUID | str,
    organization_id: UUID | str,
) -> None:
    from lucent.secrets import SecretRegistry

    try:
        await SecretRegistry.get().delete(
            connection_secret_key(connection_id), _secret_scope(organization_id)
        )
    except Exception:  # noqa: BLE001 — best effort on delete
        logger.warning(
            "Could not delete SSO client secret for connection %s",
            connection_id,
            exc_info=True,
        )


async def has_client_secret(
    pool: asyncpg.Pool,
    *,
    connection_id: UUID | str,
    organization_id: UUID | str,
) -> bool:
    return (
        await get_client_secret(
            pool, connection_id=connection_id, organization_id=organization_id
        )
        is not None
    )


def validate_slug(slug: str) -> str:
    """Normalize and validate a connection slug.

    The slug appears in an unauthenticated URL, so it is restricted to a shape
    that cannot be confused with any other route segment (no dots, no slashes,
    no percent escapes).
    """
    clean = (slug or "").strip().lower()
    if not _SLUG_RE.match(clean):
        raise SsoConfigError(
            "Slug must be lowercase letters, digits, and hyphens, and start with "
            "a letter or digit (max 63 characters)."
        )
    return clean


def validate_connection_fields(
    *,
    provider_type: str,
    issuer: str | None,
    client_id: str | None,
    idp_entity_id: str | None,
    idp_sso_url: str | None,
    idp_certificates: list[str] | None,
) -> None:
    """Reject a connection that could not possibly complete a login.

    Validating at write time rather than login time means a misconfigured
    connection is a visible form error instead of a redirect loop for every
    user.
    """
    if provider_type in ("oidc", "github"):
        if not (client_id or "").strip():
            raise SsoConfigError("A client ID is required for this provider.")
        if provider_type == "oidc" and not (issuer or "").strip():
            raise SsoConfigError(
                "An issuer URL is required for OIDC. It is the trust anchor the "
                "id_token signature and 'iss' claim are verified against."
            )
        if issuer:
            from urllib.parse import urlparse

            parsed = urlparse(issuer)
            if parsed.scheme != "https" or not parsed.netloc:
                raise SsoConfigError("The issuer must be an absolute https:// URL.")
    elif provider_type == "saml":
        if not (idp_entity_id or "").strip():
            raise SsoConfigError("The IdP entity ID is required for SAML.")
        if not (idp_sso_url or "").strip():
            raise SsoConfigError("The IdP SSO URL is required for SAML.")
        from urllib.parse import urlparse

        parsed = urlparse(idp_sso_url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise SsoConfigError("The IdP SSO URL must be an absolute URL.")
        if not idp_certificates:
            raise SsoConfigError(
                "At least one IdP signing certificate is required for SAML. "
                "Without it an assertion signature cannot be verified, and an "
                "unverified assertion is just an attacker-supplied claim."
            )
        for cert in idp_certificates:
            _validate_pem_certificate(cert)
    else:
        raise SsoConfigError(f"Unsupported provider type: {provider_type}")


def _validate_pem_certificate(value: str) -> None:
    """Parse a PEM cert so a typo fails here, not during a login."""
    import base64

    text = (value or "").strip()
    match = re.search(
        r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", text, re.DOTALL
    )
    if not match:
        raise SsoConfigError(
            "Each IdP certificate must be PEM-encoded "
            "(-----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----)."
        )
    body = re.sub(r"\s+", "", match.group(1))
    try:
        der = base64.b64decode(body, validate=True)
    except Exception:  # noqa: BLE001 — any decode failure is the same user error
        raise SsoConfigError("An IdP certificate is not valid base64.") from None
    try:
        from cryptography import x509

        x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001
        raise SsoConfigError("An IdP certificate could not be parsed as X.509.") from None


def normalize_certificates(values: Any) -> list[str]:
    """Accept a string or list, normalize to a list of PEM blocks."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        match = re.search(
            r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.DOTALL
        )
        out.append(match.group(0) if match else text)
    return out


def normalize_domains(values: Any) -> list[str]:
    """Normalize an allowed-domain list to lowercase bare domains."""
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    out: list[str] = []
    for value in values:
        domain = str(value or "").strip().lower().lstrip("@")
        if domain:
            out.append(domain)
    return sorted(set(out))


def email_domain_allowed(email: str | None, allowed_domains: list[str]) -> bool:
    """Whether a mailbox may federate through this connection.

    An empty allowlist means "no domain restriction". Tenants are expected to
    restrict when their IdP is a shared multi-tenant one (Google, Microsoft),
    where otherwise any account on the planet can present a valid id_token.
    """
    if not allowed_domains:
        return True
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain in set(allowed_domains)


def role_from_mappings(
    *,
    claim_values: list[str],
    role_mappings: list[dict[str, Any]],
    default_role: str,
) -> str:
    """Derive a Lucent role from IdP group claims.

    Highest-privilege match wins so that a user in both an admin and a member
    group gets admin regardless of the order the IdP happens to emit groups in.
    The result never exceeds ``owner`` and never becomes ``hyperadmin``: a
    federated identity must not be able to grant instance-operator rights.
    """

    allowed = {"member", "admin", "owner"}
    rank = {"member": 0, "admin": 1, "owner": 2}
    best = default_role if default_role in allowed else "member"
    claimed = {str(value).strip().lower() for value in claim_values if str(value).strip()}

    for mapping in role_mappings or []:
        if not isinstance(mapping, dict):
            continue
        value = str(mapping.get("idp_value", "")).strip().lower()
        role = str(mapping.get("role", "")).strip().lower()
        if not value or role not in allowed:
            continue
        if value in claimed and rank[role] > rank.get(best, 0):
            best = role

    return best if best in allowed else "member"


def safe_return_to(value: str | None) -> str:
    """Constrain a post-login redirect to a same-origin absolute path.

    An open redirect on a login callback is a phishing primitive: the attacker
    sends a link to the real login page and gets the victim bounced to their own
    site already authenticated-looking. Only a leading-slash path is allowed,
    and protocol-relative URLs ("//evil.com") are rejected explicitly.
    """
    if not value:
        return "/"
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return "/"
    return candidate

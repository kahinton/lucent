"""Identity provisioning: invitations, SSO federation, and SCIM.

Sub-modules:

* :mod:`lucent.identity.sso_config` — connection configuration, secret storage,
  and the validation/mapping helpers shared by both protocols.
* :mod:`lucent.identity.oidc` — OpenID Connect (and GitHub OAuth) login.
* :mod:`lucent.identity.saml` — SAML 2.0 SP-initiated login.
* :mod:`lucent.identity.provisioning` — the one code path that turns an
  asserted external identity into a Lucent session, shared by SSO, invitations,
  and SCIM so the safety rules live in exactly one place.
"""

from lucent.identity.sso_config import (  # noqa: F401
    PROVIDER_PRESETS,
    SsoConfigError,
    email_domain_allowed,
    normalize_certificates,
    normalize_domains,
    role_from_mappings,
    safe_return_to,
    validate_connection_fields,
    validate_slug,
)

__all__ = [
    "PROVIDER_PRESETS",
    "SsoConfigError",
    "email_domain_allowed",
    "normalize_certificates",
    "normalize_domains",
    "role_from_mappings",
    "safe_return_to",
    "validate_connection_fields",
    "validate_slug",
]

"""OpenID Connect (and GitHub OAuth) login.

Two protocols share this module because they are the same round trip with one
substantive difference:

* **OIDC** returns a signed ``id_token`` (a JWT) whose signature, issuer,
  audience, expiry and nonce are all verified. Email is taken from verified
  claims.
* **GitHub** is plain OAuth 2.0. It returns no id_token and has no discovery
  document, so there is nothing to signature-check. Identity is established by
  calling GitHub's own API with the access token. This is weaker in principle
  (it trusts the token endpoint's transport plus GitHub's API), which is why it
  is modelled as its own provider type rather than disguised as OIDC.

Security properties implemented here, each of which is load-bearing:

* **PKCE (S256)** on every authorization-code exchange. It binds the code to
  this browser's flow so an intercepted code cannot be redeemed elsewhere.
* **State** is 256 bits, single-use, and consumed atomically in the database.
  A callback whose state has already been seen is refused — that is the CSRF
  and replay defence for the redirect.
* **Nonce** is sent to the IdP and required to match the ``id_token`` claim, so
  a stolen id_token from a different flow cannot be replayed here.
* **Discovery is cached per issuer but re-derived from the configured issuer**,
  never from a URL the caller supplies, so the trust anchor cannot be moved.
* **TLS verification is never disabled** and redirect URIs are absolute https
  when the deployment is not on localhost.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from lucent.identity.provisioning import AssertedIdentity
from lucent.logging import get_logger

logger = get_logger("identity.oidc")

#: How long an in-flight login may take before its state expires. Long enough
#: for an MFA prompt, short enough that an abandoned flow leaves nothing useful.
LOGIN_STATE_TTL_SECONDS = 600

#: Clock skew tolerated when validating ``exp``/``iat``. IdP clocks drift.
CLOCK_SKEW_SECONDS = 120

#: Minimum interval between discovery fetches for one issuer.
DISCOVERY_CACHE_TTL_SECONDS = 3600

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class OidcError(Exception):
    """A login could not be completed. Message is safe to log, not to show verbatim."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """Everything needed to redirect the browser to the IdP."""

    url: str
    state: str
    nonce: str
    code_verifier: str


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64url(digest)
    return verifier, challenge


def _base64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_redirect_uri(base_url: str, *, provider_type: str) -> str:
    """The callback URL registered with the IdP.

    Derived from the request's own base URL so a deployment behind a proxy
    issues the externally visible address. It must match the IdP registration
    exactly, which is why it is not configurable per request.
    """
    root = base_url if base_url.endswith("/") else base_url + "/"
    return f"{root}sso/callback/{'github' if provider_type == 'github' else 'oidc'}"


async def discover(issuer: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch and cache an issuer's OpenID configuration.

    The document is fetched from ``{issuer}/.well-known/openid-configuration``.
    Fetching from anywhere else (a URL supplied by the browser, or a callback
    parameter) would let an attacker point us at their own JWKS and sign
    arbitrary id_tokens, so the issuer is the only input.
    """
    normalized = (issuer or "").rstrip("/")
    if not normalized:
        raise OidcError("OIDC issuer is not configured")

    now = time.monotonic()
    cached = _discovery_cache.get(normalized)
    if cached is not None and now - cached[0] < DISCOVERY_CACHE_TTL_SECONDS:
        return cached[1]

    url = f"{normalized}/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OidcError(f"Could not reach the OIDC discovery document: {exc}") from exc

    if response.status_code != 200:
        raise OidcError(
            f"OIDC discovery returned HTTP {response.status_code} for {normalized}"
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise OidcError("OIDC discovery document was not valid JSON") from exc

    # The document must agree with the issuer we asked about. A mismatch means
    # either a misconfiguration or an active redirect attempt; both are fatal.
    declared_issuer = str(document.get("issuer") or "").rstrip("/")
    if declared_issuer and declared_issuer != normalized:
        raise OidcError(
            f"Discovery document issuer {declared_issuer!r} does not match the "
            f"configured issuer {normalized!r}"
        )
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OidcError(f"Discovery document is missing {required}")

    _discovery_cache[normalized] = (now, document)
    return document


def clear_discovery_cache(issuer: str | None = None) -> None:
    """Drop cached discovery data — used after a connection is reconfigured."""
    if issuer is None:
        _discovery_cache.clear()
    else:
        _discovery_cache.pop((issuer or "").rstrip("/"), None)


async def build_authorization_request(
    connection: dict[str, Any],
    *,
    base_url: str,
    return_to: str | None = None,
) -> AuthorizationRequest:
    """Prepare the redirect for either an OIDC or a GitHub connection."""
    provider_type = connection["provider_type"]
    client_id = (connection.get("client_id") or "").strip()
    if not client_id:
        raise OidcError("This connection has no client ID configured")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce_pair()
    redirect_uri = build_redirect_uri(base_url, provider_type=provider_type)

    if provider_type == "github":
        from lucent.identity.sso_config import GITHUB_AUTHORIZE_URL

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "read:user user:email",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
        return AuthorizationRequest(
            url=f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}",
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

    document = await discover(connection.get("issuer") or "")
    scopes = (connection.get("scopes") or "openid email profile").strip()
    if "openid" not in scopes.split():
        scopes = f"openid {scopes}".strip()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    authorization_endpoint = document["authorization_endpoint"]
    separator = "&" if "?" in authorization_endpoint else "?"
    return AuthorizationRequest(
        url=f"{authorization_endpoint}{separator}{urlencode(params)}",
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
    )


async def complete_login(
    connection: dict[str, Any],
    *,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    nonce: str | None,
    base_url: str,
    timeout: float = 15.0,
) -> AssertedIdentity:
    """Exchange an authorization code and return the asserted identity."""
    if connection["provider_type"] == "github":
        return await _complete_github(
            connection,
            client_secret=client_secret,
            code=code,
            code_verifier=code_verifier,
            base_url=base_url,
            timeout=timeout,
        )
    return await _complete_oidc(
        connection,
        client_secret=client_secret,
        code=code,
        code_verifier=code_verifier,
        nonce=nonce,
        base_url=base_url,
        timeout=timeout,
    )


async def _post_token(
    endpoint: str,
    data: dict[str, str],
    *,
    timeout: float,
    accepts_json: bool,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                endpoint,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
    except httpx.HTTPError as exc:
        raise OidcError(f"Token exchange failed: {exc}") from exc

    if response.status_code != 200:
        # Deliberately does not echo the response body: token endpoints can
        # include the client secret in an error payload.
        raise OidcError(f"Token endpoint returned HTTP {response.status_code}")

    if accepts_json:
        try:
            return response.json()
        except ValueError as exc:
            raise OidcError("Token endpoint returned a non-JSON body") from exc
    # GitHub defaults to form encoding unless asked otherwise; we ask, but be
    # tolerant in case a proxy rewrites the Accept header.
    from urllib.parse import parse_qs

    parsed = parse_qs(response.text)
    return {key: values[0] for key, values in parsed.items() if values}


async def _complete_oidc(
    connection: dict[str, Any],
    *,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    nonce: str | None,
    base_url: str,
    timeout: float,
) -> AssertedIdentity:
    issuer = (connection.get("issuer") or "").rstrip("/")
    document = await discover(issuer)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": build_redirect_uri(base_url, provider_type="oidc"),
        "client_id": (connection.get("client_id") or "").strip(),
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    token_response = await _post_token(
        document["token_endpoint"], data, timeout=timeout, accepts_json=True
    )
    id_token = token_response.get("id_token")
    if not id_token:
        # Without an id_token there is nothing authenticated to trust. An
        # access token alone would mean calling userinfo on the strength of an
        # unverified exchange, which is exactly what OIDC exists to avoid.
        raise OidcError("Token response did not include an id_token")

    claims = _verify_id_token(
        id_token,
        issuer=issuer,
        client_id=(connection.get("client_id") or "").strip(),
        nonce=nonce,
        jwks_uri=str(document["jwks_uri"]),
    )

    email, verified = _extract_email(claims, connection)
    groups = _extract_groups(claims, connection)
    mapping = connection.get("attribute_mapping") or {}

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise OidcError("id_token has no subject claim")

    display_name = (
        claims.get(_mapped(mapping, "display_name", "name"))
        or claims.get("preferred_username")
        or email
    )
    return AssertedIdentity(
        subject=subject,
        email=email,
        email_verified=verified,
        display_name=str(display_name) if display_name else None,
        avatar_url=claims.get(_mapped(mapping, "avatar_url", "picture")),
        groups=groups,
        raw=claims,
    )


def _mapped(mapping: dict[str, Any], logical: str, fallback: str) -> str:
    """Resolve a claim name through the connection's attribute mapping."""
    value = mapping.get(logical) if isinstance(mapping, dict) else None
    return str(value) if value else fallback


def _extract_email(claims: dict[str, Any], connection: dict[str, Any]) -> tuple[str | None, bool]:
    """Pull the email address and its verification state out of the claims.

    OIDC's ``email_verified`` is authoritative. When the IdP omits it entirely,
    the address is treated as **unverified** — the safe default, because an
    unverified address must not be able to adopt an existing account.
    """
    mapping = connection.get("attribute_mapping") or {}
    raw_email = claims.get(_mapped(mapping, "email", "email"))
    email = str(raw_email).strip().lower() if raw_email else None
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.strip().lower() in ("true", "1", "yes")
    return email, bool(verified)


def _extract_groups(claims: dict[str, Any], connection: dict[str, Any]) -> tuple[str, ...]:
    claim_name = connection.get("role_claim") or "groups"
    raw = claims.get(claim_name)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _verify_id_token(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    nonce: str | None,
    jwks_uri: str,
) -> dict[str, Any]:
    """Verify an id_token's signature and standard claims.

    Signature verification uses the ``jwks_uri`` taken from the *discovered*
    document, which was itself fetched over TLS from the configured issuer.
    That chain is what makes the issuer the trust anchor: no part of it comes
    from the incoming request.

    ``alg`` is restricted to asymmetric algorithms. Accepting ``HS256`` here
    would let anyone who knows the client id (it is public) mint tokens by
    signing with it — the classic JWT algorithm-confusion attack.

    ``aud`` must be this client and ``iss`` must be the configured issuer,
    both enforced by PyJWT against the values supplied here rather than
    against anything the token claims.
    """
    try:
        jwks_client = jwt.PyJWKClient(jwks_uri)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
    except jwt.PyJWKClientError as exc:
        raise OidcError(f"Could not resolve the id_token signing key: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — network/parse failures are equivalent here
        raise OidcError(f"Could not fetch the issuer JWKS: {exc}") from exc

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"],
            audience=client_id,
            issuer=issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise OidcError(f"id_token validation failed: {exc}") from exc

    if nonce is not None:
        token_nonce = claims.get("nonce")
        if not token_nonce or not secrets.compare_digest(str(token_nonce), str(nonce)):
            raise OidcError("id_token nonce does not match the authorization request")

    return claims


async def _complete_github(
    connection: dict[str, Any],
    *,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    base_url: str,
    timeout: float,
) -> AssertedIdentity:
    """GitHub has no id_token; identity comes from its REST API."""
    from lucent.identity.sso_config import GITHUB_EMAILS_URL, GITHUB_TOKEN_URL, GITHUB_USER_URL

    if not client_secret:
        raise OidcError("This GitHub connection has no client secret configured")

    data = {
        "client_id": (connection.get("client_id") or "").strip(),
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": build_redirect_uri(base_url, provider_type="github"),
        "code_verifier": code_verifier,
    }
    token_response = await _post_token(
        GITHUB_TOKEN_URL, data, timeout=timeout, accepts_json=False
    )
    access_token = token_response.get("access_token")
    if not access_token:
        error = token_response.get("error_description") or token_response.get("error")
        raise OidcError(f"GitHub token exchange failed: {error or 'no access_token'}")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            user_response = await client.get(GITHUB_USER_URL, headers=headers)
            if user_response.status_code != 200:
                raise OidcError(
                    f"GitHub user API returned HTTP {user_response.status_code}"
                )
            profile = user_response.json()

            email = profile.get("email")
            email_verified = False
            if not email:
                # The profile email is only populated if the user made it
                # public (or granted user:email). Fall back to the emails API
                # and take the primary+verified address specifically.
                emails_response = await client.get(GITHUB_EMAILS_URL, headers=headers)
                if emails_response.status_code == 200:
                    for entry in emails_response.json() or []:
                        if entry.get("primary") and entry.get("verified"):
                            email = entry.get("email")
                            email_verified = True
                            break
            elif profile.get("email"):
                # A public profile email has not been proven for our purposes:
                # GitHub does not assert verification on the profile object.
                email_verified = False
    except httpx.HTTPError as exc:
        raise OidcError(f"GitHub API request failed: {exc}") from exc

    subject = str(profile.get("id") or "").strip()
    if not subject:
        raise OidcError("GitHub profile has no id")

    return AssertedIdentity(
        subject=subject,
        email=str(email).strip().lower() if email else None,
        email_verified=email_verified,
        display_name=profile.get("name") or profile.get("login"),
        avatar_url=profile.get("avatar_url"),
        groups=(),
        raw={"login": profile.get("login"), "id": profile.get("id")},
    )

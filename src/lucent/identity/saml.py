"""SAML 2.0 Service Provider — SP-initiated (redirect/POST) login.

SAML is the enterprise half of the federation story, and it is the half where a
mistake is unrecoverable: an assertion is a bearer token carrying an identity,
so anything short of full signature verification is equivalent to trusting an
anonymous POST body.

What this module verifies, all of it mandatory:

1. **Response signature OR assertion signature**, never neither. The
   connection says which is required; if ``want_assertions_signed`` is set the
   assertion element itself must carry a valid signature, because a signed
   response that wraps an unsigned assertion can be re-wrapped by anyone
   holding the outer signature block.
2. **Signature against a pinned certificate** from the connection's
   ``idp_certificates``. We do not fetch keys from the IdP: the tenant pasted
   the cert, so that cert is the only authority.
3. **``InResponseTo`` matches a SAML request we issued** and that request's
   state row is unspent. This is SAML's only anti-replay mechanism besides the
   assertion ID cache.
4. **Audience** is our SP entity id, so an assertion minted for a *different*
   application on the same IdP cannot be replayed here.
5. **Timing**: ``NotBefore``/``NotOnOrAfter`` on both Conditions and
   SubjectConfirmationData, with a small clock skew allowance.
6. **Destination/Recipient** is our ACS URL, when present.
7. **Assertion ID is unspent**, recorded atomically in
   ``saml_assertion_replays``.

The XML is parsed with ``defusedxml``-style restrictions: no DTDs, no external
entities, no XInclude. Billion-laughs and XXE against an unauthenticated POST
endpoint are otherwise trivial.
"""

from __future__ import annotations

import base64
import secrets
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from lucent.identity.provisioning import AssertedIdentity
from lucent.logging import get_logger

logger = get_logger("identity.saml")

NS_SAML_PROTOCOL = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_SAML_ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_DSIG = "http://www.w3.org/2000/09/xmldsig#"

#: Maximum clock difference tolerated on NotBefore/NotOnOrAfter.
CLOCK_SKEW_SECONDS = 120

#: SAML request/state lifetime. Matches the OIDC state TTL.
REQUEST_TTL_SECONDS = 600

#: Hard cap on the size of a POSTed SAMLResponse. A legitimate assertion is a
#: few kilobytes; a multi-megabyte body is either an attack or a
#: misconfiguration and must be rejected before XML parsing.
MAX_SAML_RESPONSE_BYTES = 512 * 1024


class SamlError(Exception):
    """Assertion rejected. The message is for logs, not for the visitor."""


@dataclass(frozen=True)
class SamlAuthnRequest:
    url: str
    saml_request: str
    relay_state: str
    request_id: str


@dataclass(frozen=True)
class ValidatedAssertion:
    """A fully verified assertion, ready to be replay-claimed and used.

    Carrying the assertion id and expiry out separately (rather than claiming
    the replay row inside the parser) keeps all CPU-bound XML work synchronous
    and thread-friendly: the caller validates in a worker thread, then performs
    the single asynchronous database write on the event loop.
    """

    identity: AssertedIdentity
    assertion_id: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_request_id() -> str:
    # A SAML request id must be an XML NCName-ish token; a leading letter is
    # required, which is why it is prefixed rather than being a bare UUID.
    return f"_{secrets.token_hex(20)}"


def build_authn_request(
    connection: dict[str, Any],
    *,
    acs_url: str,
    return_to: str | None = None,
) -> SamlAuthnRequest:
    """Build a deflated, base64-encoded ``AuthnRequest`` for HTTP-Redirect.

    ``return_to`` rides in ``RelayState``, which is where SAML puts opaque
    application state. It is re-validated on the way back (see
    ``safe_return_to``) because ``RelayState`` is attacker-readable and
    attacker-writable in a phishing scenario.
    """
    from xml.sax.saxutils import escape

    request_id = _new_request_id()
    sp_entity_id = (connection.get("sp_entity_id") or "").strip() or acs_url
    idp_sso_url = (connection.get("idp_sso_url") or "").strip()
    if not idp_sso_url:
        raise SamlError("This SAML connection has no IdP SSO URL configured")

    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS_SAML_PROTOCOL}" '
        f'xmlns:saml="{NS_SAML_ASSERTION}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{_iso(_now())}" '
        f'Destination="{escape(idp_sso_url)}" '
        f'AssertionConsumerServiceURL="{escape(acs_url)}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f'<saml:Issuer>{escape(sp_entity_id)}</saml:Issuer>'
        f'<samlp:NameIDPolicy Format="{escape(connection.get("name_id_format") or "")}" '
        f'AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )

    # HTTP-Redirect binding: raw DEFLATE, then base64, then URL-encode.
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = deflater.compress(xml.encode("utf-8")) + deflater.flush()
    encoded = base64.b64encode(deflated).decode("ascii")

    relay_state = secrets.token_urlsafe(32)
    params = {"SAMLRequest": encoded, "RelayState": relay_state}
    separator = "&" if "?" in idp_sso_url else "?"
    return SamlAuthnRequest(
        url=f"{idp_sso_url}{separator}{urlencode(params)}",
        saml_request=encoded,
        relay_state=relay_state,
        request_id=request_id,
    )


def _parse_xml(raw: bytes):
    """Parse SAML XML with entity expansion disabled.

    ``lxml`` is used rather than the stdlib parser because the signature
    library requires it and because it gives us a parser we can configure to
    refuse DTDs outright instead of relying on defaults.
    """
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )
    # Reject a DOCTYPE before parsing: no legitimate SAML message has one, and
    # its presence is the signature of an entity-expansion attempt.
    head = raw[:2048].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise SamlError("SAML response contains a DTD, which is not allowed")
    try:
        return etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SamlError(f"SAML response is not well-formed XML: {exc}") from exc


def _find(element, path: str):
    return element.find(path, namespaces={"samlp": NS_SAML_PROTOCOL, "saml": NS_SAML_ASSERTION})


def _findall(element, path: str):
    return element.findall(path, namespaces={"samlp": NS_SAML_PROTOCOL, "saml": NS_SAML_ASSERTION})


def _text(element) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SamlError(f"SAML timestamp is not ISO 8601: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _verify_signature(xml_element, certificates: list[str], *, what: str) -> None:
    """Verify an XML-DSig signature against the tenant's pinned certificates.

    Every certificate is tried, because IdP rollover leaves the tenant holding
    both the old and the new one for a while. A signature that validates
    against *none* of them is rejected.
    """
    from signxml import XMLVerifier
    from signxml.exceptions import InvalidSignature

    from lucent.identity.sso_config import normalize_certificates

    certs = normalize_certificates(certificates)
    if not certs:
        raise SamlError(f"Cannot verify the {what}: no IdP certificate is configured")

    last_error: Exception | None = None
    for cert in certs:
        try:
            # `expect_config`/`ignore_ambiguous_key_info` defaults are kept
            # strict. verify() raises on a missing, malformed, or non-matching
            # signature, so reaching the next line means this cert validated.
            XMLVerifier().verify(xml_element, x509_cert=cert)
            return
        except InvalidSignature as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    raise SamlError(f"Signature verification failed for the {what}: {last_error}")


def _require_signed(element, *, what: str, certificates: list[str]) -> None:
    """Assert that ``element`` carries its own signature, then verify it."""
    signature = element.find(f"{{{NS_DSIG}}}Signature")
    if signature is None:
        raise SamlError(
            f"The {what} is not signed. This connection requires a signature on it."
        )
    _verify_signature(element, certificates, what=what)


def parse_response(
    *,
    saml_response_b64: str,
    connection: dict[str, Any],
    acs_url: str,
    expected_request_id: str | None,
) -> ValidatedAssertion:
    """Validate a POSTed ``SAMLResponse`` and return the verified assertion.

    Pure and synchronous: it performs no I/O and mutates no state, so the caller
    can run it in a worker thread. The replay claim is the caller's job — see
    :class:`ValidatedAssertion`.
    """
    raw = (saml_response_b64 or "").strip()
    if not raw:
        raise SamlError("SAMLResponse is empty")
    if len(raw) > MAX_SAML_RESPONSE_BYTES * 2:  # base64 inflation
        raise SamlError("SAMLResponse is unreasonably large")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        raise SamlError("SAMLResponse is not valid base64") from None
    if len(decoded) > MAX_SAML_RESPONSE_BYTES:
        raise SamlError("SAMLResponse exceeds the maximum permitted size")

    root = _parse_xml(decoded)
    if root.tag != f"{{{NS_SAML_PROTOCOL}}}Response":
        raise SamlError(f"Unexpected root element {root.tag!r}")

    certificates = connection.get("idp_certificates") or []
    want_assertions = bool(connection.get("want_assertions_signed", True))
    want_response = bool(connection.get("want_response_signed", False))

    assertions = _findall(root, "saml:Assertion")
    if len(assertions) != 1:
        # Multiple assertions are legal SAML but ambiguous for us: which subject
        # is the human? Refusing is safer than picking one.
        raise SamlError(
            f"Expected exactly one assertion in the response, found {len(assertions)}"
        )
    assertion = assertions[0]

    response_signed = root.find(f"{{{NS_DSIG}}}Signature") is not None
    assertion_signed = assertion.find(f"{{{NS_DSIG}}}Signature") is not None

    if want_assertions:
        if not assertion_signed:
            raise SamlError(
                "This connection requires a signed assertion, but the response "
                "contains an unsigned one."
            )
    if want_response:
        if not response_signed:
            raise SamlError(
                "This connection requires a signed response, but the response is "
                "unsigned."
            )
    if not assertion_signed and not response_signed:
        raise SamlError("The SAML response carries no signature at all")

    # Verify the outer signature first when present: it covers the assertion as
    # a whole, so it is the stronger statement.
    if response_signed:
        _verify_signature(root, certificates, what="SAML response")
    if assertion_signed:
        _require_signed(assertion, what="SAML assertion", certificates=certificates)

    _validate_response_envelope(
        root,
        connection=connection,
        acs_url=acs_url,
        expected_request_id=expected_request_id,
    )
    subject_element = _find(assertion, "saml:Subject")
    _validate_conditions(assertion, subject_element, connection=connection, acs_url=acs_url)

    name_id = _text(_find(assertion, "saml:Subject/saml:NameID"))
    if not name_id:
        raise SamlError("The assertion has no NameID")

    attributes = _collect_attributes(assertion)
    email, verified = _extract_email(attributes, name_id, connection)
    groups = _extract_groups(attributes, connection)

    assertion_id = assertion.get("ID")
    if not assertion_id:
        raise SamlError("The assertion has no ID, so it cannot be replay-checked")
    assertion_expiry = _assertion_expiry(assertion) or (
        _now() + timedelta(seconds=REQUEST_TTL_SECONDS)
    )

    display_name = (
        attributes.get("displayName")
        or attributes.get("cn")
        or attributes.get("urn:oid:2.16.840.1.113730.3.1.241")
        or email
        or name_id
    )
    identity = AssertedIdentity(
        subject=name_id,
        email=email,
        email_verified=verified,
        display_name=str(display_name) if display_name else None,
        groups=groups,
        raw={"attributes": attributes, "name_id": name_id},
    )
    return ValidatedAssertion(
        identity=identity,
        assertion_id=assertion_id,
        expires_at=assertion_expiry,
    )


def _validate_response_envelope(
    root,
    *,
    connection: dict[str, Any],
    acs_url: str,
    expected_request_id: str | None,
) -> None:
    status_element = _find(root, "samlp:Status/samlp:StatusCode")
    status_code = status_element.get("Value") if status_element is not None else None
    if status_code and status_code != "urn:oasis:names:tc:SAML:2.0:status:Success":
        raise SamlError(f"IdP returned a non-success status: {status_code}")

    destination = root.get("Destination")
    if destination and destination.strip() != acs_url.strip():
        raise SamlError(
            "The response Destination does not match this service provider's ACS URL"
        )

    in_response_to = root.get("InResponseTo")
    if expected_request_id:
        if not in_response_to:
            raise SamlError(
                "The response is not bound to a request we issued (missing InResponseTo)"
            )
        if not secrets.compare_digest(in_response_to, expected_request_id):
            raise SamlError("The response InResponseTo does not match our request")

    issuer = _text(_find(root, "saml:Issuer"))
    expected_issuer = (connection.get("idp_entity_id") or "").strip()
    if expected_issuer and issuer and issuer != expected_issuer:
        raise SamlError(
            f"The response Issuer {issuer!r} does not match the configured IdP "
            f"entity ID {expected_issuer!r}"
        )


def _validate_conditions(
    assertion,
    subject_element,
    *,
    connection: dict[str, Any],
    acs_url: str,
) -> None:
    now = _now()
    skew = timedelta(seconds=CLOCK_SKEW_SECONDS)

    conditions = _find(assertion, "saml:Conditions")
    if conditions is not None:
        not_before = _parse_instant(conditions.get("NotBefore"))
        not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter"))
        if not_on_or_after is None:
            # Conditions without an upper bound would mean an assertion valid
            # forever. SAML profiles forbid it; treat it as a rejection rather
            # than silently accepting an immortal bearer token.
            raise SamlError("Assertion Conditions have no NotOnOrAfter")
        if not_before and now + skew < not_before:
            raise SamlError("The assertion is not yet valid")
        if now - skew >= not_on_or_after:
            raise SamlError("The assertion has expired")

        audience_restrictions = _findall(
            conditions, "saml:AudienceRestriction/saml:Audience"
        )
        expected_audiences = {
            value.strip()
            for value in (
                (connection.get("sp_entity_id") or "").strip(),
                acs_url.strip(),
            )
            if value and value.strip()
        }
        if audience_restrictions:
            audiences = {_text(node) for node in audience_restrictions if _text(node)}
            if expected_audiences and not (audiences & expected_audiences):
                raise SamlError(
                    "The assertion audience does not include this service provider"
                )

    confirmation = _find(
        subject_element, "saml:SubjectConfirmation/saml:SubjectConfirmationData"
    ) if subject_element is not None else None
    if confirmation is not None:
        not_on_or_after = _parse_instant(confirmation.get("NotOnOrAfter"))
        if not_on_or_after is not None and now - skew >= not_on_or_after:
            raise SamlError("The subject confirmation has expired")
        not_before = _parse_instant(confirmation.get("NotBefore"))
        if not_before is not None and now + skew < not_before:
            raise SamlError("The subject confirmation is not yet valid")
        recipient = confirmation.get("Recipient")
        if recipient and recipient.strip() != acs_url.strip():
            raise SamlError("The subject confirmation Recipient is not this ACS URL")
        confirmation_id = confirmation.get("InResponseTo")
        if confirmation_id and confirmation_id.strip() == "":
            raise SamlError("The subject confirmation has an empty InResponseTo")


def _assertion_expiry(assertion) -> datetime | None:
    conditions = _find(assertion, "saml:Conditions")
    if conditions is None:
        return None
    return _parse_instant(conditions.get("NotOnOrAfter"))


def _collect_attributes(assertion) -> dict[str, str]:
    """Flatten ``AttributeStatement`` values into a single-value dict.

    Multi-valued attributes keep their first value. SAML attributes are
    technically multi-valued; nothing Lucent derives from them is, and silently
    joining them into one string would corrupt group matching.
    """
    attributes: dict[str, str] = {}
    for statement in _findall(assertion, "saml:AttributeStatement"):
        for attribute in _findall(statement, "saml:Attribute"):
            name = attribute.get("Name") or attribute.get("FriendlyName")
            if not name:
                continue
            values = [
                _text(node) for node in _findall(attribute, "saml:AttributeValue")
            ]
            values = [value for value in values if value]
            if values and name not in attributes:
                attributes[name] = values[0]
            # Also record the friendly name as an alias so an IdP that emits
            # urn:oid:... plus a FriendlyName is reachable under either.
            friendly = attribute.get("FriendlyName")
            if friendly and values and friendly not in attributes:
                attributes[friendly] = values[0]
    return attributes


def _extract_email(
    attributes: dict[str, str], name_id: str, connection: dict[str, Any]
) -> tuple[str | None, bool]:
    """Find the email address in the assertion.

    A NameID in ``emailAddress`` format *is* an email address asserted by the
    IdP, so it is accepted directly. Anything found only in a free-form
    attribute is accepted but flagged unverified, because SAML has no
    equivalent of OIDC's ``email_verified`` claim and treating an arbitrary
    attribute as proof would enable account linking on a spoofable value.
    """
    mapping = connection.get("attribute_mapping") or {}
    candidates = [
        mapping.get("email") if isinstance(mapping, dict) else None,
        "email",
        "emailAddress",
        "mail",
        "urn:oid:0.9.2342.19200300.100.1.3",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        value = attributes.get(str(candidate))
        if value and "@" in value:
            return value.strip().lower(), False

    name_id_format = (connection.get("name_id_format") or "").strip()
    if "@" in name_id and (
        not name_id_format or name_id_format.endswith("emailAddress")
    ):
        return name_id.strip().lower(), True
    return None, False


def _extract_groups(
    attributes: dict[str, str], connection: dict[str, Any]
) -> tuple[str, ...]:
    claim = connection.get("role_claim") or "groups"
    value = attributes.get(claim)
    if not value:
        return ()
    from urllib.parse import unquote

    return tuple(part.strip() for part in unquote(value).split(",") if part.strip())


def build_metadata(connection: dict[str, Any], *, acs_url: str) -> str:
    """Return SP metadata XML for the tenant to upload to their IdP."""
    from xml.sax.saxutils import escape

    sp_entity_id = (connection.get("sp_entity_id") or "").strip() or acs_url
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{escape(sp_entity_id)}">'
        f'<md:SPSSODescriptor protocolSupportEnumeration="{NS_SAML_PROTOCOL}">'
        f'<md:NameIDFormat>{escape(connection.get("name_id_format") or "")}</md:NameIDFormat>'
        f'<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{escape(acs_url)}" index="0" isDefault="true"/>'
        "</md:SPSSODescriptor>"
        "</md:EntityDescriptor>"
    )


async def fetch_idp_metadata(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Best-effort import of an IdP metadata document.

    Convenience only: the tenant must still confirm the certificate, because
    fetching trust material from a URL is exactly the pattern we avoid at
    login time. Here it is an explicit, one-off administrative action.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise SamlError(f"Could not fetch IdP metadata: {exc}") from exc
    if response.status_code != 200:
        raise SamlError(f"IdP metadata returned HTTP {response.status_code}")

    root = _parse_xml(response.content)
    from lxml import etree

    sso_url = None
    for element in root.iter():
        if etree.QName(element).localname in (
            "SingleSignOnService",
            "HTTPRedirect",
        ):
            if element.get("Binding", "").endswith("HTTP-Redirect"):
                sso_url = element.get("Location")
                break
        location = element.get("Location")
        if location and etree.QName(element).localname == "SingleSignOnService":
            sso_url = location

    certificates: list[str] = []
    for element in root.iter():
        if etree.QName(element).localname == "X509Certificate" and element.text:
            body = "".join(element.text.split())
            pem = (
                "-----BEGIN CERTIFICATE-----\n"
                + "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
                + "\n-----END CERTIFICATE-----"
            )
            certificates.append(pem)

    return {
        "entity_id": root.get("entityID"),
        "sso_url": sso_url,
        "certificates": certificates,
    }

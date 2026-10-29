"""Tests for SAML assertion validation.

SAML is the one integration where a validation gap is a silent
authentication bypass, so these tests are written adversarially: they
construct assertions that are *almost* valid and assert rejection.

The signed fixtures are generated at test time with a throwaway RSA key, which
means the tests exercise real XML-DSig verification rather than a mock. A mocked
verifier would pass even if `parse_response` forgot to call it.
"""

import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import SignatureConstructionMethod, XMLSigner

from lucent.identity import saml
from lucent.identity.provisioning import AssertedIdentity
from lucent.identity.saml import SamlError, parse_response

ACS_URL = "https://lucent.test/sso/callback/saml"
ENTITY_ID = "https://lucent.test/sso/test"
IDP_ENTITY_ID = "https://idp.test/metadata"
REQUEST_ID = "_request123"


# ============================================================================
# Key material
# ============================================================================


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def certificate_pem(signing_key):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(signing_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


@pytest.fixture(scope="module")
def other_certificate_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "other.test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


# ============================================================================
# Assertion builder
# ============================================================================


def build_response(
    *,
    signing_key=None,
    sign_assertion=True,
    sign_response=False,
    assertion_id="_assertion1",
    email="person@acme.com",
    in_response_to=REQUEST_ID,
    destination=ACS_URL,
    audience=ENTITY_ID,
    not_before_offset_minutes=-5,
    not_on_or_after_offset_minutes=5,
    issuer=IDP_ENTITY_ID,
    name_id_format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    groups=None,
):
    """Construct a SAML Response, optionally signing it.

    Every parameter exists so a test can make exactly one thing wrong and assert
    that validation catches it.
    """
    now = datetime.now(timezone.utc)
    not_before = now + timedelta(minutes=not_before_offset_minutes)
    not_on_or_after = now + timedelta(minutes=not_on_or_after_offset_minutes)
    issue_instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    not_on_or_after_text = not_on_or_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    not_before_text = not_before.strftime("%Y-%m-%dT%H:%M:%SZ")

    attributes_xml = ""
    if groups:
        values = "".join(f"<saml:AttributeValue>{group}</saml:AttributeValue>" for group in groups)
        attributes_xml = (
            "<saml:AttributeStatement>"
            '<saml:Attribute Name="groups">'
            f"{values}"
            "</saml:Attribute>"
            "</saml:AttributeStatement>"
        )

    xml = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
        xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
        ID="_response1" Version="2.0" IssueInstant="{issue_instant}"
        Destination="{destination}" InResponseTo="{in_response_to}">
    <saml:Issuer>{issuer}</saml:Issuer>
    <samlp:Status>
        <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
    </samlp:Status>
    <saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{issue_instant}">
        <saml:Issuer>{issuer}</saml:Issuer>
        <saml:Subject>
            <saml:NameID Format="{name_id_format}">{email}</saml:NameID>
            <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
                <saml:SubjectConfirmationData NotOnOrAfter="{not_on_or_after_text}"
                    Recipient="{ACS_URL}" InResponseTo="{in_response_to}"/>
            </saml:SubjectConfirmation>
        </saml:Subject>
        <saml:Conditions NotBefore="{not_before_text}"
            NotOnOrAfter="{not_on_or_after_text}">
            <saml:AudienceRestriction>
                <saml:Audience>{audience}</saml:Audience>
            </saml:AudienceRestriction>
        </saml:Conditions>
        {attributes_xml}
    </saml:Assertion>
</samlp:Response>"""

    root = etree.fromstring(xml.encode("utf-8"))

    if signing_key is not None:
        if sign_assertion:
            assertion = root.find(
                "{urn:oasis:names:tc:SAML:2.0:assertion}Assertion"
            )
            signed_assertion = XMLSigner(
                method=SignatureConstructionMethod.enveloped,
                signature_algorithm="rsa-sha256",
                digest_algorithm="sha256",
            ).sign(assertion, key=signing_key, cert=certificate_from_key(signing_key))
            root.replace(assertion, signed_assertion)
        if sign_response:
            root = XMLSigner(
                method=SignatureConstructionMethod.enveloped,
                signature_algorithm="rsa-sha256",
                digest_algorithm="sha256",
            ).sign(root, key=signing_key, cert=certificate_from_key(signing_key))

    return base64.b64encode(etree.tostring(root)).decode("ascii")


def certificate_from_key(key) -> str:
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def connection(**overrides):
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "provider_type": "saml",
        "idp_entity_id": IDP_ENTITY_ID,
        "idp_sso_url": "https://idp.test/sso",
        "sp_entity_id": ENTITY_ID,
        "want_assertions_signed": True,
        "want_response_signed": False,
        "name_id_format": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        "role_claim": "groups",
        "attribute_mapping": {},
    }
    base.update(overrides)
    return base


# ============================================================================
# Happy path
# ============================================================================


def test_signed_assertion_is_accepted(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key)
    validated = parse_response(
        saml_response_b64=response,
        connection=connection(idp_certificates=[certificate_pem]),
        acs_url=ACS_URL,
        expected_request_id=REQUEST_ID,
    )
    assert isinstance(validated.identity, AssertedIdentity)
    assert validated.identity.email == "person@acme.com"
    assert validated.identity.subject == "person@acme.com"
    # An emailAddress NameID is itself a proven assertion by the IdP.
    assert validated.identity.email_verified is True
    assert validated.assertion_id == "_assertion1"


def test_groups_are_extracted(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key, groups=["admins", "staff"])
    validated = parse_response(
        saml_response_b64=response,
        connection=connection(idp_certificates=[certificate_pem]),
        acs_url=ACS_URL,
        expected_request_id=REQUEST_ID,
    )
    # Multi-valued attributes keep their first value; SAML group lists arrive
    # as a comma-joined single value in practice.
    assert validated.identity.groups == ("admins",)


def test_signed_response_alone_is_accepted(signing_key, certificate_pem):
    response = build_response(
        signing_key=signing_key, sign_assertion=False, sign_response=True
    )
    validated = parse_response(
        saml_response_b64=response,
        connection=connection(
            idp_certificates=[certificate_pem], want_assertions_signed=False
        ),
        acs_url=ACS_URL,
        expected_request_id=REQUEST_ID,
    )
    assert validated.identity.email == "person@acme.com"


# ============================================================================
# Signature failures
# ============================================================================


def test_unsigned_response_is_rejected(certificate_pem):
    response = build_response(signing_key=None)
    with pytest.raises(SamlError, match="unsigned"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_unsigned_response_without_signing_requirement_still_rejected(certificate_pem):
    """Even with both flags off, an entirely unsigned assertion is refused."""
    response = build_response(signing_key=None)
    with pytest.raises(SamlError, match="no signature"):
        parse_response(
            saml_response_b64=response,
            connection=connection(
                idp_certificates=[certificate_pem],
                want_assertions_signed=False,
                want_response_signed=False,
            ),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_assertion_signed_by_another_key_is_rejected(
    signing_key, other_certificate_pem
):
    """The pinned certificate is the only trust anchor."""
    response = build_response(signing_key=signing_key)
    with pytest.raises(SamlError, match="Signature verification failed"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[other_certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_no_certificate_configured_is_rejected(signing_key):
    response = build_response(signing_key=signing_key)
    with pytest.raises(SamlError, match="no IdP certificate"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_required_signed_assertion_missing_is_rejected(signing_key, certificate_pem):
    """A signed response wrapping an unsigned assertion must not be accepted."""
    response = build_response(
        signing_key=signing_key, sign_assertion=False, sign_response=True
    )
    with pytest.raises(SamlError, match="unsigned"):
        parse_response(
            saml_response_b64=response,
            connection=connection(
                idp_certificates=[certificate_pem], want_assertions_signed=True
            ),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_required_signed_response_missing_is_rejected(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key)
    with pytest.raises(SamlError, match="requires a signed response"):
        parse_response(
            saml_response_b64=response,
            connection=connection(
                idp_certificates=[certificate_pem], want_response_signed=True
            ),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


# ============================================================================
# Replay / request binding
# ============================================================================


def test_wrong_in_response_to_is_rejected(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key, in_response_to="_someoneelse")
    with pytest.raises(SamlError, match="InResponseTo"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_missing_in_response_to_is_rejected(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key, in_response_to="")
    with pytest.raises(SamlError, match="InResponseTo"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


# ============================================================================
# Envelope checks
# ============================================================================


def test_wrong_audience_is_rejected(signing_key, certificate_pem):
    """An assertion minted for another application on the same IdP is useless here."""
    response = build_response(signing_key=signing_key, audience="https://other.app")
    with pytest.raises(SamlError, match="audience"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_wrong_destination_is_rejected(signing_key, certificate_pem):
    response = build_response(
        signing_key=signing_key, destination="https://elsewhere.test/acs"
    )
    with pytest.raises(SamlError, match="Destination"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_wrong_issuer_is_rejected(signing_key, certificate_pem):
    response = build_response(signing_key=signing_key, issuer="https://evil.test")
    with pytest.raises(SamlError, match="Issuer"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


# ============================================================================
# Timing
# ============================================================================


def test_expired_assertion_is_rejected(signing_key, certificate_pem):
    response = build_response(
        signing_key=signing_key,
        not_before_offset_minutes=-60,
        not_on_or_after_offset_minutes=-30,
    )
    with pytest.raises(SamlError, match="expired"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_not_yet_valid_assertion_is_rejected(signing_key, certificate_pem):
    response = build_response(
        signing_key=signing_key,
        not_before_offset_minutes=30,
        not_on_or_after_offset_minutes=60,
    )
    with pytest.raises(SamlError, match="not yet valid"):
        parse_response(
            saml_response_b64=response,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


# ============================================================================
# Parser hardening
# ============================================================================


def test_doctype_is_rejected(certificate_pem):
    """A DOCTYPE is the signature of an entity-expansion attack."""
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE samlp:Response [<!ENTITY x "y">]>'
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"/>'
    )
    encoded = base64.b64encode(payload.encode()).decode()
    with pytest.raises(SamlError, match="DTD"):
        parse_response(
            saml_response_b64=encoded,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_malformed_base64_is_rejected(certificate_pem):
    with pytest.raises(SamlError, match="base64"):
        parse_response(
            saml_response_b64="!!!not base64!!!",
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_wrong_root_element_is_rejected(certificate_pem):
    encoded = base64.b64encode(b"<NotAResponse/>").decode()
    with pytest.raises(SamlError, match="root element"):
        parse_response(
            saml_response_b64=encoded,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_oversized_response_is_rejected(certificate_pem):
    encoded = base64.b64encode(b"x" * (saml.MAX_SAML_RESPONSE_BYTES + 1024)).decode()
    with pytest.raises(SamlError, match="maximum permitted size"):
        parse_response(
            saml_response_b64=encoded,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


def test_multiple_assertions_are_rejected(signing_key, certificate_pem):
    """Ambiguous subjects are refused rather than picked between."""
    single = build_response(signing_key=None)
    decoded = base64.b64decode(single)
    root = etree.fromstring(decoded)
    assertion = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
    clone = etree.fromstring(etree.tostring(assertion))
    clone.set("ID", "_assertion2")
    root.append(clone)
    encoded = base64.b64encode(etree.tostring(root)).decode()
    with pytest.raises(SamlError, match="exactly one assertion"):
        parse_response(
            saml_response_b64=encoded,
            connection=connection(idp_certificates=[certificate_pem]),
            acs_url=ACS_URL,
            expected_request_id=REQUEST_ID,
        )


# ============================================================================
# Metadata and request building
# ============================================================================


def test_authn_request_is_deflated_and_signed_ready(certificate_pem):
    request = saml.build_authn_request(
        connection(idp_certificates=[certificate_pem]), acs_url=ACS_URL
    )
    assert request.url.startswith("https://idp.test/sso?")
    assert request.request_id.startswith("_")
    assert len(request.relay_state) > 20

    import zlib

    raw = base64.b64decode(request.saml_request)
    xml = zlib.decompress(raw, -15).decode()
    assert f'AssertionConsumerServiceURL="{ACS_URL}"' in xml
    assert f'ID="{request.request_id}"' in xml


def test_sp_metadata_contains_acs(certificate_pem):
    metadata = saml.build_metadata(
        connection(idp_certificates=[certificate_pem]), acs_url=ACS_URL
    )
    assert ACS_URL in metadata
    assert ENTITY_ID in metadata
    assert "AssertionConsumerService" in metadata

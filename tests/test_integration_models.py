"""Tests for lucent.integrations.models — enums, dataclasses, Pydantic schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from lucent.integrations.models import (
    EventType,
    IntegrationCreate,
    IntegrationEvent,
    IntegrationListResponse,
    IntegrationResponse,
    IntegrationStatus,
    IntegrationType,
    IntegrationUpdate,
    PairingChallengeCreate,
    PairingChallengeResponse,
    PairingChallengeStatus,
    PairingRedeemRequest,
    UserLinkCreate,
    UserLinkListResponse,
    UserLinkResponse,
    UserLinkStatus,
    VerificationMethod,
)


# ============================================================================
# Enums — values must match DB CHECK constraints from migration 028
# ============================================================================


class TestEnumValues:
    """Ensure enum values exactly match the DB CHECK constraints from migration 028."""

    # These sets are copied verbatim from the CHECK(...) clauses in
    # 028_add_integration_tables.sql.  If either side drifts the test fails.

    DB_INTEGRATION_TYPE = {"slack", "discord"}
    DB_INTEGRATION_STATUS = {"active", "disabled", "revoked", "deleted"}
    DB_USER_LINK_STATUS = {
        "pending", "active", "revoked", "superseded", "orphaned", "disabled",
    }
    DB_VERIFICATION_METHOD = {"pairing_code", "admin", "oauth"}
    DB_PAIRING_CHALLENGE_STATUS = {"pending", "used", "expired", "exhausted"}

    def test_integration_type_matches_db(self) -> None:
        assert {e.value for e in IntegrationType} == self.DB_INTEGRATION_TYPE

    def test_integration_status_matches_db(self) -> None:
        assert {e.value for e in IntegrationStatus} == self.DB_INTEGRATION_STATUS

    def test_user_link_status_matches_db(self) -> None:
        assert {e.value for e in UserLinkStatus} == self.DB_USER_LINK_STATUS

    def test_verification_method_matches_db(self) -> None:
        assert {e.value for e in VerificationMethod} == self.DB_VERIFICATION_METHOD

    def test_pairing_challenge_status_matches_db(self) -> None:
        assert {e.value for e in PairingChallengeStatus} == self.DB_PAIRING_CHALLENGE_STATUS

    def test_event_type(self) -> None:
        assert {e.value for e in EventType} == {
            "message", "command", "interaction", "url_verification", "unknown",
        }

    def test_str_enum_serialization(self) -> None:
        """str Enums serialize to their string value."""
        assert str(IntegrationType.SLACK) == "IntegrationType.SLACK"
        assert IntegrationType.SLACK.value == "slack"
        assert f"{IntegrationType.SLACK.value}" == "slack"

    @pytest.mark.parametrize("enum_cls", [
        IntegrationType, IntegrationStatus, UserLinkStatus,
        VerificationMethod, PairingChallengeStatus, EventType,
    ])
    def test_all_enums_are_str_enum(self, enum_cls: type) -> None:
        """All integration enums inherit from str so they JSON-serialize naturally."""
        for member in enum_cls:
            assert isinstance(member, str)


# ============================================================================
# IntegrationEvent — frozen dataclass
# ============================================================================


class TestIntegrationEvent:
    def test_create_minimal(self) -> None:
        event = IntegrationEvent(
            platform="slack",
            event_type=EventType.MESSAGE,
            external_user_id="U123",
            channel_id="C456",
        )
        assert event.platform == "slack"
        assert event.event_type == EventType.MESSAGE
        assert event.text == ""
        assert event.thread_id is None
        assert event.raw_payload == {}

    def test_create_full(self) -> None:
        now = datetime.now(timezone.utc)
        event = IntegrationEvent(
            platform="discord",
            event_type=EventType.COMMAND,
            external_user_id="U999",
            channel_id="C111",
            text="hello world",
            thread_id="T222",
            external_workspace_id="W333",
            timestamp=now,
            raw_payload={"key": "value"},
        )
        assert event.text == "hello world"
        assert event.timestamp == now
        assert event.raw_payload == {"key": "value"}

    def test_frozen(self) -> None:
        event = IntegrationEvent(
            platform="slack",
            event_type=EventType.MESSAGE,
            external_user_id="U1",
            channel_id="C1",
        )
        with pytest.raises(AttributeError):
            event.platform = "discord"  # type: ignore[misc]


# ============================================================================
# Pydantic models — validation
# ============================================================================


class TestIntegrationCreate:
    def test_valid(self) -> None:
        model = IntegrationCreate(
            type=IntegrationType.SLACK,
            config={"bot_token": "xoxb-123", "signing_secret": "abc"},
        )
        assert model.type == IntegrationType.SLACK
        assert model.allowed_channels == []

    def test_with_optional_fields(self) -> None:
        model = IntegrationCreate(
            type=IntegrationType.DISCORD,
            external_workspace_id="G123",
            config={"token": "abc"},
            allowed_channels=["C1", "C2"],
        )
        assert model.external_workspace_id == "G123"
        assert len(model.allowed_channels) == 2

    def test_missing_required_field(self) -> None:
        with pytest.raises(ValidationError):
            IntegrationCreate(type=IntegrationType.SLACK)  # type: ignore[call-arg]

    def test_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            IntegrationCreate(type="telegram", config={})  # type: ignore[arg-type]


class TestIntegrationUpdate:
    def test_all_optional(self) -> None:
        model = IntegrationUpdate()
        assert model.status is None
        assert model.allowed_channels is None
        assert model.config is None

    def test_partial_update(self) -> None:
        model = IntegrationUpdate(status=IntegrationStatus.DISABLED)
        assert model.status == IntegrationStatus.DISABLED
        assert model.config is None


class TestIntegrationResponse:
    def test_valid(self) -> None:
        now = datetime.now(timezone.utc)
        uid = uuid4()
        resp = IntegrationResponse(
            id=uid,
            organization_id=uuid4(),
            type=IntegrationType.SLACK,
            status=IntegrationStatus.ACTIVE,
            external_workspace_id="W123",
            allowed_channels=["C1"],
            config_version=1,
            created_by=uuid4(),
            updated_by=None,
            created_at=now,
            updated_at=now,
            disabled_at=None,
            revoked_at=None,
        )
        assert resp.id == uid
        assert resp.config_version == 1


class TestUserLinkCreate:
    def test_valid(self) -> None:
        model = UserLinkCreate(
            integration_id=uuid4(),
            user_id=uuid4(),
            external_user_id="U123",
        )
        assert model.verification_method == VerificationMethod.PAIRING_CODE

    def test_admin_verification(self) -> None:
        model = UserLinkCreate(
            integration_id=uuid4(),
            user_id=uuid4(),
            external_user_id="U456",
            verification_method=VerificationMethod.ADMIN,
        )
        assert model.verification_method == VerificationMethod.ADMIN


class TestPairingRedeemRequest:
    def test_valid(self) -> None:
        model = PairingRedeemRequest(code="ABC123")
        assert model.code == "ABC123"

    def test_empty_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PairingRedeemRequest(code="")


class TestIntegrationListResponse:
    def test_empty_list(self) -> None:
        resp = IntegrationListResponse(integrations=[], total_count=0)
        assert resp.total_count == 0
        assert resp.integrations == []


class TestUserLinkListResponse:
    def test_empty_list(self) -> None:
        resp = UserLinkListResponse(links=[], total_count=0)
        assert resp.total_count == 0
        assert resp.links == []


class TestPairingChallengeCreate:
    def test_valid(self) -> None:
        iid = uuid4()
        model = PairingChallengeCreate(integration_id=iid)
        assert model.integration_id == iid

    def test_missing_integration_id(self) -> None:
        with pytest.raises(ValidationError):
            PairingChallengeCreate()  # type: ignore[call-arg]


class TestPairingChallengeResponse:
    def test_valid_with_code(self) -> None:
        now = datetime.now(timezone.utc)
        resp = PairingChallengeResponse(
            id=uuid4(),
            integration_id=uuid4(),
            user_id=uuid4(),
            code="ABC123",
            expires_at=now,
            status=PairingChallengeStatus.PENDING,
            created_at=now,
        )
        assert resp.code == "ABC123"

    def test_code_defaults_to_none(self) -> None:
        now = datetime.now(timezone.utc)
        resp = PairingChallengeResponse(
            id=uuid4(),
            integration_id=uuid4(),
            user_id=uuid4(),
            expires_at=now,
            status=PairingChallengeStatus.USED,
            created_at=now,
        )
        assert resp.code is None


# ============================================================================
# Serialization round-trips — model_dump → model_validate
# ============================================================================


class TestSerializationRoundTrips:
    """Pydantic models survive dump → validate without data loss."""

    def test_integration_create_round_trip(self) -> None:
        original = IntegrationCreate(
            type=IntegrationType.DISCORD,
            external_workspace_id="G789",
            config={"token": "secret", "nested": {"a": 1}},
            allowed_channels=["C1", "C2"],
        )
        data = original.model_dump()
        restored = IntegrationCreate.model_validate(data)
        assert restored == original

    def test_integration_create_json_round_trip(self) -> None:
        original = IntegrationCreate(
            type=IntegrationType.SLACK,
            config={"bot_token": "xoxb-123"},
        )
        json_str = original.model_dump_json()
        restored = IntegrationCreate.model_validate_json(json_str)
        assert restored == original

    def test_integration_update_round_trip(self) -> None:
        original = IntegrationUpdate(
            status=IntegrationStatus.REVOKED,
            allowed_channels=["C3"],
            config={"new_token": "abc"},
        )
        data = original.model_dump()
        restored = IntegrationUpdate.model_validate(data)
        assert restored == original

    def test_integration_response_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        original = IntegrationResponse(
            id=uuid4(),
            organization_id=uuid4(),
            type=IntegrationType.SLACK,
            status=IntegrationStatus.ACTIVE,
            external_workspace_id="W1",
            allowed_channels=["C1"],
            config_version=3,
            created_by=uuid4(),
            updated_by=uuid4(),
            created_at=now,
            updated_at=now,
            disabled_at=now,
            revoked_at=None,
        )
        data = original.model_dump()
        restored = IntegrationResponse.model_validate(data)
        assert restored == original

    def test_integration_response_json_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        original = IntegrationResponse(
            id=uuid4(),
            organization_id=uuid4(),
            type=IntegrationType.SLACK,
            status=IntegrationStatus.ACTIVE,
            external_workspace_id=None,
            allowed_channels=[],
            config_version=1,
            created_by=uuid4(),
            updated_by=None,
            created_at=now,
            updated_at=now,
            disabled_at=None,
            revoked_at=None,
        )
        json_str = original.model_dump_json()
        restored = IntegrationResponse.model_validate_json(json_str)
        assert restored == original

    def test_user_link_create_round_trip(self) -> None:
        original = UserLinkCreate(
            integration_id=uuid4(),
            user_id=uuid4(),
            external_user_id="U999",
            external_workspace_id="W111",
            verification_method=VerificationMethod.OAUTH,
        )
        data = original.model_dump()
        restored = UserLinkCreate.model_validate(data)
        assert restored == original

    def test_user_link_response_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        original = UserLinkResponse(
            id=uuid4(),
            organization_id=uuid4(),
            integration_id=uuid4(),
            user_id=uuid4(),
            provider=IntegrationType.DISCORD,
            external_user_id="D456",
            external_workspace_id="G789",
            status=UserLinkStatus.ACTIVE,
            verification_method=VerificationMethod.ADMIN,
            linked_at=now,
            created_at=now,
            updated_at=now,
        )
        json_str = original.model_dump_json()
        restored = UserLinkResponse.model_validate_json(json_str)
        assert restored == original

    def test_pairing_challenge_response_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        original = PairingChallengeResponse(
            id=uuid4(),
            integration_id=uuid4(),
            user_id=uuid4(),
            code="XYZ789",
            expires_at=now,
            status=PairingChallengeStatus.PENDING,
            created_at=now,
        )
        data = original.model_dump()
        restored = PairingChallengeResponse.model_validate(data)
        assert restored == original

    def test_pairing_redeem_request_round_trip(self) -> None:
        original = PairingRedeemRequest(code="PAIR-42")
        data = original.model_dump()
        restored = PairingRedeemRequest.model_validate(data)
        assert restored == original

    def test_list_responses_round_trip(self) -> None:
        il = IntegrationListResponse(integrations=[], total_count=0)
        assert IntegrationListResponse.model_validate(il.model_dump()) == il

        ul = UserLinkListResponse(links=[], total_count=0)
        assert UserLinkListResponse.model_validate(ul.model_dump()) == ul
